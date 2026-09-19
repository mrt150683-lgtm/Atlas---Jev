"""Regression journeys for scan boundaries, persisted freshness and recovery."""
import json
import os
import zipfile
from pathlib import Path

from cms.bundle import export_bundle
from cms.graph_builder import build_graph
from cms.memory import CodebaseMemory
from cms.providers import MockProvider
from cms.scanner import scan
from cms.summarizer import generate_summaries
from cms.update import incremental_update


def write(root, rel, text):
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def sync(root, provider=None):
    stats = incremental_update(root, provider or MockProvider(), echo=lambda *a: None)
    return stats, CodebaseMemory.load(root / ".memory/graph.json").graph


def test_nested_ignore_prevents_prompt_and_bundle_submission(tmp_path):
    write(tmp_path, "src/.gitignore", "/private.json\n*.generated.py\n!keep.generated.py\n")
    write(tmp_path, "src/private.json", '{"secret": "synthetic-private-marker"}')
    write(tmp_path, "src/x.generated.py", "pass\n")
    write(tmp_path, "src/keep.generated.py", "pass\n")
    write(tmp_path, "src/deep/private.json", "{}")
    write(tmp_path, "main.py", "pass\n")
    records = scan(tmp_path)
    assert {r.rel_path for r in records} == {"main.py", "src/keep.generated.py", "src/deep/private.json"}

    class Capture(MockProvider):
        def __init__(self): self.prompts = []
        def summarize(self, prompt, context):
            self.prompts.append(prompt)
            return super().summarize(prompt, context)
    provider = Capture()
    generate_summaries(build_graph(records), tmp_path, provider)
    assert not any("synthetic-private-marker" in p for p in provider.prompts)
    sync(tmp_path)
    bundle = export_bundle(tmp_path, tmp_path / "share.cmsbundle", include_source=True)
    with zipfile.ZipFile(bundle) as archive:
        assert "source/src/private.json" not in archive.namelist()
        assert "source/src/keep.generated.py" in archive.namelist()


def test_root_atlas_override_can_override_nested_git_rule(tmp_path):
    write(tmp_path, "src/.gitignore", "private.json\n")
    write(tmp_path, "src/private.json", "{}")
    write(tmp_path, ".cmsignore", "!src/private.json\n")
    assert {r.rel_path for r in scan(tmp_path)} == {"src/private.json"}


def test_same_mtime_same_size_edit_refreshes_summary_and_untouched_content_skips(tmp_path):
    target = write(tmp_path, "main.py", "def old_name():\n    pass\n")
    sync(tmp_path)
    stamp = target.stat().st_mtime
    target.write_text("def new_name():\n    pass\n", encoding="utf-8")
    os.utime(target, (stamp, stamp))
    stats, graph = sync(tmp_path)
    assert stats.changed == ["main.py"] and stats.summarized == 1
    assert "new_name" in graph.nodes["file:main.py"]["summary"]
    assert "old_name" not in graph.nodes["file:main.py"]["summary"]
    os.utime(target, (stamp + 100, stamp + 100))
    stats, _ = sync(tmp_path)
    assert stats.changed == [] and stats.summarized == 0


def test_deleted_members_and_exports_are_removed_on_readback_and_bundle(tmp_path):
    write(tmp_path, "keep.py", "# @memory:feature:Shared\ndef keep():\n    pass\n")
    write(tmp_path, "gone.py", "# @memory:feature:Shared\ndef gone():\n    pass\n")
    write(tmp_path, "obsolete.py", "# @memory:feature:Obsolete\ndef obsolete():\n    pass\n")
    _, before = sync(tmp_path)
    before.nodes["feature:Shared"]["exercised_by"] = ["old-unversioned-test"]
    before.nodes["feature:Shared"]["review"] = {"verdict": "aligned"}
    CodebaseMemory(before).save(tmp_path / ".memory/graph.json")
    write(tmp_path, ".memory/summaries/personal-note.md", "My manually written note.\n")
    (tmp_path / "gone.py").unlink()
    (tmp_path / "obsolete.py").unlink()
    stats, after = sync(tmp_path)
    shared = after.nodes["feature:Shared"]
    assert set(stats.changed) == {"gone.py", "obsolete.py"}
    assert shared["members"] == ["func:keep.py::keep"]
    assert "gone.py" not in shared["summary"]
    assert not shared.get("exercised_by") and not shared.get("review")
    assert not (tmp_path / ".memory/summaries/gone.py.md").exists()
    assert not (tmp_path / ".memory/features/Obsolete.md").exists()
    assert (tmp_path / ".memory/summaries/personal-note.md").read_text() == "My manually written note.\n"
    bundle = export_bundle(tmp_path, tmp_path / "share.cmsbundle")
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        assert ".memory/summaries/gone.py.md" not in names
        assert ".memory/features/Obsolete.md" not in names
        assert ".memory/summaries/personal-note.md" not in names


def test_edited_obsolete_generated_copy_is_preserved_locally_not_shared(tmp_path):
    write(tmp_path, "old.py", "pass\n")
    sync(tmp_path)
    saved = tmp_path / ".memory/summaries/old.py.md"
    saved.write_text(saved.read_text(encoding="utf-8") + "My annotation\n", encoding="utf-8")
    (tmp_path / "old.py").unlink()
    sync(tmp_path)
    assert saved.exists() and "My annotation" in saved.read_text(encoding="utf-8")
    with zipfile.ZipFile(export_bundle(tmp_path, tmp_path / "share.cmsbundle")) as archive:
        assert ".memory/summaries/old.py.md" not in archive.namelist()


