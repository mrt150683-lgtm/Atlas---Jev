"""Real source/graph journeys through optional, auditable context ranking."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time

import pytest

from cms import context_selection, decision
from cms.graph_builder import build_graph, graph_to_json
from cms.features import build_features
from cms.memory import CodebaseMemory
from cms.providers import MockProvider
from cms.scanner import scan
from cms.scope import save_scope
from cms.summarizer import generate_summaries


SOURCES = {
    "storage.py": '''"""Store records in durable local storage."""
def store_records(records):
    """Store records and preserve their order."""
    return list(records)
''',
    "worker.py": '''"""Processing record batches."""
from storage import store_records
def process_batch(records):
    """Store records after processing a batch."""
    return store_records(records)
''',
    "audit.py": '''"""Audit events for stored records."""
def write_audit(records):
    """Write audit evidence about records."""
    return len(records)
''',
    "views.py": '''"""Render record lists."""
def draw_records(records):
    """Display records to the operator."""
    return str(records)
''',
}
TARGET = "func:audit.py::write_audit"
QUERY = "store records"


@pytest.fixture(autouse=True)
def never_use_network(monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("No integration test may contact Jev")

    monkeypatch.setattr(decision, "_urlopen", forbidden)
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    yield
    assert not attempts


def rebuild(root: Path) -> CodebaseMemory:
    graph = build_graph(scan(root))
    generate_summaries(graph, root, MockProvider())
    memory = CodebaseMemory(graph)
    memory.save(root / ".memory" / "graph.json")
    return memory


@pytest.fixture
def project(tmp_path):
    for path, source in SOURCES.items():
        (tmp_path / path).write_text(source, encoding="utf-8")
    memory = rebuild(tmp_path)
    assert memory.graph.nodes["file:storage.py"]["content_hash"]
    assert memory.graph.has_edge("func:worker.py::process_batch", "func:storage.py::store_records")
    return tmp_path, memory


def configure(root, mode="assist", **overrides):
    values = {"mode": mode, "allow_cloud": True, **overrides}
    (root / decision.CONFIG_NAME).write_text(json.dumps(values), encoding="utf-8")


class Ranker:
    def __init__(self, *, target=TARGET, default=0.1, callback=None, mutate=None):
        self.calls = []
        self.target = target
        self.default = default
        self.callback = callback
        self.mutate = mutate

    def rank(self, query, candidates):
        self.calls.append((query, deepcopy(candidates)))
        if self.callback:
            self.callback()
        result = {
            "model": decision.DEFAULT_MODEL,
            "scores": {row["id"]: (0.99 if row["id"] == self.target else self.default)
                       for row in candidates},
            "usage": {"input_tokens": 123, "output_tokens": 9}, "elapsed_ms": 1.5,
        }
        if self.mutate:
            self.mutate(result)
        return result


def ids(selection):
    return [hit.node_id for hit in selection.hits]


def select(project, provider, **kwargs):
    root, memory = project
    return context_selection.select_context(memory, root, QUERY, top_k=1, provider=provider, **kwargs)


def test_off_preserves_local_order_without_touching_provider_or_graph(project):
    root, memory = project
    provider = Ranker()
    before = deepcopy(graph_to_json(memory.graph))
    graph_bytes = (root / ".memory/graph.json").read_bytes()
    expected = [hit.node_id for hit in memory.query_intent(QUERY, top_k=1)]
    result = select(project, provider)
    assert result.receipt["status"] == "off"
    assert ids(result) == expected
    assert not provider.calls
    assert all(row["relevance"] is None for row in result.receipt["candidates"])
    assert graph_to_json(memory.graph) == before
    assert (root / ".memory/graph.json").read_bytes() == graph_bytes


def test_assist_changes_context_order_but_never_graph_facts(project):
    root, memory = project
    configure(root)
    provider = Ranker()
    before = deepcopy(graph_to_json(memory.graph))
    graph_bytes = (root / ".memory/graph.json").read_bytes()
    assert memory.query_intent(QUERY, top_k=1)[0].node_id != TARGET
    result = select(project, provider)
    assert result.receipt["status"] == "applied"
    assert ids(result) == [TARGET]
    assert result.receipt["selected_ids"] == result.receipt["proposed_ids"] == [TARGET]
    assert len(provider.calls) == 1
    rows = result.to_dict()["results"]
    assert rows[0]["selection"]["relevance"] == 0.99
    assert rows[0]["selection"]["source_freshness"] == "current"
    assert rows[0]["start_line"] and rows[0]["path"] == "audit.py"
    assert graph_to_json(memory.graph) == before
    assert (root / ".memory/graph.json").read_bytes() == graph_bytes


def test_shadow_records_proposal_while_returning_unchanged_local_hits(project):
    root, memory = project
    configure(root, "shadow")
    result = select(project, Ranker())
    assert result.receipt["status"] == "shadow"
    assert ids(result) == result.receipt["baseline_ids"]
    assert result.receipt["proposed_ids"] == [TARGET]
    assert ids(result) != [TARGET]


@pytest.mark.parametrize("mode", ["assist", "shadow"])
def test_all_low_scores_abstain_without_a_proposed_pack(project, mode):
    root, _ = project
    configure(root, mode)
    result = select(project, Ranker(target=None, default=0.02))
    assert result.receipt["status"] == "no_match"
    assert ids(result) == result.receipt["baseline_ids"]
    assert result.receipt["proposed_ids"] == []
    assert all(row["proposed_rank"] is None for row in result.receipt["candidates"])
    assert all(row["relevance"] == 0.02 for row in result.receipt["candidates"])


def test_allow_remote_false_prevents_even_a_ready_provider(project):
    root, _ = project
    configure(root)
    provider = Ranker()
    result = select(project, provider, allow_remote=False)
    assert result.receipt["status"] == "local"
    assert not provider.calls
    assert ids(result) == result.receipt["baseline_ids"]


@pytest.mark.parametrize("configuration", [
    {"mode": "assist", "allow_cloud": False},
    {"mode": "shadow", "allow_cloud": False},
    {"mode": "assist", "allow_cloud": True, "model": "jev-latest"},
])
def test_permission_or_config_failure_retains_local_evidence(project, configuration):
    root, _ = project
    (root / decision.CONFIG_NAME).write_text(json.dumps(configuration), encoding="utf-8")
    provider = Ranker()
    result = select(project, provider)
    assert result.receipt["status"] == "fallback"
    assert ids(result) == result.receipt["baseline_ids"]
    assert not provider.calls


def test_missing_key_cannot_replay_cached_cloud_result(project, monkeypatch):
    root, _ = project
    configure(root)
    provider = Ranker()
    first = select(project, provider)
    assert first.receipt["status"] == "applied"
    monkeypatch.delenv("TYPESAFE_API_KEY")
    second = select(project, provider)
    assert second.receipt["status"] == "fallback" and second.receipt["cache_hit"] is False
    assert ids(second) == second.receipt["baseline_ids"] and len(provider.calls) == 1


def test_stale_source_is_detected_before_remote_processing(project):
    root, _ = project
    configure(root)
    (root / "audit.py").write_text(SOURCES["audit.py"] + "# changed\n", encoding="utf-8")
    provider = Ranker()
    result = select(project, provider)
    assert result.receipt["status"] == "fallback"
    assert not provider.calls
    assert ids(result) == result.receipt["baseline_ids"]
    audit = [row for row in result.receipt["candidates"] if row["path"] == "audit.py"]
    assert audit and all(row["source_freshness"] == "stale" for row in audit)


def test_source_change_during_inference_discards_selection_and_updates_freshness(project):
    root, _ = project
    configure(root)

    def edit_during_call():
        (root / "audit.py").write_text(SOURCES["audit.py"] + "# changed during rank\n", encoding="utf-8")

    result = select(project, Ranker(callback=edit_during_call))
    assert result.receipt["status"] == "fallback"
    assert ids(result) == result.receipt["baseline_ids"]
    audit = [row for row in result.receipt["candidates"] if row["path"] == "audit.py"]
    assert audit and all(row["source_freshness"] == "stale" for row in audit)
    assert all(row["selection"]["source_freshness"] != "current"
               for row in result.results() if row["path"] == "audit.py")


def test_graph_change_during_inference_discards_selection(project):
    root, _ = project
    configure(root)
    graph_file = root / ".memory/graph.json"

    def change_saved_graph():
        graph_file.write_text(graph_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    result = select(project, Ranker(callback=change_saved_graph))
    assert result.receipt["status"] == "fallback"
    assert ids(result) == result.receipt["baseline_ids"]


def test_config_revoked_during_inference_cannot_apply_old_permission(project):
    root, _ = project
    configure(root)
    result = select(project, Ranker(callback=lambda: configure(root, "off", allow_cloud=False)))
    assert result.receipt["status"] == "fallback"
    assert ids(result) == result.receipt["baseline_ids"]


def test_config_corrupted_during_inference_discards_already_proposed_selection(project):
    root, _ = project
    configure(root)

    def corrupt_settings():
        (root / decision.CONFIG_NAME).write_text("{broken", encoding="utf-8")

    result = select(project, Ranker(callback=corrupt_settings))
    assert result.receipt["status"] == "fallback"
    assert ids(result) == result.receipt["baseline_ids"]


@pytest.mark.parametrize("mutation", [
    lambda r: r["scores"].pop(next(iter(r["scores"]))),
    lambda r: r["scores"].update(invented=0.99),
    lambda r: r["scores"].update({next(iter(r["scores"])): float("nan")}),
    lambda r: r["scores"].update({next(iter(r["scores"])): True}),
    lambda r: r.update(model="jev-latest"),
    lambda r: r.update(usage={"input_tokens": 100}),
])
def test_invalid_provider_reply_never_partly_ranks_candidates(project, mutation):
    root, _ = project
    configure(root)
    result = select(project, Ranker(mutate=mutation))
    assert result.receipt["status"] == "fallback"
    assert ids(result) == result.receipt["baseline_ids"]
    assert result.receipt["proposed_ids"] == []
    assert all(row["relevance"] is None for row in result.receipt["candidates"])
    assert not (root / ".memory/context/cache.json").exists()


def test_cached_replay_does_not_charge_usage_and_query_change_recomputes(project):
    root, memory = project
    configure(root)
    provider = Ranker()
    first = select(project, provider)
    second = select(project, provider)
    assert first.receipt["cache_hit"] is False and second.receipt["cache_hit"] is True
    assert second.receipt["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert second.receipt["cached_usage"] == first.receipt["usage"]
    assert len(provider.calls) == 1 and ids(first) == ids(second)
    third = context_selection.select_context(memory, root, QUERY + " audit", top_k=1, provider=provider)
    assert third.receipt["cache_hit"] is False and len(provider.calls) == 2


def test_refresh_after_source_change_invalidates_previous_cache(project):
    root, _ = project
    configure(root)
    provider = Ranker()
    before = select(project, provider)
    (root / "audit.py").write_text(SOURCES["audit.py"] + "# refreshed evidence\n", encoding="utf-8")
    updated = rebuild(root)
    after = select((root, updated), provider)
    assert after.receipt["status"] == "applied"
    assert after.receipt["cache_hit"] is False and len(provider.calls) == 2
    assert after.receipt["graph_fingerprint"] != before.receipt["graph_fingerprint"]


def test_explicit_paths_are_kept_even_when_they_exceed_top_k(project):
    root, memory = project
    configure(root)
    result = context_selection.select_context(memory, root, "store records in audit.py and views.py",
                                              top_k=1, provider=Ranker(target=None, default=0.8))
    assert result.receipt["status"] == "applied"
    assert set(ids(result)) == {"file:audit.py", "file:views.py"}
    selected = [row for row in result.receipt["candidates"] if row["selected"]]
    assert len(selected) == 2 and all(row["mandatory"] for row in selected)


def test_backticked_symbol_cannot_be_voted_out(project):
    root, memory = project
    configure(root)
    result = context_selection.select_context(memory, root, "store records with `write_audit`",
                                              top_k=1, provider=Ranker(target="file:storage.py"))
    assert ids(result) == [TARGET]
    assert next(row for row in result.receipt["candidates"] if row["id"] == TARGET)["mandatory"]


def test_required_context_larger_than_candidate_limit_stays_local(project):
    root, memory = project
    configure(root, max_candidates=1)
    provider = Ranker()
    result = context_selection.select_context(memory, root, "audit.py and views.py", top_k=1, provider=provider)
    assert result.receipt["status"] == "fallback"
    assert set(ids(result)) == {"file:audit.py", "file:views.py"} and not provider.calls


def test_history_round_trip_shares_full_receipt_and_record_false_does_not_write(project):
    root, _ = project
    configure(root)
    result = select(project, Ranker())
    assert context_selection.get_receipt(root, result.receipt["id"]) == result.receipt
    assert context_selection.history(root)["items"][0] == result.receipt
    path = root / ".memory/context/history.json"
    before = path.read_bytes()
    next_result = select(project, Ranker(), record=False)
    assert path.read_bytes() == before
    assert context_selection.get_receipt(root, next_result.receipt["id"]) is None
    with pytest.raises(ValueError):
        context_selection.get_receipt(root, "../graph.json")


@pytest.mark.parametrize("contents", ['{broken', '{"items":[null]}'])
def test_corrupt_history_is_preserved_and_reported_without_losing_context(project, contents):
    root, _ = project
    path = root / ".memory/context/history.json"
    path.parent.mkdir(parents=True)
    path.write_text(contents, encoding="utf-8")
    result = select(project, Ranker())
    assert result.hits and "persistence_warning" in result.receipt
    assert path.read_text(encoding="utf-8") == contents
    with pytest.raises(ValueError):
        context_selection.history(root)


def test_corrupt_cache_is_preserved_and_falls_back_without_a_paid_request(project):
    root, _ = project
    configure(root)
    path = root / ".memory/context/cache.json"
    path.parent.mkdir(parents=True)
    contents = "{broken cache"
    path.write_text(contents, encoding="utf-8")
    provider = Ranker()
    result = select(project, provider)
    assert result.receipt["status"] == "fallback" and not provider.calls
    assert path.read_text(encoding="utf-8") == contents


def test_empty_shortlist_never_contacts_provider(project):
    root, memory = project
    configure(root)
    provider = Ranker()
    result = context_selection.select_context(memory, root, "zyxwvuunfindableterm", provider=provider)
    assert result.receipt["status"] == "no_match"
    assert result.hits == [] and result.receipt["proposed_ids"] == []
    assert not provider.calls


def test_local_decisions_stay_in_context_without_cloud_disclosure(project):
    root, memory = project
    configure(root)
    memory.graph.add_node("decision:private", type="decision", path="",
                          name="store_records_privatepolicy", summary="PRIVATE LOCAL DECISION")
    provider = Ranker()
    result = context_selection.select_context(memory, root, QUERY + " privatepolicy",
                                              top_k=3, provider=provider)
    assert result.receipt["status"] == "applied"
    assert "decision:private" in ids(result)
    sent = provider.calls[0][1]
    assert all(row["id"] != "decision:private" for row in sent)
    assert "PRIVATE LOCAL DECISION" not in json.dumps(sent)
    local = next(row for row in result.receipt["candidates"] if row["id"] == "decision:private")
    assert local["model_eligible"] is False and local["relevance"] is None


def test_concurrent_request_has_bounded_wait_and_keeps_local_results(project):
    root, _ = project
    configure(root, timeout_seconds=0.1)
    entered, release = threading.Event(), threading.Event()

    def hold_first_request():
        entered.set()
        assert release.wait(3)

    provider = Ranker(callback=hold_first_request)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(select, project, provider, record=False)
        assert entered.wait(1)
        try:
            started = time.monotonic()
            second = select(project, provider, record=False)
            assert time.monotonic() - started < 1.5
            assert second.receipt["status"] == "fallback"
            assert ids(second) == second.receipt["baseline_ids"]
            assert len(provider.calls) == 1
        finally:
            release.set()
        assert first.result(timeout=2).receipt["status"] == "applied"


@pytest.mark.parametrize("policy", ["scope", "cmsignore", "gitignore"])
def test_new_exclusions_prevent_egress_from_an_existing_graph(project, policy):
    root, _ = project
    configure(root)
    if policy == "scope":
        save_scope(root, ["storage.py"])
    else:
        (root / (".cmsignore" if policy == "cmsignore" else ".gitignore")).write_text(
            "audit.py\n", encoding="utf-8")
    provider = Ranker(default=0.8)
    result = select(project, provider)
    assert result.receipt["status"] == "applied"
    paths = {row["path"] for row in provider.calls[0][1]}
    assert "audit.py" not in paths
    if policy == "scope":
        assert paths == {"storage.py"}
    audit = [row for row in result.receipt["candidates"] if row["path"] == "audit.py"]
    assert audit and all(row["model_eligible"] is False for row in audit)
    assert all("processing scope or exclusions" in row["reason"] for row in audit)


@pytest.mark.parametrize("rule", ["nested", "pruned-parent", "override"])
def test_nested_ignore_and_pruned_parent_match_scanner_semantics(project, rule):
    root, _ = project
    (root / "private").mkdir()
    (root / "private/audit.py").write_text(SOURCES["audit.py"], encoding="utf-8")
    memory = rebuild(root)
    configure(root)
    if rule == "nested":
        (root / "private/.gitignore").write_text("/audit.py\n", encoding="utf-8")
    elif rule == "pruned-parent":
        (root / ".gitignore").write_text("private/\n!private/audit.py\n", encoding="utf-8")
    else:
        (root / "private/.gitignore").write_text("/audit.py\n", encoding="utf-8")
        (root / ".cmsignore").write_text("!private/audit.py\n", encoding="utf-8")
    provider = Ranker(default=0.8)
    result = select((root, memory), provider)
    assert result.receipt["status"] == "applied"
    sent = {row["path"] for row in provider.calls[0][1]}
    scanned = {row.rel_path for row in scan(root)}
    assert ("private/audit.py" in sent) == ("private/audit.py" in scanned) == (rule == "override")


@pytest.mark.parametrize("contents", ['{broken', '[]', '{}', '{"include":"storage.py"}',
                                      '{"include":[3]}', '{"include":[],"include":["storage.py"]}'])
def test_malformed_scope_fails_closed_instead_of_defaulting_to_all(project, contents):
    root, _ = project
    configure(root)
    (root / ".cmsscope.json").write_text(contents, encoding="utf-8")
    provider = Ranker()
    result = select(project, provider)
    assert result.receipt["status"] == "fallback" and not provider.calls
    assert ids(result) == result.receipt["baseline_ids"]


def test_policy_change_before_provider_call_is_checked_under_lock(project, monkeypatch):
    root, _ = project
    configure(root)
    real_snapshot = context_selection._policy_snapshot
    snapshots = []

    def change_after_first_snapshot(*args):
        value = real_snapshot(*args)
        snapshots.append(value)
        if len(snapshots) == 1:
            (root / ".cmsignore").write_text("audit.py\n", encoding="utf-8")
        return value

    monkeypatch.setattr(context_selection, "_policy_snapshot", change_after_first_snapshot)
    provider = Ranker()
    result = select(project, provider)
    assert result.receipt["status"] == "fallback" and not provider.calls
    assert len(snapshots) >= 2


def test_policy_change_during_provider_call_prevents_applying_proposal(project):
    root, _ = project
    configure(root)
    provider = Ranker(callback=lambda: (root / ".cmsignore").write_text("audit.py\n", encoding="utf-8"))
    result = select(project, provider)
    assert result.receipt["status"] == "fallback"
    assert ids(result) == result.receipt["baseline_ids"]
    assert "exclusions changed" in result.receipt["reason"]


def test_policy_change_invalidates_cache_and_rebuilds_egress_candidates(project):
    root, _ = project
    configure(root)
    provider = Ranker(default=0.8)
    first = select(project, provider)
    (root / ".cmsignore").write_text("audit.py\n", encoding="utf-8")
    second = select(project, provider)
    assert second.receipt["status"] == "applied" and second.receipt["cache_hit"] is False
    assert len(provider.calls) == 2
    assert all(row["path"] != "audit.py" for row in provider.calls[-1][1])
    assert first.receipt["processing_policy_fingerprint"] != second.receipt["processing_policy_fingerprint"]


def feature_project(root):
    files = {
        "entry.py": 'from helper import step\n# @memory:feature:Workflow\ndef run():\n    step()\n',
        "helper.py": 'from hidden import finish\ndef step():\n    finish()\n',
        "hidden.py": 'def finish():\n    return "private dependency"\n',
    }
    for path, source in files.items():
        (root / path).write_text(source, encoding="utf-8")
    memory = rebuild(root)
    build_features(memory.graph, MockProvider(), discover=False)
    memory.save(root / ".memory/graph.json")
    configure(root)
    return memory


def test_feature_summary_is_local_if_any_transitive_source_is_excluded(tmp_path):
    memory = feature_project(tmp_path)
    (tmp_path / ".cmsignore").write_text("hidden.py\n", encoding="utf-8")
    provider = Ranker(target="feature:Workflow", default=0.8)
    result = context_selection.select_context(memory, tmp_path, "Workflow", provider=provider)
    feature = next(row for row in result.receipt["candidates"] if row["id"] == "feature:Workflow")
    assert feature["model_eligible"] is False
    assert all(row["id"] != "feature:Workflow" for _, payload in provider.calls for row in payload)
    assert all(row["path"] != "hidden.py" for _, payload in provider.calls for row in payload)


def test_feature_dependency_edit_cannot_appear_current(tmp_path):
    memory = feature_project(tmp_path)
    (tmp_path / "hidden.py").write_text('def finish():\n    return "changed"\n', encoding="utf-8")
    provider = Ranker(target="feature:Workflow")
    result = context_selection.select_context(memory, tmp_path, "Workflow", provider=provider)
    feature = next(row for row in result.receipt["candidates"] if row["id"] == "feature:Workflow")
    assert feature["source_freshness"] == "stale"
    assert result.receipt["status"] == "fallback" and not provider.calls


@pytest.mark.parametrize("change", ["updated-source-index", "members", "membership-edge", "description"])
def test_retained_feature_narrative_must_match_current_graph_context(tmp_path, change):
    memory = feature_project(tmp_path)
    feature_id = "feature:Workflow"
    attrs = memory.graph.nodes[feature_id]
    assert attrs["narrative_context_hash"] and attrs["summary"]
    if change == "updated-source-index":
        old_feature = deepcopy(attrs)
        old_edges = list(memory.graph.in_edges(feature_id, data=True))
        (tmp_path / "hidden.py").write_text('def finish():\n    return "new behavior"\n', encoding="utf-8")
        memory = rebuild(tmp_path)
        memory.graph.add_node(feature_id, **old_feature)
        for source, target, edge in old_edges:
            memory.graph.add_edge(source, target, **edge)
    elif change == "members":
        attrs["members"].append("func:helper.py::step")
        memory.graph.add_edge("func:helper.py::step", feature_id, type="PART_OF")
    elif change == "membership-edge":
        memory.graph.add_edge("func:helper.py::step", feature_id, type="PART_OF")
    else:
        attrs["description"] = "The feature's intended behavior changed."
    provider = Ranker(target=feature_id)
    result = context_selection.select_context(memory, tmp_path, "Workflow", provider=provider)
    feature = next(row for row in result.receipt["candidates"] if row["id"] == feature_id)
    assert feature["source_freshness"] == "stale"
    assert result.receipt["status"] == "fallback" and not provider.calls


@pytest.mark.parametrize("stamp", [None, "", "not-a-hash", 123])
def test_feature_summary_without_valid_narrative_stamp_is_unknown(tmp_path, stamp):
    memory = feature_project(tmp_path)
    memory.graph.nodes["feature:Workflow"]["narrative_context_hash"] = stamp
    provider = Ranker(target="feature:Workflow")
    result = context_selection.select_context(memory, tmp_path, "Workflow", provider=provider)
    feature = next(row for row in result.receipt["candidates"] if row["id"] == "feature:Workflow")
    assert feature["source_freshness"] == "unknown"
    assert result.receipt["status"] == "fallback" and not provider.calls


def test_feature_summary_with_current_narrative_stamp_can_be_ranked(tmp_path):
    memory = feature_project(tmp_path)
    provider = Ranker(target="feature:Workflow")
    result = context_selection.select_context(memory, tmp_path, "Workflow", top_k=1, provider=provider)
    feature = next(row for row in result.receipt["candidates"] if row["id"] == "feature:Workflow")
    assert feature["source_freshness"] == "current"
    assert result.receipt["status"] == "applied" and len(provider.calls) == 1
    assert ids(result) == ["feature:Workflow"]


def test_component_summary_requires_containing_file_provenance(project):
    root, memory = project
    configure(root)
    assert memory.graph.nodes[TARGET]["summary"]
    memory.graph.nodes["file:audit.py"].pop("summary_meta")
    provider = Ranker()
    result = select(project, provider)
    assert result.receipt["status"] == "fallback" and not provider.calls
    row = next(row for row in result.receipt["candidates"] if row["id"] == TARGET)
    assert row["source_freshness"] == "unknown"


def test_fully_excluded_shortlist_explains_why_it_stays_local(project):
    root, _ = project
    configure(root)
    (root / ".cmsignore").write_text("*.py\n", encoding="utf-8")
    provider = Ranker()
    result = select(project, provider)
    assert result.receipt["status"] == "local" and not provider.calls
    assert "permitted for cloud processing" in result.receipt["reason"]
    assert all("Kept local: current processing scope or exclusions" in row["reason"]
               for row in result.receipt["candidates"])


@pytest.mark.parametrize("query,top_k", [("x" * 4001, 1), ("", 1), ("valid", True), ("valid", 51)],
                         ids=["query-budget", "empty-query", "boolean-limit", "candidate-budget"])
def test_public_input_bounds_precede_selection(project, query, top_k):
    root, memory = project
    provider = Ranker()
    with pytest.raises(ValueError):
        context_selection.select_context(memory, root, query, top_k=top_k, provider=provider)
    assert not provider.calls
