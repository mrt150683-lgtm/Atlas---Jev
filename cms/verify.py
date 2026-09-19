"""Test↔feature verification — turn verification checklists into executable proof.

``map_tests_to_features`` runs the test suite under coverage with dynamic
contexts (one context per test function), then intersects each feature's
member line ranges with the lines each test executed. The resulting test ids
land on feature nodes as ``exercised_by`` — the tests that *execute* the
feature's code. (Deliberately not called "verified": coverage proves
execution, not behavioural correctness.) ``verify_feature`` then runs exactly
those tests and reports pass/fail.

Requires ``coverage`` and ``pytest`` (``pip install cms[dev]``).
"""

from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable

import networkx as nx

from .features import get_features
from .scanner import scan

COVERAGE_RC = """\
[run]
dynamic_context = test_function
branch = False

[json]
show_contexts = True
"""
CACHE_SCHEMA = 2


def _python() -> str:
    """Interpreter for subprocess runs — sys.executable is the exe when frozen."""
    if getattr(sys, "frozen", False):
        return shutil.which("python") or shutil.which("python3") or "python"
    return sys.executable


def _context_to_pytest_id(context: str, known_files: list[str]) -> str | None:
    """coverage context 'test_x.test_fn' or 'pkg.test_x.TestC.test_fn' ->
    pytest id 'tests/test_x.py::test_fn'. Module names are resolved against the
    scanned file list because pytest's rootdir handling strips package prefixes."""
    context = context.split("|", 1)[0].strip()
    if not context:
        return None
    parts = context.split(".")
    if len(parts) < 2:
        return None
    # longest module prefix that matches a known file's path suffix
    for cut in range(len(parts) - 1, 0, -1):
        suffix = "/".join(parts[:cut]) + ".py"
        match = next(
            (f for f in known_files if f == suffix or f.endswith("/" + suffix)), None
        )
        if match:
            tail = "::".join(parts[cut:])
            return f"{match}::{tail}"
    return None


def _coverage_input_hash(root: Path, pytest_args: list[str]) -> str:
    return _hash([verification_input_hash(root), pytest_args])


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def verification_input_hash(root: Path) -> str:
    """Content identity of project inputs, including tests outside semantic scope.

    This deliberately excludes generated memory. File timestamps are not proof
    identities. Runtime/dependency versions are included; external services and
    assertion adequacy remain explicit limitations of a local test run.
    """
    import os
    from importlib.metadata import version, PackageNotFoundError

    root = Path(root).resolve()
    paths = {root / rec.rel_path for rec in scan(root)}
    ignored = {".git", ".venv", "venv", "node_modules", "__pycache__", ".memory", ".pytest_cache", "build", "dist"}
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in ignored and not (Path(directory) / d).is_symlink()]
        for name in files:
            if (name.endswith(".py") and (name.startswith("test") or name == "conftest.py")) or name in {
                "pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg", "requirements.txt",
                "requirements-dev.txt", "uv.lock", "poetry.lock", ".coveragerc", ".cmsscope.json", ".cmsignore",
            }:
                paths.add(Path(directory) / name)
    rows = []
    for path in sorted(paths):
        if path.relative_to(root).as_posix() == "docs/feature_ledger.json":
            continue  # generated completion claims are consumers, not execution inputs
        if not path.resolve().is_relative_to(root):
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = "unreadable"
        rows.append((path.relative_to(root).as_posix(), digest))
    versions = []
    for package in ("pytest", "coverage"):
        try:
            versions.append((package, version(package)))
        except PackageNotFoundError:
            versions.append((package, "missing"))
    return _hash([CACHE_SCHEMA, sys.version, versions, rows])


def build_verification_result(root: Path, test_ids: list[str], passed: bool) -> dict:
    return {"schema_version": CACHE_SCHEMA, "input_hash": verification_input_hash(root),
            "test_ids": sorted(set(test_ids)), "tests": len(set(test_ids)),
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "passed": bool(passed), "evidence_kind": "test_execution"}


