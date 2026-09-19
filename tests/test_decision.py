"""The optional cloud boundary never invents evidence or leaks provider failures."""

from dataclasses import replace
import io
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from cms import decision

_REAL_URLOPEN = decision._urlopen


@pytest.fixture(autouse=True)
def isolated_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Unexpected network request")

    monkeypatch.setattr(decision, "_urlopen", forbidden)
    yield
    assert not attempts, "Transport was reached by an operation that must stay local"


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-private-key")
    return decision.DecisionSettings(mode="shadow", allow_cloud=True)


def candidates():
    return [{"id": "node:a", "name": "save", "path": "cms/storage.py", "kind": "function",
             "summary": "Writes graph data to disk.", "source_code": "MUST NOT LEAVE",
             "credentials": "MUST NOT LEAVE"},
            {"id": "node:b", "name": "draw", "path": "cms/ui.py", "kind": "function",
             "summary": "Draws the graph."}]


def answer():
    return {"model": decision.DEFAULT_MODEL,
            "answers": {"q0": {"type": "noul", "noul": 0.96},
                        "q1": {"type": "noul", "noul": 0.1}},
            "usage": {"input_tokens": 432, "output_tokens": 20}}


class Response(io.BytesIO):
    status = 200

    def geturl(self):
        return decision.ENDPOINT


def transport(monkeypatch, result=None, raw=None):
    calls = []

    def open_request(request, *, timeout):
        calls.append((request, timeout))
        return Response(raw if raw is not None else json.dumps(result or answer()).encode())

    monkeypatch.setattr(decision, "_urlopen", open_request)
    return calls


def test_missing_config_and_key_keep_decisions_off(tmp_path):
    settings = decision.load_settings(tmp_path)
    assert settings == decision.DecisionSettings()
    report = decision.status(tmp_path)
    assert report["mode"] == "off"
    assert report["configured"] is report["allow_cloud"] is report["ready"] is False
    with pytest.raises(decision.DecisionUnavailableError, match="off"):
        decision.JevDecisionProvider(settings).rank("storage", candidates())


@pytest.mark.parametrize("values", [
    {"mode": "enabled"}, {"allow_cloud": "true"}, {"allow_cloud": 1},
    {"model": "jev-latest"}, {"model": "https://evil.example"},
    {"timeout_seconds": 0}, {"timeout_seconds": 16}, {"timeout_seconds": True},
    {"timeout_seconds": 10 ** 400},
    {"max_candidates": 65}, {"max_candidates": 0}, {"max_candidates": True},
    {"max_input_chars": 64001}, {"max_input_chars": 1},
    {"api_key": "DO_NOT_ECHO"}, {"endpoint": "https://evil.example"},
    {"configured": True}, {"mode": []},
])
def test_bad_settings_fail_closed_without_echoing_input(tmp_path, values):
    (tmp_path / decision.CONFIG_NAME).write_text(json.dumps(values), encoding="utf-8")
    with pytest.raises(decision.DecisionConfigError) as error:
        decision.load_settings(tmp_path)
    assert "DO_NOT_ECHO" not in str(error.value)
    report = decision.status(tmp_path)
    assert report["ready"] is False and report["allow_cloud"] is False


@pytest.mark.parametrize("raw", [b"{", b"[]", b'{"mode":"off","mode":"assist"}',
                                  b'{"timeout_seconds":NaN}', b'{"mode":"\xff"}', b" " * 17000],
                         ids=["syntax", "array", "duplicate", "nan", "encoding", "oversized"])
def test_invalid_config_json_is_sanitized(tmp_path, raw):
    (tmp_path / decision.CONFIG_NAME).write_bytes(raw)
    with pytest.raises(decision.DecisionConfigError):
        decision.load_settings(tmp_path)
    assert decision.status(tmp_path)["ready"] is False


def test_explicit_cloud_permission_and_key_are_both_required(tmp_path, monkeypatch):
    path = tmp_path / decision.CONFIG_NAME
    path.write_text('{"mode":"shadow"}', encoding="utf-8")
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-in-status")
    assert "allow_cloud" in decision.status(tmp_path)["reason"]
    with pytest.raises(decision.DecisionUnavailableError):
        decision.JevDecisionProvider(decision.load_settings(tmp_path)).rank("storage", candidates())
    path.write_text('{"mode":"assist","allow_cloud":true}', encoding="utf-8")
    assert decision.status(tmp_path)["ready"] is True
    assert "not-in-status" not in json.dumps(decision.status(tmp_path))
    monkeypatch.delenv("TYPESAFE_API_KEY")
    assert decision.status(tmp_path)["ready"] is False
    with pytest.raises(decision.DecisionUnavailableError, match="not configured"):
        decision.JevDecisionProvider(decision.load_settings(tmp_path)).rank("storage", candidates())


