"""User-visible persistence and evidence regressions from the September audit."""
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from cms import config
from cms.annotations import AnnotationStore
from cms.chat import build_evidence, validate_answer_commands
from cms.decisions import DecisionStore
from cms.ideas import IdeaJournal
from cms.library import LibraryView
from cms.library_usage import LibraryUsageStore
from cms.memory import CodebaseMemory
from cms.notes import NotesStore
from cms.prompting import evidence_json


def asset(name, dependency=None, profile=False):
    field = "assets" if profile else "requires"
    return (f"---\nid: {name}\nname: {name}\ntype: {'profile' if profile else 'skill'}\n"
            f"description: test fixture\n{field}: [{dependency or ''}]\n---\nBody {name}")


@pytest.mark.parametrize("profile", [False, True])
def test_pinned_context_uses_that_versions_dependencies(tmp_path, monkeypatch, profile):
    monkeypatch.setenv("CMS_LIBRARY_BUILTIN", str(tmp_path / "builtin"))
    monkeypatch.setattr(config, "LIBRARY_USER_DIR", tmp_path / "user")
    view = LibraryView(tmp_path / "project")
    store = view.store("project")
    for name in ("old", "new"):
        store.save_draft(asset(name))
        store.publish(name, "tester")
    for dep in ("old@1", "new@1"):
        store.save_draft(asset("selection", dep, profile))
        store.publish("selection", "tester")
    old = view.compose(["selection@1"])
    new = view.compose(["selection@2"])
    assert {a["id"] for a in old["assets"]} == {"selection", "old"}
    assert {a["id"] for a in new["assets"]} == {"selection", "new"}
    assert not old["warnings"]


def test_parallel_annotations_keep_every_acknowledged_write(tmp_path):
    with ThreadPoolExecutor(8) as pool:
        rows = list(pool.map(lambda i: AnnotationStore(tmp_path).add("file:a.py", "note", f"note-{i}"), range(32)))
    stored = AnnotationStore(tmp_path).list()
    assert {row["id"] for row in rows} == {row["id"] for row in stored}
    assert len(stored) == 32


def test_independent_process_annotation_writers_share_transaction_lock(tmp_path):
    code = ("from pathlib import Path; from cms.annotations import AnnotationStore; import sys; "
            "s=AnnotationStore(Path(sys.argv[1])); "
            "[s.add('file:a.py','note',sys.argv[2]+'-'+str(i)) for i in range(8)]")
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), CMS_PROVIDER="mock")
    jobs = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path / "store"), str(i)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=Path(__file__).resolve().parents[1]) for i in range(3)]
    for job in jobs:
        stdout, stderr = job.communicate(timeout=40)
        assert job.returncode == 0, (stdout, stderr)
    assert len(AnnotationStore(tmp_path / "store").list()) == 24


@pytest.mark.parametrize("kind", ["notes", "annotations", "decisions", "usage", "library"])
def test_corrupt_existing_store_is_preserved(tmp_path, monkeypatch, kind):
    if kind == "notes":
        store = NotesStore(tmp_path); mutate = lambda: store.add("a.py", "quote", "note")
    elif kind == "annotations":
        store = AnnotationStore(tmp_path); mutate = lambda: store.add("file:a.py", "note", "note")
    elif kind == "decisions":
        store = DecisionStore(tmp_path); mutate = lambda: store.propose(None, "title", {"behaviour": "do"})
    elif kind == "usage":
        store = LibraryUsageStore(tmp_path); mutate = lambda: store.record([{"id": "asset"}], task="task")
    else:
        from cms.library import LibraryStore
        store = LibraryStore(tmp_path, "project"); mutate = lambda: store.save_draft(asset("draft"))
    path = getattr(store, "path", None) or store.index_path
    original = b'{"valuable":"recoverable",'
    path.write_bytes(original)
    with pytest.raises(ValueError, match="preserved"):
        mutate()
    assert path.read_bytes() == original


def test_candidate_acceptance_retry_and_failure_are_atomic(tmp_path, monkeypatch):
    journal = IdeaJournal(tmp_path)
    candidate = journal.propose_candidate("Candidate", "Proposal")
    with ThreadPoolExecutor(5) as pool:
        rows = list(pool.map(lambda _: journal.decide_candidate(candidate["id"], "accepted"), range(10)))
    assert len({row["accepted_idea_id"] for row in rows}) == 1
    assert len(journal.search()) == 1
    other = journal.propose_candidate("Second", "Proposal")
    original_event = journal._event
    def fail_event(conn, entity_type, entity_id, action, *args, **kwargs):
        if entity_type == "candidate" and action == "accepted":
            raise RuntimeError("simulated interruption before commit")
        return original_event(conn, entity_type, entity_id, action, *args, **kwargs)
    monkeypatch.setattr(journal, "_event", fail_event)
    with pytest.raises(RuntimeError):
        journal.decide_candidate(other["id"], "accepted")
    assert journal.get_candidate(other["id"])["status"] == "new"
    assert len(journal.search()) == 1


