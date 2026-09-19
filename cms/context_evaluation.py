"""Paired retrieval diagnostics over caller-labelled cases; never an effectiveness claim."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path
import time

from . import config
from .context_selection import select_context
from .memory import CodebaseMemory

MAX_CASES = 100
PROTOCOL = "retrieval diagnostic, not coding effectiveness"


def _ratio(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def _metrics(ids: list[str], expected: set[str]) -> dict:
    found = set(ids)
    relevant = len(found & expected)
    return {"precision": _ratio(relevant, len(found)),
            "recall": _ratio(relevant, len(expected))}


def _validate(cases, memory, top_k):
    if type(top_k) is not int or not 1 <= top_k <= 50:
        raise ValueError("top_k must be an integer from 1 to 50.")
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise ValueError(f"Supply between 1 and {MAX_CASES} labelled cases.")
    seen, validated = set(), []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Every evaluation case must be an object.")
        case_id, query, expected = case.get("id"), case.get("query"), case.get("expected_ids")
        if (not isinstance(case_id, str) or not case_id.strip() or len(case_id) > 200
                or case_id.strip() in seen):
            raise ValueError("Case IDs must be unique, nonempty strings of at most 200 characters.")
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise ValueError("Each query must contain between 1 and 4000 characters.")
        if (not isinstance(expected, list) or any(not isinstance(node, str) for node in expected)
                or len(set(expected)) != len(expected)
                or any(node not in memory.graph for node in expected)):
            raise ValueError("expected_ids must contain unique IDs from the current graph.")
        answerable = case.get("answerable", bool(expected))
        if type(answerable) is not bool or answerable != bool(expected):
            raise ValueError("Answerable cases need expected IDs; unanswerable cases must have none.")
        seen.add(case_id.strip())
        validated.append({"id": case_id.strip(), "query": query.strip(),
                          "expected_ids": expected.copy(), "answerable": answerable})
    return validated


def evaluate(root: Path, cases: list[dict], top_k: int = 5) -> dict:
    """Evaluate every case under existing project policy, without enabling cloud use.

    Labels belong to the caller and are never sent to the selection provider.
    Local, selected and proposed IDs come from the same retrieval receipt.
    Unexpected per-case failures remain in denominators and are reported.
    """
    root = Path(root).resolve()
    memory = CodebaseMemory.load(root / config.MEMORY_DIR_NAME / "graph.json")
    cases = _validate(cases, memory, top_k)  # Complete preflight before any inference.
    rows = []
    for case in cases:
        started = time.perf_counter()
        error = None
        try:
            receipt = select_context(memory, root, case["query"], top_k=top_k,
                                     source="evaluation", record=False).receipt
        except Exception as exc:
            error = type(exc).__name__  # Do not expose provider bodies or credentials.
            receipt = {"status": "error", "mode": "unknown", "candidates": [],
                       "baseline_ids": [h.node_id for h in memory.query_intent(case["query"], top_k)],
                       "selected_ids": [], "proposed_ids": []}
        expected = set(case["expected_ids"])
        candidate_ids = [row["id"] for row in receipt.get("candidates", [])]
        row = {**case, "receipt_id": receipt.get("id"), "status": receipt["status"],
               "receipt_persisted": False, "receipt": deepcopy(receipt),
               "mode": receipt.get("mode"), "reason": receipt.get("reason"), "error": error,
               "cache_hit": bool(receipt.get("cache_hit")), "model": receipt.get("model"),
               "elapsed_ms": receipt.get("elapsed_ms", round((time.perf_counter() - started) * 1000, 2)),
               "usage": receipt.get("usage", {"input_tokens": 0, "output_tokens": 0}),
               "cached_usage": receipt.get("cached_usage", {"input_tokens": 0, "output_tokens": 0}),
               "candidate_ids": candidate_ids,
               "missing_from_shortlist": sorted(expected - set(candidate_ids)),
               "candidate_recall": _ratio(len(expected & set(candidate_ids)), len(expected))
                   if case["answerable"] else None,
               "metrics": {}}
        for name in ("baseline", "selected", "proposed"):
            row[name + "_ids"] = list(receipt.get(name + "_ids", []))
            row["metrics"][name] = _metrics(row[name + "_ids"], expected) if case["answerable"] else None
        row["no_match_signal"] = row["status"] == "no_match"
        row["empty_selection"] = not row["selected_ids"]
        row["abstention_signal"] = (row["status"] not in {"fallback", "error"}
                                    and (row["no_match_signal"] or row["empty_selection"]))
        rows.append(row)

    answerable = [row for row in rows if row["answerable"]]
    unanswerable = [row for row in rows if not row["answerable"]]
    aggregate = {}
    for name in ("baseline", "selected", "proposed"):
        aggregate[name] = {
            metric: _ratio(sum(row["metrics"][name][metric]["numerator"] for row in answerable),
                           sum(row["metrics"][name][metric]["denominator"] for row in answerable))
            for metric in ("precision", "recall")}
    return {"protocol": PROTOCOL, "project": root.name, "top_k": top_k,
            "label_source": "Caller-supplied labels; independence must be established by the evaluator.",
            "cases_total": len(rows), "answerable_cases": len(answerable),
            "unanswerable_cases": len(unanswerable), "excluded_cases": 0,
            "modes": dict(Counter(row["mode"] for row in rows)),
            "statuses": dict(Counter(row["status"] for row in rows)),
            "fallback_count": sum(row["status"] == "fallback" for row in rows),
            "error_count": sum(row["status"] == "error" for row in rows),
            "cache_hits": sum(row["cache_hit"] for row in rows), "metrics": aggregate,
            "candidate_recall": _ratio(sum(row["candidate_recall"]["numerator"] for row in answerable),
                                       sum(row["candidate_recall"]["denominator"] for row in answerable)),
            "shortlist_miss_cases": sum(bool(row["missing_from_shortlist"]) for row in answerable),
            "unanswerable": {
                "baseline_empty_rate": _ratio(sum(not row["baseline_ids"] for row in unanswerable), len(unanswerable)),
                "abstention_signal_rate": _ratio(sum(row["abstention_signal"] for row in unanswerable), len(unanswerable)),
                "no_match_signals": sum(row["no_match_signal"] for row in unanswerable),
                "empty_selection_cases": sum(row["empty_selection"] for row in unanswerable),
                "fallback_or_error_cases": sum(row["status"] in {"fallback", "error"} for row in unanswerable)},
            "answerable_abstention_signals": sum(row["abstention_signal"] for row in answerable),
            "elapsed_ms": round(sum(row["elapsed_ms"] for row in rows), 2),
            "usage": {key: sum(row["usage"].get(key, 0) for row in rows)
                      for key in ("input_tokens", "output_tokens")},
            "cached_usage": {key: sum(row["cached_usage"].get(key, 0) for row in rows)
                             for key in ("input_tokens", "output_tokens")},
            "limitations": ["Precision and recall are micro-averaged over answerable cases only; zero precision denominator is undefined.",
                            "Each receipt is embedded in its case; evaluation does not add receipts to Atlas history, so receipt IDs are not history links.",
                            "Proposed results are diagnostic; shadow proposals were not delivered and missing proposals count as zero recall.",
                            "A no-match signal can retain local inspection results; it is not necessarily an empty selection.",
                            "Usage is reported provider usage, not guaranteed billing; failures may incur unreported charges. Cached usage is historical, not new consumption.",
                            "Labels must be prepared independently. No coding effectiveness, human usability or statistical significance is established."],
            "cases": rows}