@pytest.mark.parametrize("key", ["key\r\nInjected: yes", "key with spaces", "非ascii", "x" * 5000])
def test_invalid_credential_never_reaches_transport(enabled, monkeypatch, key):
    monkeypatch.setenv("TYPESAFE_API_KEY", key)
    with pytest.raises(decision.DecisionUnavailableError) as error:
        decision.JevDecisionProvider(enabled).rank("storage", candidates())
    assert key not in str(error.value)


def test_rank_uses_independent_questions_and_only_approved_fields(enabled, monkeypatch):
    calls = transport(monkeypatch)
    result = decision.JevDecisionProvider(enabled).rank("Where does graph persistence happen?", candidates())
    assert result["scores"] == {"node:a": 0.96, "node:b": 0.1}
    assert result["model"] == decision.DEFAULT_MODEL
    assert result["usage"] == {"input_tokens": 432, "output_tokens": 20}
    assert result["elapsed_ms"] >= 0
    assert len(calls) == 1
    request, timeout = calls[0]
    body = json.loads(request.data)
    assert request.full_url == decision.ENDPOINT and request.method == "POST"
    assert request.get_header("Authorization") == "Bearer test-private-key"
    assert 0 < timeout <= enabled.timeout_seconds
    assert body["model"] == decision.DEFAULT_MODEL
    assert set(body["questions"]) == {"q0", "q1"}
    for index, item in enumerate(body["state"]["candidates"]):
        assert set(item) == {"name", "path", "kind", "summary"}
        assert body["questions"][f"q{index}"]["type"] == "noul"
        assert f"candidates[{index}]" in body["questions"][f"q{index}"]["instructions"]
    assert "MUST NOT LEAVE" not in request.data.decode()
    assert "node:a" not in request.data.decode()
    assert "test-private-key" not in json.dumps(result)


def test_long_fields_are_bounded_and_empty_candidates_do_not_call(enabled, monkeypatch):
    calls = transport(monkeypatch)
    data = candidates()
    data[0]["summary"] = "a" * 5000
    decision.JevDecisionProvider(enabled).rank("storage", data)
    assert len(json.loads(calls[0][0].data)["state"]["candidates"][0]["summary"]) == 1200
    result = decision.JevDecisionProvider(enabled).rank("storage", [])
    assert result["scores"] == {} and result["usage"]["input_tokens"] == 0
    assert len(calls) == 1


@pytest.mark.parametrize("data", [
    [{"id": "a"}, {"id": "a"}], [{"id": ""}], [{"id": 12}], [{"id": "a", "summary": {}}],
    ["not-a-record"], candidates() * 20,
])
def test_bad_candidate_sets_do_not_call(enabled, data):
    with pytest.raises(decision.DecisionProviderError):
        decision.JevDecisionProvider(enabled).rank("storage", data)


@pytest.mark.parametrize("query", ["", " " * 20, "x" * 4001, None, "\ud800"])
def test_bad_queries_do_not_call(enabled, query):
    with pytest.raises(decision.DecisionProviderError):
        decision.JevDecisionProvider(enabled).rank(query, candidates())


