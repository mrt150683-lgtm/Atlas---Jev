"""Known-stale evidence stays labelled in the prompts humans and agents receive."""
import json
import re

import pytest

from cms import decision
from cms.chat import ask
from cms.features import build_features
from cms.graph_builder import build_graph
from cms.memory import CodebaseMemory
from cms.mcp import MCPServer
from cms.prompt_export import export_prompt
from cms.providers import MockProvider
from cms.scanner import scan
from cms.summarizer import generate_summaries

SOURCE = '''# @memory:feature:TokenChecking
# @memory:summary:Token verification.
def verify_token(token):
    return token
'''
QUERY = "Inspect auth.py and TokenChecking token verification"
WARNING = "Dynamic call targets are not resolved by this structural evidence."
FEATURE_WARNING = "This feature trace does not cover dynamic dispatch."


@pytest.fixture
def stale_project(tmp_path, monkeypatch):
    attempts = []
    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Freshness surface checks must remain offline")
    monkeypatch.setattr(decision, "_urlopen", forbidden)
    source = tmp_path / "auth.py"
    source.write_text(SOURCE, encoding="utf-8")
    graph = build_graph(scan(tmp_path))
    generate_summaries(graph, tmp_path, MockProvider())
    build_features(graph, MockProvider())
    for _, attrs in graph.nodes(data=True):
        if attrs.get("path") == "auth.py":
            attrs["analysis_warnings"] = [WARNING]
    graph.nodes["feature:TokenChecking"]["analysis_warnings"] = [FEATURE_WARNING]
    CodebaseMemory(graph).save(tmp_path / ".memory" / "graph.json")
    # Change the real bytes after indexing. Do not mock the freshness detector.
    source.write_text(SOURCE.replace("return token", "return token.strip()"), encoding="utf-8")
    yield tmp_path
    assert attempts == []


def test_actual_answer_prompt_contains_stale_source_and_analysis_limits(stale_project):
    class CapturingProvider:
        name, model = "offline-answer-fixture", "fixture-v1"
        def summarize(self, prompt, context):
            self.prompt = prompt
            return "The indexed evidence is stale; inspect auth.py before judging its behavior."
    provider = CapturingProvider()
    entry = ask(stale_project, QUERY, provider)
    evidence = json.loads(re.search(r"<evidence_json>\s*(.*?)\s*</evidence_json>", provider.prompt, re.S).group(1))
    stale_hits = [hit for hit in evidence["ranked_hits"] if hit["path"] == "auth.py"]
    assert stale_hits and all(hit["source_freshness"] == "stale" for hit in stale_hits)
    assert all(WARNING in hit["analysis_warnings"] for hit in stale_hits)
    feature = next(row for row in evidence["matched_features"] if row["feature"] == "TokenChecking")
    assert feature["source_freshness"] == "stale"
    assert FEATURE_WARNING in feature["analysis_warnings"]
    assert entry["context_selection"]["mode"] == "off"
    assert "If evidence is stale" in provider.prompt


def test_actual_exported_json_and_markdown_retain_limits(stale_project):
    content, json_path = export_prompt(stale_project, QUERY, as_json=True)
    pack = json.loads(content)
    assert json.loads(json_path.read_text(encoding="utf-8")) == pack
    assert pack["context_selection"]["mode"] == "off"
    relevant = [hit for hit in pack["relevant_code"] if hit["path"] == "auth.py"]
    assert relevant and all(hit["source_freshness"] == "stale" for hit in relevant)
    assert all(WARNING in hit["analysis_warnings"] for hit in relevant)
    content, markdown_path = export_prompt(stale_project, QUERY)
    assert markdown_path.read_text(encoding="utf-8") == content
    assert "Source freshness: stale. Source changed since indexing." in content
    assert f"Analysis limitation: {WARNING}" in content
    assert f"Analysis limitation: {FEATURE_WARNING}" in content


def test_agent_intent_response_retains_per_target_freshness(stale_project):
    server = MCPServer(stale_project)
    reply = server.declare_intent(QUERY)
    assert reply["context_selection"]["mode"] == "off"
    assert reply["context_selection"]["id"]
    relevant = [hit for hit in reply["relevant_code"] if hit["path"] == "auth.py"]
    assert relevant and all(hit["source_freshness"] == "stale" for hit in relevant)
    assert all(WARNING in hit["analysis_warnings"] for hit in relevant)
    assert server.get_context_decision(reply["context_selection"]["id"])["id"] == reply["context_selection"]["id"]