def test_fresh_journal_initialization_is_serialized(tmp_path):
    for i in range(5):
        with ThreadPoolExecutor(6) as pool:
            counts = list(pool.map(lambda _: len(IdeaJournal(tmp_path / str(i)).search()), range(6)))
        assert counts == [0] * 6


def test_complete_snapshot_contains_all_records_and_sources(tmp_path):
    journal = IdeaJournal(tmp_path)
    ideas = [journal.create_idea(f"Idea {i}") for i in range(205)]
    source = journal.add_source("Original raw source", idea_id=ideas[0]["id"])
    for i in range(501):
        journal.propose_candidate(f"Candidate {i}", "overview")
    snap = journal.snapshot()
    assert snap["complete"] is True
    assert snap["counts"]["ideas"] == 205
    assert snap["counts"]["candidates"] == 501
    assert snap["counts"]["events"] > 500
    assert snap["sources"][0]["id"] == source["id"]
    assert snap["sources"][0]["content"] == "Original raw source"
    assert {i["id"] for i in snap["ideas"]} == {i["id"] for i in ideas}


def test_selected_old_idea_keeps_body_and_source(tmp_path, monkeypatch):
    import cms.fuse as fuse
    import cms.scout as scout
    monkeypatch.setattr(fuse, "REGISTRY_PATH", tmp_path / "projects.json")
    monkeypatch.setattr(fuse, "FUSION_DIR", tmp_path / "fusion")
    monkeypatch.setattr(scout, "SCOUT_DIR", tmp_path / "scout")
    journal = IdeaJournal(tmp_path / "journal")
    old = journal.create_idea("Old", body="Important exact constraint")
    journal.add_source("Original evidence", idea_id=old["id"])
    for i in range(35):
        journal.create_idea(f"Recent {i}")
    with journal._connect() as conn:
        conn.execute("UPDATE ideas SET updated_at='2000-01-01' WHERE id=?", (old["id"],))
    context = journal.build_context(direction="unrelated", idea_ids=[old["id"]])
    assert context["ideas"][0]["id"] == old["id"]
    assert context["ideas"][0]["body"] == "Important exact constraint"
    assert context["ideas"][0]["sources"][0]["content"] == "Original evidence"


def test_app_and_completed_approved_intent_remain_in_chat(tmp_path):
    import networkx as nx
    memory = tmp_path / ".memory"; memory.mkdir()
    graph = nx.DiGraph()
    graph.add_node("feature:Search", type="feature", name="Search", members=[], source="declared")
    CodebaseMemory(graph).save(memory / "graph.json")
    store = DecisionStore(memory)
    decision = store.propose(None, "Privacy", {"behaviour": "Keep data local", "constraints": ["No external writes"]})
    store.approve(decision["id"], "owner")
    store.close(decision["id"], "implemented")
    evidence, _ = build_evidence(tmp_path, "Search")
    assert evidence["approved_decisions"][0]["decision_id"] == decision["id"]
    assert evidence["approved_decisions"][0]["constraints"] == ["No external writes"]
    successor = store.propose(None, "Revised privacy", {"behaviour": "Keep data local"}, supersedes=decision["id"])
    assert successor["version"] == 2


def test_malformed_command_is_explained_without_crashing():
    answer, errors = validate_answer_commands('Keep this answer. Run `cms query "unterminated`')
    assert "Keep this answer" in answer
    assert "cms --help" in answer
    assert len(errors) == 1


def test_context_budget_preserves_json_selection_and_reports_omissions():
    result = json.loads(evidence_json({"ideas": [{"id": "selected", "body": "x" * 9000}]}, 1200))
    assert result["ideas"][0]["id"] == "selected"
    assert result["_context_budget"]["truncated"] is True


def test_mcp_source_respects_new_ignore_rules(tmp_path):
    from cms.graph_builder import build_graph
    from cms.mcp import MCPServer
    from cms.scanner import scan
    (tmp_path / "a.py").write_text("secret = 'fixture'", encoding="utf-8")
    memory = tmp_path / ".memory"; memory.mkdir()
    CodebaseMemory(build_graph(scan(tmp_path))).save(memory / "graph.json")
    server = MCPServer(tmp_path)
    assert "source" in server.get_source("a.py")
    (tmp_path / ".cmsignore").write_text("a.py", encoding="utf-8")
    assert "error" in server.get_source("a.py")