def verification_status(root: Path, result: dict | None, *, input_hash: str | None = None) -> dict:
    result = result or {}
    if not result:
        return {"current": False, "passed": False, "status": "not_recorded", "reason": "No recorded test execution."}
    current = (result.get("schema_version") == CACHE_SCHEMA and bool(result.get("test_ids"))
               and result.get("input_hash") == (input_hash or verification_input_hash(root)))
    return {"current": current, "passed": current and result.get("passed") is True,
            "status": ("passed" if result.get("passed") is True else "failed") if current else "stale",
            "reason": ("Selected tests passed; this alone does not establish requirement coverage."
                       if result.get("passed") is True else "Selected tests failed.") if current else
                      "Code, tests, configuration or runtime changed, or this legacy result has no input identity."}


def coverage_is_current(root: Path, graph, *, input_hash: str | None = None) -> bool:
    record = graph.graph.get("coverage_evidence") or {}
    return (record.get("schema_version") == CACHE_SCHEMA and
            record.get("input_hash") == (input_hash or verification_input_hash(root)))


def behavioral_status(root: Path, evidence: dict | None, *, goal: str | None = None,
                      input_hash: str | None = None) -> dict:
    """Declared criterion/test links plus actual runs; never inferred from approval."""
    evidence = evidence or {}
    criteria = evidence.get("criteria") or []
    current = (not evidence.get("inputs_changed_during_run") and evidence.get("schema_version") == CACHE_SCHEMA and
               evidence.get("kind") == "declared_criterion_test_runs" and bool(criteria) and
               evidence.get("intent_hash") == intent_context_hash(root, evidence.get("feature")) and
               evidence.get("input_hash") == (input_hash or verification_input_hash(root)) and
               evidence.get("plan_hash") == _hash([evidence.get("goal"), [
                   {k: c.get(k) for k in ("id", "expectation", "test_ids")} for c in criteria]]))
    matches = goal is None or str(evidence.get("goal", "")).strip() == str(goal).strip()
    passed = current and matches and all(c.get("passed") is True for c in criteria)
    return {"current": current, "goal_matches": matches, "passed": passed,
            "status": "passed" if passed else ("failed" if current and matches else "missing_or_stale"),
            "criteria": len(criteria),
            "limitation": "Requirement-to-test links are explicitly declared; Atlas does not independently prove assertion adequacy or external behavior."}


def intent_context_hash(root: Path, feature: str | None) -> str:
    from .decisions import DecisionStore
    store = DecisionStore(Path(root) / ".memory", root=root)
    decisions = [store.approved_for(None)]
    if feature:
        decisions.append(store.approved_for(feature))
    return _hash([{k: d.get(k) for k in ("id", "intent", "approved_at")} for d in decisions if d])


