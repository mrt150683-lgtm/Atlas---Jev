from hashlib import sha256
from types import SimpleNamespace

import networkx as nx
import pytest

from cms import context_evaluation as evaluation
from cms.memory import CodebaseMemory


@pytest.fixture
def project(tmp_path):
    graph = nx.DiGraph()
    for name in ("auth", "math", "other"):
        source = f"def {name}():\n    pass\n"
        (tmp_path / f"{name}.py").write_text(source, encoding="utf-8")
        digest = sha256((tmp_path / f"{name}.py").read_bytes()).hexdigest()
        graph.add_node(f"file:{name}.py", type="file", name=f"{name}.py", path=f"{name}.py",
                       summary=f"{name} behavior", content_hash=digest, summary_meta={"content_hash": digest})
    CodebaseMemory(graph).save(tmp_path / ".memory" / "graph.json")
    return tmp_path


def receipt(baseline, selected, proposed=(), *, status="applied", mode="assist", candidates=None,
            cache=False, usage=10):
    return SimpleNamespace(receipt={"id": "test", "status": status, "mode": mode,
        "baseline_ids": baseline, "selected_ids": selected, "proposed_ids": list(proposed),
        "candidates": [{"id": node} for node in (candidates if candidates is not None else baseline)],
        "elapsed_ms": 2, "cache_hit": cache, "usage": {"input_tokens": usage, "output_tokens": 1},
        "cached_usage": {"input_tokens": 9, "output_tokens": 2} if cache else {}})


def test_paired_metrics_include_misses_and_separate_cached_usage(project, monkeypatch):
    responses = iter([
        receipt(["file:math.py", "file:auth.py"], ["file:auth.py"], ["file:auth.py"]),
        receipt(["file:math.py"], ["file:math.py"], ["file:math.py"],
                mode="shadow", status="shadow", cache=True, usage=0),
    ])
    calls = []
    def choose(memory, root, query, **kwargs):
        calls.append((query, kwargs))
        return next(responses)
    monkeypatch.setattr(evaluation, "select_context", choose)
    report = evaluation.evaluate(project, [
        {"id": "auth", "query": "auth", "expected_ids": ["file:auth.py"]},
        {"id": "missing", "query": "other", "expected_ids": ["file:other.py"]}])
    assert report["metrics"]["baseline"]["precision"] == {"numerator": 1, "denominator": 3, "value": 1 / 3}
    assert report["metrics"]["selected"]["precision"]["value"] == 0.5
    assert report["metrics"]["selected"]["recall"]["value"] == 0.5
    assert report["candidate_recall"]["value"] == 0.5
    assert report["shortlist_miss_cases"] == 1
    assert report["cases"][1]["missing_from_shortlist"] == ["file:other.py"]
    assert report["usage"]["input_tokens"] == 10
    assert report["cached_usage"]["input_tokens"] == 9
    assert report["modes"] == {"assist": 1, "shadow": 1}
    assert report["excluded_cases"] == 0
    assert all(call[1] == {"top_k": 5, "source": "evaluation", "record": False} for call in calls)


def test_unanswerable_abstention_never_rewards_failed_provider(project, monkeypatch):
    replies = iter([receipt(["file:auth.py"], ["file:auth.py"], status="no_match"),
                    receipt([], [], status="fallback"), receipt([], [], status="off", mode="off")])
    monkeypatch.setattr(evaluation, "select_context", lambda *a, **k: next(replies))
    report = evaluation.evaluate(project, [{"id": str(n), "query": "unrelated", "expected_ids": []} for n in range(3)])
    assert report["unanswerable"]["abstention_signal_rate"]["value"] == 2 / 3
    assert report["unanswerable"]["no_match_signals"] == 1
    assert report["unanswerable"]["empty_selection_cases"] == 2
    assert report["fallback_count"] == 1
    assert report["metrics"]["selected"]["recall"]["value"] is None


def test_unexpected_failure_is_counted_and_error_details_are_redacted(project, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private provider response")
    monkeypatch.setattr(evaluation, "select_context", fail)
    report = evaluation.evaluate(project, [{"id": "a", "query": "auth", "expected_ids": ["file:auth.py"]}])
    assert report["cases_total"] == report["error_count"] == 1
    assert report["metrics"]["selected"]["recall"]["value"] == 0
    assert report["cases"][0]["baseline_ids"] == ["file:auth.py"]
    assert report["cases"][0]["error"] == "RuntimeError"
    assert "private provider response" not in str(report)


@pytest.mark.parametrize("invalid", [
    {"id": " a ", "query": "ok", "expected_ids": []},
    {"id": "b", "query": "x" * 4001, "expected_ids": []},
    {"id": "b", "query": "ok", "expected_ids": ["missing"]},
    {"id": "b", "query": "ok", "expected_ids": [], "answerable": True},
    {"id": "b", "query": "ok", "expected_ids": ["file:auth.py"], "answerable": False},
    {"id": "b", "query": "ok", "expected_ids": ["file:auth.py", "file:auth.py"]},
])
def test_all_cases_preflight_before_any_provider_call(project, monkeypatch, invalid):
    calls = []
    monkeypatch.setattr(evaluation, "select_context", lambda *a, **k: calls.append(True))
    with pytest.raises(ValueError):
        evaluation.evaluate(project, [{"id": "a", "query": "auth", "expected_ids": ["file:auth.py"]}, invalid])
    assert calls == []


def test_actual_off_mode_is_local_and_does_not_create_policy(project, monkeypatch):
    from cms import decision
    def forbidden(*args, **kwargs):
        raise AssertionError("No remote transport in off mode")
    monkeypatch.setattr(decision, "_urlopen", forbidden)
    report = evaluation.evaluate(project, [{"id": "auth", "query": "auth", "expected_ids": ["file:auth.py"]}])
    assert report["protocol"] == "retrieval diagnostic, not coding effectiveness"
    assert report["statuses"] == {"off": 1}
    assert report["metrics"]["baseline"] == report["metrics"]["selected"]
    assert report["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert not (project / decision.CONFIG_NAME).exists()
    assert not (project / ".memory" / "context" / "history.json").exists()
    case = report["cases"][0]
    assert case["receipt_persisted"] is False
    assert case["receipt"]["id"] == case["receipt_id"]
    assert len(case["receipt"]["graph_fingerprint"]) == 64
    assert case["receipt"]["policy_version"]
    assert case["receipt"]["candidates"][0]["source_freshness"] == "current"
    assert case["receipt"]["selected_ids"] == case["selected_ids"]


def test_report_embeds_an_independent_receipt_snapshot(project, monkeypatch):
    response = receipt(["file:auth.py"], ["file:auth.py"])
    response.receipt.update(graph_fingerprint="evidence-at-evaluation", policy_version="policy-at-evaluation")
    response.receipt["candidates"][0]["source_freshness"] = "stale"
    monkeypatch.setattr(evaluation, "select_context", lambda *a, **k: response)
    report = evaluation.evaluate(project, [{"id": "auth", "query": "auth", "expected_ids": ["file:auth.py"]}])
    response.receipt["candidates"][0]["source_freshness"] = "current"
    saved = report["cases"][0]["receipt"]
    assert saved["graph_fingerprint"] == "evidence-at-evaluation"
    assert saved["policy_version"] == "policy-at-evaluation"
    assert saved["candidates"][0]["source_freshness"] == "stale"