def test_new_member_invalidates_existing_feature_narrative(tmp_path):
    write(tmp_path, "first.py", "# @memory:feature:Shared\ndef first():\n    pass\n")
    _, before = sync(tmp_path)
    write(tmp_path, "second.py", "# @memory:feature:Shared\ndef second():\n    pass\n")
    _, after = sync(tmp_path)
    assert "second.py" in after.nodes["feature:Shared"]["summary"]
    assert before.nodes["feature:Shared"]["narrative_context_hash"] != after.nodes["feature:Shared"]["narrative_context_hash"]


def test_changed_dependency_invalidates_narrative_without_changing_member_file(tmp_path):
    write(tmp_path, "entry.py", "from helper import save\n# @memory:feature:Workflow\ndef run():\n    save()\n")
    dependency = write(tmp_path, "helper.py", "def save():\n    return 1\n")
    _, before = sync(tmp_path)
    dependency.write_text("def save():\n    return 2\n", encoding="utf-8")
    _, after = sync(tmp_path)
    assert before.nodes["feature:Workflow"]["narrative_context_hash"] != after.nodes["feature:Workflow"]["narrative_context_hash"]


def test_parse_failure_survives_persistence_and_clears_after_repair(tmp_path):
    target = write(tmp_path, "broken.py", "def broken(:\n    pass\n")
    _, graph = sync(tmp_path)
    attrs = graph.nodes["file:broken.py"]
    assert attrs["parse_status"] == "failed" and attrs["parse_error"]
    assert attrs["analysis_warnings"] and "parsing failed" in attrs["summary"].lower()
    hit = CodebaseMemory(graph).query_intent("broken")[0]
    assert hit.analysis_warnings
    target.write_text("def repaired():\n    pass\n", encoding="utf-8")
    _, graph = sync(tmp_path)
    attrs = graph.nodes["file:broken.py"]
    assert attrs["parse_status"] == "complete" and not attrs.get("parse_error")
    assert not attrs.get("analysis_warnings") and graph.has_node("func:broken.py::repaired")


def test_test_edits_outside_scan_scope_invalidate_carried_execution_evidence(tmp_path):
    from cms.scope import save_scope
    from cms.verify import CACHE_SCHEMA, build_verification_result, verification_input_hash, verification_status
    write(tmp_path, "module.py", "# @memory:feature:Work\ndef work():\n    pass\n")
    test_file = write(tmp_path, "tests/test_work.py", "def test_work():\n    assert True\n")
    save_scope(tmp_path, ["module.py"])
    _, before = sync(tmp_path)
    before.graph["coverage_evidence"] = {"schema_version": CACHE_SCHEMA, "input_hash": verification_input_hash(tmp_path)}
    before.nodes["feature:Work"]["exercised_by"] = ["tests/test_work.py::test_work"]
    before.nodes["feature:Work"]["verify_result"] = build_verification_result(tmp_path, ["tests/test_work.py::test_work"], True)
    CodebaseMemory(before).save(tmp_path / ".memory/graph.json")
    _, unchanged = sync(tmp_path)
    assert unchanged.nodes["feature:Work"]["exercised_by"]
    test_file.write_text("def test_work():\n    assert False\n", encoding="utf-8")
    stats, after = sync(tmp_path)
    assert stats.changed == []  # test lives outside the selected semantic scope
    assert not after.nodes["feature:Work"].get("exercised_by")
    historical = after.nodes["feature:Work"]["verify_result"]
    assert historical["passed"] is True and not verification_status(tmp_path, historical)["current"]


def test_watch_retries_provider_failure_and_stops_cleanly(tmp_path):
    from cms.update import watch
    target = write(tmp_path, "main.py", "def old_name():\n    pass\n")
    sync(tmp_path)

    class OnceFailing(MockProvider):
        def __init__(self): self.attempts = 0
        def summarize(self, prompt, context):
            self.attempts += 1
            if self.attempts == 1:
                raise TimeoutError("offline transient failure")
            return super().summarize(prompt, context)

    class Stop:
        stopped = False
        waits = 0
        def is_set(self): return self.stopped
        def set(self): self.stopped = True
        def wait(self, delay):
            self.waits += 1
            assert self.waits < 12, "watch failed to converge"
            if self.waits == 1:
                target.write_text("def new_name():\n    pass\n", encoding="utf-8")
            return self.stopped
    stop = Stop()
    statuses = []
    def status(row):
        statuses.append(row["status"])
        if row["status"] == "running" and "retrying" in statuses:
            stop.set()
    provider = OnceFailing()
    watch(tmp_path, provider, interval=0.01, echo=lambda *a: None, stop_event=stop, on_status=status)
    assert statuses == ["running", "retrying", "running", "stopped"]
    assert provider.attempts == 2
    graph = CodebaseMemory.load(tmp_path / ".memory/graph.json").graph
    assert "new_name" in graph.nodes["file:main.py"]["summary"]
    assert json.loads((tmp_path / ".memory/watch_status.json").read_text())["status"] == "stopped"
