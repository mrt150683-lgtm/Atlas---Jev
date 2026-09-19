"""The real HTTP, MCP, brief and chat paths share a durable selection receipt."""
import http.client
import io
import json
import threading
from http.server import ThreadingHTTPServer

import pytest
from typer.testing import CliRunner

from cms.chat import ask
from cms.cli import app
from cms.context_selection import get_receipt
from cms.decision import ENDPOINT
from cms.graph_builder import build_graph
from cms.memory import CodebaseMemory
from cms.mcp import MCPServer
from cms.prompt_export import build_task_pack
from cms.providers import MockProvider
from cms.scanner import scan
from cms.summarizer import generate_summaries
from cms.ui import _MemoryCache, make_handler


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "retry.py").write_text('def retry_request():\n    return "RAW_SOURCE_CANARY_DO_NOT_SEND"\n')
    (tmp_path / "decoder.py").write_text('def decode_response():\n    """Decode a retry transport response."""\n    return {}\n')
    graph = build_graph(scan(tmp_path))
    generate_summaries(graph, tmp_path, MockProvider())
    memory = CodebaseMemory(graph)
    memory.save(tmp_path / ".memory/graph.json")
    (tmp_path / ".atlas-decisions.json").write_text(json.dumps({"mode": "assist", "allow_cloud": True}))
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-key-never-persist")
    sent = []

    class Response(io.BytesIO):
        status = 200

        def geturl(self):
            return ENDPOINT

    def transport(request, *, timeout):
        payload = json.loads(request.data)
        sent.append(payload)
        answers = {f"q{i}": {"type": "noul", "noul": .99 if row["name"] == "decode_response" else .1}
                   for i, row in enumerate(payload["state"]["candidates"])}
        return Response(json.dumps({"model": payload["model"], "answers": answers,
                                   "usage": {"input_tokens": 345, "output_tokens": 0}}).encode())

    monkeypatch.setattr("cms.decision._urlopen", transport)
    return tmp_path, memory, sent


@pytest.fixture
def viewer(project):
    root, _, _ = project
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root, _MemoryCache(root / ".memory/graph.json")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)

    def request(path, body=None, headers=None):
        connection.request("GET" if body is None else "POST", path,
                           None if body is None else json.dumps(body), headers or {})
        response = connection.getresponse()
        code, content = response.status, response.read()
        if code in (403, 409):  # The server deliberately closes rejected sessions.
            connection.close()
        return code, content

    session = json.loads(request("/api/session")[1])
    headers = {"Content-Type": "application/json", "X-Atlas-Session": session["token"],
               "X-Atlas-Project": session["project"]}
    yield request, headers
    connection.close()
    server.shutdown()
    server.server_close()


def test_readonly_viewer_routes_and_typeahead_never_call_jev(viewer, project):
    request, _ = viewer
    for route in ("/context", "/api/context/status", "/api/context/history", "/api/query?q=retry",
                  "/api/prompt?task=retry"):
        code, body = request(route)
        assert code == 200, body
    assert project[2] == []
    assert b"window.ATLAS_SESSION" in request("/context")[1]
    assert b'Context decisions' in request("/")[1]


def test_preview_and_mcp_share_actual_wire_contract_cache_and_receipt(viewer, project):
    request, headers = viewer
    root, _, sent = project
    code, raw = request("/api/context/preview", {"query": "retry", "top_k": 1}, headers)
    assert code == 200
    preview = json.loads(raw)
    assert preview["selection"]["status"] == "applied"
    assert preview["results"][0]["name"] == "decode_response"
    assert len(sent) == 1
    payload = sent[0]
    assert set(payload) == {"state", "questions", "model"}
    assert "RAW_SOURCE_CANARY" not in json.dumps(payload)
    assert "synthetic-key" not in json.dumps(payload)
    assert all(set(row) == {"name", "path", "kind", "summary"} for row in payload["state"]["candidates"])

    server = MCPServer(root)
    hits = server.query_codebase("retry", 1)
    assert hits[0]["node_id"] == preview["results"][0]["node_id"]
    assert len(sent) == 1  # Same evidence/query/policy coalesces across surfaces.
    receipt = server.get_context_decision(hits[0]["selection"]["receipt_id"])
    assert receipt["cache_hit"] is True and receipt["usage"]["input_tokens"] == 0
    fetched = json.loads(request("/api/context/receipt?id=" + receipt["id"])[1])
    assert fetched == receipt
    assert len(json.loads(request("/api/context/history")[1])["items"]) == 2
    assert "synthetic-key" not in (root / ".memory/context/history.json").read_text()
    assert "synthetic-key" not in (root / ".memory/context/cache.json").read_text()


def test_preview_requires_current_session_and_valid_query(viewer, project):
    request, headers = viewer
    assert request("/api/context/preview", {"query": "retry"}, {"Content-Type": "application/json"})[0] == 409
    assert request("/api/context/preview", {"query": "retry"}, {**headers, "X-Atlas-Session": "wrong"})[0] == 403
    assert request("/api/context/preview", {"query": "", "top_k": 1}, headers)[0] == 400
    assert request("/api/context/preview", {"query": "retry", "top_k": True}, headers)[0] == 400
    assert request("/api/context/receipt?id=../../config")[0] == 400
    assert project[2] == []


def test_task_pack_and_chat_return_inspectable_selection_without_relabeling_evidence(project):
    root, memory, _ = project
    pack = build_task_pack(memory, root, "retry", top_k=1)
    receipt = get_receipt(root, pack["context_selection"]["id"])
    assert receipt["selected_ids"] == ["func:decoder.py::decode_response"]
    assert pack["relevant_code"][0]["name"] == "decode_response"
    prompts = []

    class AnswerProvider:
        name, model = "test", "answer-model"

        def summarize(self, prompt, context):
            prompts.append(prompt)
            return "Inspect decoder.py to understand how the retry response is decoded."

    entry = ask(root, "retry", AnswerProvider())
    assert entry["context_selection"]["id"] in prompts[0]
    assert "Relevance estimates are model judgments" in prompts[0]
    persisted = json.loads((root / ".memory/chat.jsonl").read_text().splitlines()[-1])
    assert persisted["context_selection"] == entry["context_selection"]


def test_cli_context_produces_same_machine_readable_record(project):
    root, _, _ = project
    result = CliRunner().invoke(app, ["context", "retry", "--root", str(root), "--top-k", "1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["results"][0]["name"] == "decode_response"
    saved = CliRunner().invoke(app, ["context", "--root", str(root), "--receipt", payload["selection"]["id"]])
    assert saved.exit_code == 0
    assert json.loads(saved.output) == payload["selection"]


def test_declared_intent_returns_the_receipt_used_by_its_context(project):
    root, _, _ = project
    server = MCPServer(root)
    declared = server.declare_intent("retry")
    compact = declared["context_selection"]
    receipt = server.get_context_decision(compact["id"])
    assert compact["status"] == receipt["status"] == "applied"
    assert {row["name"] for row in declared["relevant_code"]} == {
        row["name"] for row in receipt["candidates"] if row["selected"]}


def test_evaluation_refuses_to_overwrite_labels_before_remote_calls(project):
    root, _, sent = project
    labels = root / "cases.json"
    labels.write_text(json.dumps([{"id": "retry", "query": "retry",
                                  "expected_ids": ["func:decoder.py::decode_response"]}]))
    original = labels.read_bytes()
    result = CliRunner().invoke(app, ["context-evaluate", str(labels), "--root", str(root), "--out", str(labels)])
    assert result.exit_code == 1
    assert "must not overwrite" in result.output
    assert labels.read_bytes() == original and sent == []