def run_verification_plan(root: Path, graph, plan: dict) -> dict:
    """Run a bounded, explicit requirement-to-pytest plan; persist via caller.

    Shape: {goal, features:[{feature, criteria:[{id, expectation, test_ids}]}]}.
    Only local pytest node IDs are accepted, never shell commands or options.
    Validate the whole plan before running or mutating graph evidence.
    """
    root = Path(root).resolve()
    if not isinstance(plan, dict) or not isinstance(plan.get("goal"), str) or not plan["goal"].strip():
        raise ValueError("verification plan needs a non-empty goal")
    features = plan.get("features")
    if not isinstance(features, list) or not 1 <= len(features) <= 30:
        raise ValueError("verification plan needs 1-30 features")
    validated, seen = [], set()
    for feature in features:
        if not isinstance(feature, dict):
            raise ValueError("each plan feature must be an object")
        name = feature.get("feature")
        node_id = f"feature:{name}"
        if not graph.has_node(node_id) or name in seen:
            raise ValueError(f"unknown or repeated feature {name!r}")
        seen.add(name)
        criteria = feature.get("criteria")
        if not isinstance(criteria, list) or not 1 <= len(criteria) <= 20:
            raise ValueError("each feature needs 1-20 criteria")
        clean, ids = [], set()
        for criterion in criteria:
            if not isinstance(criterion, dict) or any(not isinstance(criterion.get(k), str) or not criterion[k].strip()
                                                      for k in ("id", "expectation")):
                raise ValueError("each criterion needs an id and expectation")
            if criterion["id"] in ids:
                raise ValueError("criterion IDs must be unique within a feature")
            ids.add(criterion["id"])
            test_ids = criterion.get("test_ids")
            if not isinstance(test_ids, list) or not 1 <= len(test_ids) <= 50:
                raise ValueError("each criterion needs 1-50 pytest node IDs")
            for tid in test_ids:
                if not isinstance(tid, str) or "::" not in tid or tid.startswith("-"):
                    raise ValueError("supply explicit pytest node IDs, not files, commands or options")
                path = (root / tid.split("::", 1)[0]).resolve()
                if not path.is_relative_to(root) or not path.is_file() or path.suffix != ".py":
                    raise ValueError(f"test is not an existing in-project Python file: {tid!r}")
            clean.append({"id": criterion["id"], "expectation": criterion["expectation"],
                          "test_ids": sorted(set(test_ids))})
        validated.append((node_id, clean))
    before = verification_input_hash(root)
    intent_before = {node_id: intent_context_hash(root, graph.nodes[node_id]["name"])
                     for node_id, _ in validated}
    results = {}
    for node_id, criteria in validated:
        for criterion in criteria:
            passed, output = verify_feature(root, criterion["test_ids"])
            criterion.update(passed=passed, output=output[-4000:])
        result = {"schema_version": CACHE_SCHEMA, "kind": "declared_criterion_test_runs",
                  "feature": graph.nodes[node_id]["name"],
                  "intent_hash": intent_before[node_id],
                  "goal": plan["goal"].strip(), "input_hash": before, "criteria": criteria,
                  "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        result["plan_hash"] = _hash([result["goal"], [{k: c[k] for k in ("id", "expectation", "test_ids")} for c in criteria]])
        results[node_id] = result
    after = verification_input_hash(root)
    for node_id, result in results.items():
        result["inputs_changed_during_run"] = (before != after or intent_before[node_id] !=
            intent_context_hash(root, graph.nodes[node_id]["name"]))
        graph.nodes[node_id]["behavioral_evidence"] = result
    return {"features": results, "passed": all(
        not result["inputs_changed_during_run"] and all(c["passed"] for c in result["criteria"])
        for result in results.values()),
        "limitation": behavioral_status(root, None)["limitation"]}


def run_coverage(
    root: Path,
    pytest_args: list[str] | None = None,
    *,
    echo: Callable[[str], None] | None = None,
    refresh: bool = False,
    stream: bool = False,
) -> dict | None:
    """Run pytest under per-test coverage, with progress and safe caching."""
    pytest_args = pytest_args or []
    echo = echo or (lambda _message: None)
    memory_dir = root / ".memory"
    cache_file = memory_dir / "coverage_contexts.json"
    state_file = memory_dir / "verify_state.json"
    input_hash = _coverage_input_hash(root, pytest_args)
    if not refresh and cache_file.is_file() and state_file.is_file():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            if state.get("schema_version") == CACHE_SCHEMA and state.get("input_hash") == input_hash:
                echo("Coverage cache is current — reusing mapped per-test contexts.")
                return json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            pass

    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        rc = Path(tmp) / ".coveragerc"
        rc.write_text(COVERAGE_RC, encoding="utf-8")
        data_file = Path(tmp) / ".coverage"
        json_file = Path(tmp) / "coverage.json"
        env_args = ["--rcfile", str(rc), "--data-file", str(data_file)]
        echo("Coverage stage 1/3 — running pytest with per-test contexts…")
        run = subprocess.run(
            [_python(), "-m", "coverage", "run", *env_args,
             "-m", "pytest", "-q", *pytest_args],
            cwd=root, capture_output=not stream, text=True, timeout=900,
        )
        if run.returncode not in (0, 1):  # 1 = tests failed but ran; still useful
            if not stream:
                print((run.stdout or "")[-2000:] + (run.stderr or "")[-2000:], file=sys.stderr)
            return None
        echo(f"Coverage stage 1/3 complete in {time.monotonic() - started:.1f}s.")
        echo("Coverage stage 2/3 — exporting execution contexts…")
        export = subprocess.run(
            [_python(), "-m", "coverage", "json", *env_args,
             "-o", str(json_file), "--show-contexts"],
            cwd=root, capture_output=True, text=True, timeout=120,
        )
        if export.returncode != 0:
            print(export.stderr[-2000:], file=sys.stderr)
            return None
        data = json.loads(json_file.read_text(encoding="utf-8"))
        data["_atlas_evidence"] = {"schema_version": CACHE_SCHEMA,
                                   "input_hash": verification_input_hash(root),
                                   "test_run_passed": run.returncode == 0}
        if _coverage_input_hash(root, pytest_args) != input_hash:
            echo("Inputs changed during coverage; evidence was not saved. Retry after edits settle.")
            return None
        echo("Coverage stage 3/3 — saving reusable evidence cache…")
        memory_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(data), encoding="utf-8")
        state_file.write_text(json.dumps({
            "schema_version": CACHE_SCHEMA,
            "input_hash": input_hash,
            "pytest_args": pytest_args,
            "duration_seconds": round(time.monotonic() - started, 3),
        }, indent=2), encoding="utf-8")
        echo(f"Coverage mapping evidence ready in {time.monotonic() - started:.1f}s.")
        return data