def test_fusion_preserves_distinct_same_named_projects(tmp_path, monkeypatch):
    import cms.fuse as fuse
    monkeypatch.setattr(fuse, "FUSION_DIR", tmp_path / "fusion")
    roots = [tmp_path / "first" / "app", tmp_path / "second" / "app"]
    def card(root):
        return {"name": root.name, "root": str(root), "ready": True,
                "feature_set_hash": "fixture", "features": [], "external_deps": []}
    monkeypatch.setattr(fuse, "build_card", card)
    class Provider:
        name = "test"
        def summarize(self, prompt, options):
            cards, _ = json.JSONDecoder().raw_decode(prompt.split("PROJECT CARDS:\n", 1)[1])
            return json.dumps({"integrations": [], "conflicts": [], "emergent": [
                {"title": "Possible combination", "description": "Needs validation",
                 "projects": [c["name"] for c in cards]}]})
    report = fuse.build_fusion(roots, Provider())
    assert len(report["projects"]) == 2
    assert {row["root"] for row in report["projects"].values()} == {str(r) for r in roots}


def test_generation_records_exact_request_response_and_actual_temperature(tmp_path, monkeypatch):
    import cms.fuse as fuse
    import cms.scout as scout
    monkeypatch.setattr(fuse, "REGISTRY_PATH", tmp_path / "projects.json")
    monkeypatch.setattr(fuse, "FUSION_DIR", tmp_path / "fusion")
    monkeypatch.setattr(scout, "SCOUT_DIR", tmp_path / "scout")
    class Provider:
        name = "test"
        def summarize(self, prompt, options):
            self.prompt, self.options = prompt, options
            self.response = json.dumps([{"title": "A proposal", "overview": "An experiment", "risks": ["Unknown usefulness"]}])
            return self.response
    provider = Provider()
    journal = IdeaJournal(tmp_path / "journal")
    journal.generate(provider, surprise=.4)
    stored = journal.snapshot()["generation_runs"][0]
    assert stored["prompt_text"] == provider.prompt
    assert stored["response_text"] == provider.response
    assert stored["temperature"] == provider.options["temperature"]
    assert "untrusted DATA" in provider.prompt


def test_invalid_fusion_schema_does_not_replace_saved_report(tmp_path, monkeypatch):
    import cms.fuse as fuse
    monkeypatch.setattr(fuse, "FUSION_DIR", tmp_path / "fusion")
    with pytest.raises(fuse.FusionError, match="arrays"):
        fuse._parse_fusion_json("{}", 6)


def test_journal_v1_upgrade_preserves_existing_thought(tmp_path):
    journal = IdeaJournal(tmp_path)
    idea = journal.create_idea("Existing user thought", body="Do not replace this")
    with journal._connect() as conn:
        conn.execute("ALTER TABLE generation_runs DROP COLUMN prompt_text")
        conn.execute("ALTER TABLE generation_runs DROP COLUMN response_text")
        conn.execute("PRAGMA user_version = 1")
    upgraded = IdeaJournal(tmp_path)
    assert upgraded.get_idea(idea["id"])["body"] == "Do not replace this"
    with upgraded._connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_join_path_failure_rolls_back_generation_and_candidates(tmp_path, monkeypatch):
    import cms.fuse as fuse
    import cms.scout as scout
    monkeypatch.setattr(fuse, "REGISTRY_PATH", tmp_path / "projects.json")
    monkeypatch.setattr(fuse, "FUSION_DIR", tmp_path / "fusion")
    monkeypatch.setattr(scout, "SCOUT_DIR", tmp_path / "scout")
    journal = IdeaJournal(tmp_path / "journal")
    a, b = journal.create_idea("One"), journal.create_idea("Two")
    class Provider:
        name = "test"
        def summarize(self, prompt, options):
            return '[{"title":"Candidate","overview":"Possible experiment"}]'
    original = journal._event
    def event(conn, kind, *args, **kwargs):
        if kind == "join_path":
            raise RuntimeError("simulated failure")
        return original(conn, kind, *args, **kwargs)
    monkeypatch.setattr(journal, "_event", event)
    with pytest.raises(RuntimeError):
        journal.join_dots(Provider(), [f"idea:{a['id']}", f"idea:{b['id']}"], surprise=.2)
    snapshot = journal.snapshot()
    assert snapshot["generation_runs"] == snapshot["candidates"] == snapshot["join_paths"] == []