def test_unicode_and_cumulative_budgets_apply_before_sending(enabled):
    data = [{"id": str(index), "summary": "界" * 1200} for index in range(12)]
    with pytest.raises(decision.DecisionProviderError, match="budget"):
        decision.JevDecisionProvider(enabled).rank("storage", data)
    with pytest.raises(decision.DecisionProviderError, match="budget"):
        decision.JevDecisionProvider(replace(enabled, max_input_chars=1024)).rank("storage", candidates())


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(model="jev-latest"),
    lambda r: r["answers"].pop("q0"),
    lambda r: r["answers"].update(invented={"type": "noul", "noul": 1}),
    lambda r: r["answers"]["q0"].update(type="score"),
    lambda r: r["answers"]["q0"].update(reason="invented evidence"),
    lambda r: r["answers"]["q0"].update(noul=True),
    lambda r: r["answers"]["q0"].update(noul="0.99"),
    lambda r: r["answers"]["q0"].update(noul=-0.1),
    lambda r: r["answers"]["q0"].update(noul=1.1),
    lambda r: r["answers"]["q0"].update(noul=float("nan")),
    lambda r: r["answers"]["q0"].update(noul=float("inf")),
    lambda r: r["usage"].update(input_tokens=-1),
    lambda r: r["usage"].update(input_tokens=True),
    lambda r: r["usage"].update(output_tokens="20"),
    lambda r: r["usage"].pop("input_tokens"),
    lambda r: r.update(usage=None),
])
def test_untrusted_responses_are_rejected_atomically(enabled, monkeypatch, mutation):
    data = answer()
    mutation(data)
    calls = transport(monkeypatch, result=data)
    with pytest.raises(decision.DecisionProviderError):
        decision.JevDecisionProvider(enabled).rank("storage", candidates())
    assert len(calls) == 1  # No implicit retry, partial result, or fallback call.


@pytest.mark.parametrize("raw", [b"not-json-private-body", b"\xff", b"[]", b"x" * 65537,
    b'{"model":"jev-1.13.0","model":"jev-1.13.0"}',
    b'{"answers":{"q0":{"type":"noul","noul":0.9,"noul":0.1}}}'],
                         ids=["syntax", "encoding", "array", "oversized", "duplicate-model", "duplicate-answer"])
def test_malformed_response_body_is_never_exposed(enabled, monkeypatch, raw):
    transport(monkeypatch, raw=raw)
    with pytest.raises(decision.DecisionProviderError) as error:
        decision.JevDecisionProvider(enabled).rank("storage", candidates())
    assert "private-body" not in str(error.value)


@pytest.mark.parametrize("code", [401, 403, 429, 500, 529])
def test_provider_http_failures_are_sanitized_without_retry(enabled, monkeypatch, code):
    calls = []

    def fail(*a, **kw):
        calls.append(1)
        raise urllib.error.HTTPError("https://private-url", code, "private-key", {},
                                     io.BytesIO(b"private-response"))

    monkeypatch.setattr(decision, "_urlopen", fail)
    with pytest.raises(decision.DecisionProviderError) as error:
        decision.JevDecisionProvider(enabled).rank("storage", candidates())
    assert "private" not in str(error.value)
    assert len(calls) == 1


def test_network_exception_is_sanitized(enabled, monkeypatch):
    def fail(*a, **kw):
        raise OSError("private query, credential, or provider body")

    monkeypatch.setattr(decision, "_urlopen", fail)
    with pytest.raises(decision.DecisionProviderError) as error:
        decision.JevDecisionProvider(enabled).rank("storage", candidates())
    assert "private" not in str(error.value)


def test_redirect_handler_blocks_credential_forwarding():
    request = urllib.request.Request(decision.ENDPOINT, headers={"Authorization": "Bearer secret"})
    with pytest.raises(decision.DecisionProviderError, match="redirects"):
        decision._NoRedirect().redirect_request(request, None, 302, "Found", {}, "https://evil.example")


def test_real_opener_installs_redirect_blocker(monkeypatch):
    seen = []

    class Opener:
        def open(self, request, *, timeout):
            assert request.full_url == decision.ENDPOINT and timeout == 1
            return "fake-opened"

    def build_opener(*handlers):
        seen.extend(handlers)
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    assert _REAL_URLOPEN(urllib.request.Request(decision.ENDPOINT), timeout=1) == "fake-opened"
    assert any(isinstance(handler, decision._NoRedirect) for handler in seen)


def test_deadline_bounds_caller_when_transport_stalls(enabled, monkeypatch):
    release = threading.Event()
    finished = threading.Event()

    def slow_open(*a, **kw):
        try:
            release.wait(2)
            return Response(json.dumps(answer()).encode())
        finally:
            finished.set()

    monkeypatch.setattr(decision, "_urlopen", slow_open)
    start = time.monotonic()
    try:
        with pytest.raises(decision.DecisionProviderError, match="deadline"):
            decision.JevDecisionProvider(replace(enabled, timeout_seconds=0.1)).rank("storage", candidates())
        assert time.monotonic() - start < 1
    finally:
        release.set()
        assert finished.wait(1)