# @memory:feature:FeatureVerification
# @memory:connects:FeatureTracing, ImpactAnalysis
# @memory:summary:Executable evidence — per-test coverage contexts intersected with feature member line ranges give exercised_by test lists at BOTH feature and component granularity; file members count only lines inside def/class bodies (import-time execution is not behavioural evidence); cms verify <Feature> runs exactly those tests.
def map_tests_to_features(graph: nx.DiGraph, root: Path, coverage_data: dict) -> dict[str, list[str]]:
    """Intersect per-test executed lines with feature member line ranges.

    Two honesty rules (dual-review Priority-0):
    - ``exercised_by`` also lands on every func/class node individually, so
      downstream consumers (exact-flow review) get STEP-granular evidence —
      one test touching one member never vouches for the others.
    - a file member contributes only lines inside its components' bodies;
      module-level lines run at import time, and "a test imported this
      module" is not evidence the feature's behaviour was executed. A file
      with no parsed components therefore contributes no coverage evidence.
    """
    graph.graph["coverage_evidence"] = dict(coverage_data.get("_atlas_evidence") or {})
    known_files = [
        a["path"] for _, a in graph.nodes(data=True) if a.get("type") == "file"
    ]
    # rel_path -> line -> {pytest ids}
    executed: dict[str, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
    id_cache: dict[str, str | None] = {}
    for file_path, file_data in coverage_data.get("files", {}).items():
        rel = Path(file_path).as_posix()
        for line_str, contexts in (file_data.get("contexts") or {}).items():
            for ctx in contexts:
                if ctx not in id_cache:
                    id_cache[ctx] = _context_to_pytest_id(ctx, known_files)
                tid = id_cache[ctx]
                if tid:
                    executed[rel][int(line_str)].add(tid)

    # per-component evidence (and the path -> component-ranges index)
    comp_ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for node_id, attrs in graph.nodes(data=True):
        if attrs.get("type") not in ("func", "class"):
            continue
        path = attrs.get("path", "")
        start, end = attrs.get("start_line") or 0, attrs.get("end_line") or 0
        comp_ranges[path].append((start, end))
        tests: set[str] = set()
        for line, line_tests in executed.get(path, {}).items():
            if start <= line <= end:
                tests |= line_tests
        if tests:
            attrs["exercised_by"] = sorted(tests)
        else:
            attrs.pop("exercised_by", None)

    mapping: dict[str, list[str]] = {}
    for feat in get_features(graph):
        tests = set()
        for member_id in feat.get("members", []):
            if not graph.has_node(member_id):
                continue
            attrs = graph.nodes[member_id]
            path = attrs.get("path", "")
            if attrs.get("type") == "file":
                ranges = comp_ranges.get(path)
                for line, line_tests in executed.get(path, {}).items():
                    if ranges and any(s <= line <= e for s, e in ranges):
                        tests |= line_tests
            else:
                start, end = attrs.get("start_line") or 0, attrs.get("end_line") or 0
                for line, line_tests in executed.get(path, {}).items():
                    if start <= line <= end:
                        tests |= line_tests
        mapping[feat["name"]] = sorted(tests)
        graph.nodes[feat["id"]]["exercised_by"] = sorted(tests)
    return mapping


def verify_feature(root: Path, test_ids: list[str]) -> tuple[bool, str]:
    """Run exactly the tests that exercise a feature. Returns (passed, output)."""
    if not test_ids:
        return False, "no tests mapped to this feature"
    before = verification_input_hash(root)
    run = subprocess.run(
        [_python(), "-m", "pytest", "-q", *test_ids],
        cwd=root, capture_output=True, text=True, timeout=600,
    )
    output = (run.stdout + run.stderr).strip()
    if verification_input_hash(root) != before:
        return False, output + "\nInputs changed during verification; rerun after edits settle."
    return run.returncode == 0, output
