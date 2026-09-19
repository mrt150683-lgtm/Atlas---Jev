"""Shared, auditable context selection for humans and coding agents.

Jev judges relevance only. Atlas owns candidate IDs, required paths, freshness,
fallbacks and evidence. No selection changes the graph or a verification verdict.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time
import threading
import uuid

from . import config
from .decision import (DecisionError, DecisionSettings, JevDecisionProvider,
                       PROMPT_VERSION, load_settings, status)
from .features import Feature, feature_context_hash
from .memory import CodebaseMemory, QueryResult
from .scanner import IgnoreMatcher
from .scope import SCOPE_FILENAME, _norm as normalize_scope, dir_in_scope, file_in_scope
from .storage import atomic_write_json, read_json, transaction

POLICY_VERSION = "atlas-context-v2"
RELEVANCE_FLOOR = 0.5  # Advisory abstention rule, not an accuracy guarantee.
MAX_HISTORY = 100
MAX_CACHE = 128
LIMITATIONS = [
    "Relevance estimates are model judgments, not evidence that code is correct.",
    "Selection only considers the recorded shortlist; unindexed or missed code may matter.",
    "Read current source and run the declared checks before claiming completion.",
    "A current source hash does not establish that an AI summary interprets the source correctly.",
]
_rank_guard = threading.Lock()
_rank_locks: dict[str, threading.Lock] = {}


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    default=str).encode("utf-8")).hexdigest()


def _store(root: Path, name: str) -> Path:
    return Path(root) / config.MEMORY_DIR_NAME / "context" / name


def _required(memory: CodebaseMemory, query: str) -> list[str]:
    """Literal mapped paths and backticked exact symbols cannot be voted away."""
    normalized = query.replace("\\", "/")
    quoted = set(re.findall(r"`([^`]+)`", normalized))
    found = []
    for node, attrs in memory.graph.nodes(data=True):
        path = attrs.get("path", "")
        if attrs.get("type") == "file" and path and re.search(
                r"(?<![\w./-])" + re.escape(path) + r"(?![\w./-])", normalized):
            found.append(node)
        elif attrs.get("type") in {"func", "class", "feature"} and any(
                ref in quoted for ref in (node, attrs.get("name"),
                    f"{path}::{attrs.get('qualname') or attrs.get('name')}")):
            found.append(node)
    return sorted(set(found))


def _candidate_set(memory, query, top_k, limit):
    lexical = memory.query_intent(query, top_k=max(limit, top_k))
    required = _required(memory, query)
    by_id = {hit.node_id: hit for hit in lexical}
    for node in required:
        by_id.setdefault(node, memory._result(node, 0, True, True))
    baseline = list(dict.fromkeys(required + [h.node_id for h in lexical[:top_k]]))
    baseline = baseline[:max(top_k, len(required))]
    # Reserve room for nearby code with different vocabulary; never lose the
    # actual local baseline to this expansion or to the provider's budget.
    ids = list(dict.fromkeys(baseline + [h.node_id for h in lexical[:max(top_k, limit * 2 // 3)]]))
    neighbours = set()
    for hit in lexical[:4]:
        for left, right, data in list(memory.graph.in_edges(hit.node_id, data=True)) + list(memory.graph.out_edges(hit.node_id, data=True)):
            if data.get("type") in {"CALLS", "IMPORTS", "PART_OF", "CONTAINS"}:
                other = right if left == hit.node_id else left
                if memory.graph.nodes[other].get("type") in {"file", "func", "class", "feature"}:
                    neighbours.add(other)
    for node in sorted(neighbours):
        if node not in by_id:
            by_id[node] = memory._result(node, 0, True, True)
        if node not in ids:
            ids.append(node)
    ids = list(dict.fromkeys(ids + [h.node_id for h in lexical]))[:max(limit, len(baseline))]
    ranks = {h.node_id: index + 1 for index, h in enumerate(lexical)}
    return [by_id[node] for node in ids], baseline, required, ranks


def _provenance_paths(memory, hit):
    """All source inputs that can contribute to this candidate's summary.

    A feature narrative includes traced dependencies, not only its declared
    members. Follow the same edge types as feature_context_hash. Very broad
    features remain local instead of turning a query into a whole-repo audit.
    """
    paths = {hit.path} if hit.path else set()
    if hit.kind == "feature":
        attrs = memory.graph.nodes[hit.node_id]
        frontier = list(attrs.get("members") or []) + [
            node for node, _, edge in memory.graph.in_edges(hit.node_id, data=True)
            if edge.get("type") == "PART_OF"]
        visited = set()
        while frontier:
            node = frontier.pop()
            if node in visited or not memory.graph.has_node(node):
                continue
            visited.add(node)
            if len(visited) > 1024:
                return set()
            path = memory.graph.nodes[node].get("path")
            if path:
                paths.add(path)
            if len(paths) > 128:
                return set()
            frontier.extend(target for _, target, edge in memory.graph.out_edges(node, data=True)
                            if edge.get("type") in {"CONTAINS", "CALLS", "IMPORTS", "INHERITS"})
    return paths


def _policy_snapshot(root, paths):
    """Inspect only policies on the paths being considered for cloud processing."""
    names = {SCOPE_FILENAME, config.CMSIGNORE_FILENAME, ".gitignore"}
    normalized = {}
    for rel in paths:
        if not isinstance(rel, str) or "\\" in rel or Path(rel).is_absolute():
            raise ValueError("Invalid candidate provenance path.")
        parts = rel.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("Invalid candidate provenance path.")
        normalized[rel] = parts
        names.update("/".join(parts[:depth]) + "/.gitignore" for depth in range(1, len(parts)))

    def read_policy_files():
        values = {}
        for name in sorted(names):
            path = root / name
            path.resolve().relative_to(root)
            try:
                with path.open("rb") as handle:
                    data = handle.read(262_145)
            except FileNotFoundError:
                data = None
            if data is not None and len(data) > 262_144:
                raise ValueError("Source processing policy is too large.")
            values[name] = data
        return values

    files = read_policy_files()
    scope = None
    if files[SCOPE_FILENAME] is not None:
        def unique_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("Duplicate source processing policy key.")
                value[key] = item
            return value

        data = json.loads(files[SCOPE_FILENAME].decode("utf-8"), object_pairs_hook=unique_object)
        if (not isinstance(data, dict) or not isinstance(data.get("include"), list)
                or any(not isinstance(item, str) for item in data["include"])):
            raise ValueError("Source processing scope is invalid.")
        scope = {normalize_scope(item) for item in data["include"] if item.strip()} - {""}
        scope = scope or None
    matcher = IgnoreMatcher(root)
    allowed = {}
    for rel, parts in normalized.items():
        parents = ["/".join(parts[:depth]) + "/" for depth in range(1, len(parts))]
        allowed[rel] = (Path(rel).suffix.lower() in config.LANGUAGE_BY_EXTENSION
                        and file_in_scope(rel, scope) and not matcher.match_file(rel)
                        and all(dir_in_scope(parent, scope) and not matcher.match_file(parent)
                                for parent in parents))
    # IgnoreMatcher reads lazily. A policy edit during that read cannot silently
    # mix permissions from different versions of the user's selection.
    if read_policy_files() != files:
        raise ValueError("Source processing policy changed while being read.")
    stamp = _hash({"files": {name: hashlib.sha256(value).hexdigest() if value is not None else None
                              for name, value in files.items()},
                   "defaults": config.DEFAULT_IGNORES, "allowed": allowed})
    return stamp, allowed


def _freshness(memory, root, hit, memo):
    paths = _provenance_paths(memory, hit)
    if not paths:
        return "unknown", []
    states = []
    for rel in sorted(paths):
        if rel not in memo:
            file_attrs = memory.graph.nodes.get(f"file:{rel}", {})
            expected = file_attrs.get("content_hash")
            actual = None
            try:
                path = (root / rel).resolve()
                path.relative_to(root)
                if not expected or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                    state = "unknown"
                else:
                    with path.open("rb") as stream:
                        actual = hashlib.file_digest(stream, "sha256").hexdigest()
                    state = "current" if expected == actual else "stale"
                    summary_hash = (file_attrs.get("summary_meta") or {}).get("content_hash")
                    if file_attrs.get("summary"):
                        if summary_hash and summary_hash != expected:
                            state = "stale"
                        elif not summary_hash and state == "current":
                            state = "unknown"
            except (OSError, ValueError):
                state = "unknown"
            memo[rel] = {"path": rel, "expected": expected, "actual": actual, "state": state}
        states.append(memo[rel])
    state = "stale" if any(s["state"] == "stale" for s in states) else (
        "unknown" if any(s["state"] == "unknown" for s in states) else "current")
    if hit.kind in {"func", "class"} and hit.summary:
        # generate_summaries creates component slices in the same operation as
        # the file summary. It does not currently give children separate stamps.
        file_attrs = memory.graph.nodes.get(f"file:{hit.path}", {})
        file_hash = (file_attrs.get("summary_meta") or {}).get("content_hash")
        own_hash = (memory.graph.nodes[hit.node_id].get("summary_meta") or {}).get("content_hash")
        if own_hash and own_hash != file_attrs.get("content_hash"):
            state = "stale"
        elif state == "current" and (not file_attrs.get("summary") or not file_hash):
            state = "unknown"
    if hit.kind == "feature" and hit.summary and state != "stale":
        attrs = memory.graph.nodes[hit.node_id]
        stamp = attrs.get("narrative_context_hash")
        if not isinstance(stamp, str) or not re.fullmatch(r"[a-f0-9]{64}", stamp):
            state = "unknown"
        else:
            try:
                members = attrs.get("members")
                if not isinstance(members, list) or any(not isinstance(node, str) for node in members):
                    raise ValueError("Feature members lack valid provenance.")
                linked = {node for node, _, edge in memory.graph.in_edges(hit.node_id, data=True)
                          if edge.get("type") == "PART_OF"}
                feature = Feature(
                    name=attrs.get("name", ""), description=attrs.get("description", ""),
                    source=attrs.get("source", "declared"), members=members,
                    connects=attrs.get("connects") or [], aliases=attrs.get("aliases") or [],
                )
                if linked != set(members) or feature_context_hash(memory.graph, feature) != stamp:
                    state = "stale"
            except (KeyError, TypeError, ValueError, RecursionError):
                state = "unknown"
    return state, states


def _graph_stamp(root):
    try:
        stat = (root / config.MEMORY_DIR_NAME / "graph.json").stat()
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def _validate_scores(result, ids, model):
    if not isinstance(result, dict) or result.get("model") != model:
        raise ValueError("Model identity did not match the pinned model.")
    scores = result.get("scores")
    if not isinstance(scores, dict) or set(scores) != set(ids) or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or not 0 <= value <= 1 for value in scores.values()):
        raise ValueError("The provider did not return a valid score for every candidate.")
    usage = result.get("usage")
    if not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0
                                         for key in ("input_tokens", "output_tokens")):
        raise ValueError("Provider usage was incomplete.")
    return scores


def _cached_rank(root, settings, query, payload, fingerprint, provider, policy_paths, policy_stamp):
    path = _store(root, "cache.json")
    key = _hash({"query": query, "candidates": payload, "evidence": fingerprint,
                 "endpoint": "https://api.typesafe.ai/v1/systemone",
                 "settings": asdict(settings), "prompt": PROMPT_VERSION, "policy": POLICY_VERSION,
                 "processing_policy": policy_stamp})
    # One in-flight ranking per project, with exact duplicate requests coalesced.
    with _rank_guard:
        lock = _rank_locks.setdefault(str(path.resolve()), threading.Lock())
    if not lock.acquire(timeout=settings.timeout_seconds + 0.5):
        raise TimeoutError("Another context request is still running.")
    try:
        with transaction(path, timeout=settings.timeout_seconds + 0.5):
            if load_settings(root) != settings:
                raise ValueError("Decision policy changed before the request.")
            cache = read_json(path, {})
            if _policy_snapshot(root, policy_paths)[0] != policy_stamp:
                raise ValueError("Source processing policy changed before the request.")
            if key in cache:
                result = cache[key]
                _validate_scores(result, [r["id"] for r in payload], settings.model)
                return result, True
            result = provider.rank(query, payload)
            _validate_scores(result, [r["id"] for r in payload], settings.model)
            cache[key] = result
            atomic_write_json(path, dict(list(cache.items())[-MAX_CACHE:]))
            return result, False
    finally:
        lock.release()


def _persist(root, receipt):
    path = _store(root, "history.json")
    try:
        with transaction(path, timeout=2):
            saved = read_json(path, {"items": []}, rows="items")
            saved["items"] = [receipt, *saved["items"]][:MAX_HISTORY]
            atomic_write_json(path, saved)
    except (OSError, ValueError):
        receipt["persistence_warning"] = "This context decision could not be saved; existing history was preserved."


def history(root: Path) -> dict:
    return read_json(_store(root, "history.json"), {"items": []}, rows="items")


def get_receipt(root: Path, receipt_id: str) -> dict | None:
    if not isinstance(receipt_id, str) or not re.fullmatch(r"[a-f0-9]{32}", receipt_id):
        raise ValueError("Invalid context receipt id.")
    return next((item for item in history(root)["items"] if item.get("id") == receipt_id), None)


def compact_receipt(receipt):
    return {key: receipt.get(key) for key in (
        "id", "status", "mode", "reason", "provider", "model", "cache_hit",
        "elapsed_ms", "usage", "limitations", "persistence_warning")}


@dataclass
class ContextSelection:
    hits: list[QueryResult]
    receipt: dict

    def results(self):
        metadata = {item["id"]: item for item in self.receipt["candidates"]}
        return [{**asdict(hit), "selection": {
            "receipt_id": self.receipt["id"], "status": self.receipt["status"],
            "relevance": metadata[hit.node_id]["relevance"],
            "source_freshness": metadata[hit.node_id]["source_freshness"],
        }} for hit in self.hits]

    def to_dict(self):
        return {"results": self.results(), "selection": self.receipt}


# @memory:feature:ContextDecisions
# @memory:summary:Selects bounded context with optional Jev relevance estimates, protected exact targets, source freshness checks, local fallbacks and shared auditable receipts.
def select_context(memory: CodebaseMemory, root: Path, query: str, top_k: int = 8,
                   *, source: str = "query", allow_remote: bool = True,
                   record: bool = True, provider=None) -> ContextSelection:
    if not isinstance(query, str) or not query.strip() or len(query) > 4000:
        raise ValueError("Query must contain 1–4000 characters.")
    if type(top_k) is not int or not 1 <= top_k <= 50:
        raise ValueError("top_k must be an integer from 1 to 50.")
    root, query = Path(root).resolve(), query.strip()
    started = time.perf_counter()
    config_error = None
    try:
        settings = load_settings(root)
    except DecisionError:
        settings = DecisionSettings()
        config_error = "Decision settings are invalid; local results are retained."
    hits, baseline, required, local_ranks = _candidate_set(memory, query, top_k, settings.max_candidates)
    by_id = {hit.node_id: hit for hit in hits}
    memo, rows, payload = {}, [], []
    provenance = {}
    for hit in hits:
        freshness, facts = _freshness(memory, root, hit, memo)
        provenance[hit.node_id] = [fact["path"] for fact in facts]
        eligible = hit.kind in {"file", "func", "class", "feature"} and bool(facts)
        rows.append({"id": hit.node_id, "name": hit.name, "path": hit.path, "kind": hit.kind,
                     "start_line": hit.start_line, "end_line": hit.end_line,
                     "local_rank": local_ranks.get(hit.node_id), "relevance": None,
                     "mandatory": hit.node_id in required, "source_freshness": freshness,
                     "model_eligible": eligible,
                     "model_exclusion_reason": (
                         "Kept local: this item is not a source code candidate." if hit.kind not in {"file", "func", "class", "feature"}
                         else "Kept local: summary source provenance could not be established within the bounded review." if not facts
                         else "")})
        if eligible:
            payload.append({"id": hit.node_id, "name": hit.name[:200], "path": hit.path[:400],
                            "kind": hit.kind, "summary": hit.summary[:900]})
    policy_error, policy_stamp, allowed = None, None, {}
    try:
        policy_stamp, allowed = _policy_snapshot(root, set(memo))
    except (OSError, ValueError, RecursionError):
        policy_error = "Source processing scope or exclusions are unreadable or invalid; cloud processing is disabled for this request."
    for row in rows:
        if row["model_eligible"] and not all(allowed.get(rel, False) for rel in provenance[row["id"]]):
            row["model_eligible"] = False
            row["model_exclusion_reason"] = (
                "Kept local: source processing scope or exclusions could not be validated." if policy_error else
                "Kept local: current processing scope or exclusions do not permit all of this item's source inputs.")
    eligible_ids = {row["id"] for row in rows if row["model_eligible"]}
    payload = [item for item in payload if item["id"] in eligible_ids]
    fingerprint = _hash({"nodes": payload, "sources": memo, "processing_policy": policy_stamp,
                         "edges": sorted((a, b, d.get("type", "")) for a, b, d in memory.graph.edges(data=True)
                                         if a in by_id or b in by_id)})
    graph_stamp = _graph_stamp(root)
    receipt = {"id": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(),
               "query": query, "source": source, "mode": settings.mode, "status": "off",
               "reason": "Jev is off. Atlas used local retrieval and protected explicit targets.",
               "provider": None, "model": None, "cache_hit": False,
               "usage": {"input_tokens": 0, "output_tokens": 0},
               "graph_fingerprint": fingerprint, "policy_version": POLICY_VERSION,
               "processing_policy_fingerprint": policy_stamp,
               "baseline_ids": baseline, "selected_ids": baseline.copy(), "proposed_ids": [],
               "candidates": rows, "limitations": LIMITATIONS.copy(),
               "relevance_floor": RELEVANCE_FLOOR}
    if config_error:
        receipt.update(status="fallback", reason=config_error)
    elif not allow_remote:
        receipt.update(status="local", reason="This surface uses local search; no Jev request was made.")
    elif settings.mode != "off":
        readiness = status(root)
        if not readiness["ready"]:
            receipt.update(status="fallback", reason=readiness["reason"])
        elif policy_error:
            receipt.update(status="fallback", reason=policy_error)
        elif not hits:
            receipt.update(status="no_match", reason="Local retrieval found no candidates. Jev was not called.")
        elif not payload:
            receipt.update(status="local", reason="The shortlist has no code candidates permitted for cloud processing. Local results are retained; each candidate records why it was kept local.")
        elif len(payload) > settings.max_candidates:
            receipt.update(status="fallback", reason="Required context exceeds the configured candidate budget; local results are retained.")
        elif any(row["model_eligible"] and row["source_freshness"] != "current" for row in rows):
            receipt.update(status="fallback", reason="Candidate source is stale or its freshness is unknown. Refresh Atlas before using Jev ranking.")
        else:
            try:
                result, cached = _cached_rank(root, settings, query, payload, fingerprint,
                                               provider or JevDecisionProvider(settings), set(memo), policy_stamp)
                scores = result["scores"]
                receipt.update(provider="jev", model=result["model"], cache_hit=cached,
                               usage={"input_tokens": 0, "output_tokens": 0} if cached else result["usage"])
                if cached:
                    receipt["cached_usage"] = result["usage"]
                for row in rows:
                    row["relevance"] = scores.get(row["id"])
                ranked = sorted(by_id, key=lambda node: (-scores.get(node, -1), local_ranks.get(node, 10**9), node))
                # Retain one strong local lead for larger packs as a hedge against
                # a plausible but mistaken semantic ranking. Explicit targets win.
                protected = list(dict.fromkeys(required + [node for node in baseline if node not in scores]
                                               + (baseline[:1] if top_k >= 3 else [])))
                proposed = list(dict.fromkeys(protected + ranked))[:max(top_k, len(protected))]
                receipt["proposed_ids"] = proposed
                if max(scores.values()) < RELEVANCE_FLOOR:
                    receipt.update(status="no_match", proposed_ids=[], reason="Jev abstained: no sufficiently relevant candidate. Local results are retained for inspection, not endorsed by Jev.")
                elif settings.mode == "shadow":
                    receipt.update(status="shadow", reason="Jev's proposal was recorded for comparison. The agent receives local results.")
                else:
                    receipt.update(status="applied", selected_ids=proposed,
                                   reason="Jev relevance estimates ordered the shortlist; explicit targets and a local lead were retained.")
                after = {}
                for hit, row in zip(hits, rows):
                    row["source_freshness"], _ = _freshness(memory, root, hit, after)
                if after != memo or _graph_stamp(root) != graph_stamp:
                    receipt.update(status="fallback", selected_ids=baseline.copy(),
                                   reason="Source or memory changed during selection. Local results are retained; refresh before editing.")
                if load_settings(root) != settings:
                    receipt.update(status="fallback", selected_ids=baseline.copy(),
                                   reason="Decision settings changed during selection. The proposal was not applied.")
                if _policy_snapshot(root, set(memo))[0] != policy_stamp:
                    receipt.update(status="fallback", selected_ids=baseline.copy(),
                                   reason="Source processing scope or exclusions changed during selection. The proposal was not applied.")
            except (DecisionError, OSError, ValueError, TimeoutError, RecursionError):
                receipt.update(status="fallback", selected_ids=baseline.copy(),
                               reason="Jev ranking was unavailable or invalid. Atlas retained the local results.")
    selected_ranks = {node: i + 1 for i, node in enumerate(receipt["selected_ids"])}
    proposed_ranks = {node: i + 1 for i, node in enumerate(receipt["proposed_ids"])}
    for row in rows:
        row.update(selected=row["id"] in selected_ranks, selected_rank=selected_ranks.get(row["id"]),
                   proposed_rank=proposed_ranks.get(row["id"]))
        row["reason"] = ("Explicit target retained by Atlas." if row["mandatory"] else
                         "Selected context; inspect current source." if row["selected"] else
                         "Considered but not included in this context window.")
        if row["model_exclusion_reason"]:
            row["reason"] += " " + row["model_exclusion_reason"]
    receipt["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    if record:
        _persist(root, receipt)
    return ContextSelection([by_id[node] for node in receipt["selected_ids"]], receipt)
