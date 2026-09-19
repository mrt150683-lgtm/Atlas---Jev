"""Intent fidelity — does a feature match what was agreed, per the evidence?

Computed on demand from artifacts that already exist (review verdicts,
coverage mapping, verify outcomes, flow reviews, decisions, annotations) —
never stored; input fingerprints expose stale artifacts. Every
dimension carries the reason for its value, and thin evidence yields an
explicit ``insufficient_evidence`` instead of an invented score.
"""

from __future__ import annotations

from pathlib import Path

from . import config

DIMENSIONS = ("implemented", "tests_present", "tests_passing", "approved_intent",
              "intent_match", "open_contradictions", "stale_evidence")
OVERALL = ("on_track", "attention", "insufficient_evidence")


def _feature(graph, name: str) -> dict | None:
    for _, a in graph.nodes(data=True):
        if a.get("type") == "feature" and a.get("name", "").lower() == name.lower():
            return a
    return None


# @memory:feature:IntentFidelity
# @memory:connects:FeatureExpectationReview, ApprovedDecisions, ExactFlowReview, FeatureVerification
# @memory:summary:Decomposable, evidence-backed fidelity assessment per feature — every dimension explains itself, thin evidence says so instead of scoring, nothing is stored so nothing can silently go stale.
def intent_fidelity(root: Path, graph, feature_name: str) -> dict:
    feat = _feature(graph, feature_name)
    if feat is None:
        raise ValueError(f"unknown feature {feature_name!r}")
    name = feat.get("name", feature_name)
    dims: dict[str, object] = {}
    why: dict[str, str] = {}

    from .verify import behavioral_status, coverage_is_current, verification_input_hash, verification_status
    from .review import review_input_hash
    fingerprint = verification_input_hash(root)
    problems = []
    review = feat.get("review") or {}
    try:
        review_current = bool(review) and review.get("input_hash") == review_input_hash(Path(root), graph, feat)
    except (OSError, ValueError) as exc:
        review_current = False
        problems.append(f"review inputs unavailable: {type(exc).__name__}")
    verdict = review.get("verdict") if review_current else None
    if review and not review_current:
        problems.append("source review predates its current inputs or lacks provenance")
    if verdict == "aligned":
        dims["implemented"] = "yes"
        why["implemented"] = "Current bounded source review is consistent with the expectation; runtime behavior is not proven by this judgment"
    elif verdict == "partial":
        dims["implemented"] = "partial"
        why["implemented"] = "AI review found the core intent met with gaps: " + \
            "; ".join((review.get("gaps") or [])[:3])
    elif verdict == "drift":
        dims["implemented"] = "no"
        why["implemented"] = "AI review found built behaviour contradicting the expectation"
    else:
        dims["implemented"] = "unknown"
        why["implemented"] = "no valid AI review for this feature (run `cms review`)"

    tests = feat.get("exercised_by") or []
    mapping_current = coverage_is_current(root, graph, input_hash=fingerprint)
    dims["tests_present"] = bool(tests) and mapping_current
    why["tests_present"] = (f"{len(tests)} mapped test(s) execute this feature's lines"
                            if tests else "no coverage-mapped tests (run `cms verify`)")

    if tests and not mapping_current:
        problems.append("execution mapping is stale or has no input identity")
        why["tests_present"] = "Historical test candidates exist; current execution coverage has not been collected"
    vr = feat.get("verify_result") or {}
    execution = verification_status(root, vr, input_hash=fingerprint)
    criteria = behavioral_status(root, feat.get("behavioral_evidence"), input_hash=fingerprint)
    dims["tests_passing"] = execution["status"]
    why["tests_passing"] = execution["reason"]
    if execution["status"] == "stale":
        problems.append(execution["reason"])
    if feat.get("behavioral_evidence") and not criteria["current"]:
        problems.append("criterion evidence is stale or its intent changed")

    decision = None
    try:
        from .decisions import DecisionStore

        decision = DecisionStore(Path(root) / config.MEMORY_DIR_NAME,
                                 root=root).approved_for(name)
    except Exception as exc:
        problems.append(f"approved intent unavailable: {type(exc).__name__}")
    dims["approved_intent"] = "present" if decision else "absent"
    why["approved_intent"] = (f"approved decision {decision['id']} ({decision['title']})"
                              if decision else
                              "no human-approved intended-behaviour decision exists")

    flow = feat.get("flow_review") or {}
    flow_status = flow.get("status")
    if decision is None:
        dims["intent_match"] = "unknown"
        why["intent_match"] = "nothing to match against — no approved intent"
    elif flow_status == "differs_from_intent":
        dims["intent_match"] = "differs"
        why["intent_match"] = "the exact-flow review found behaviour differing from the approved intent"
    elif criteria["passed"]:
        dims["intent_match"] = "declared_checks_passed"
        why["intent_match"] = criteria["limitation"]
    else:
        dims["intent_match"] = "unknown"
        why["intent_match"] = "no verifying evidence connects the implementation to the approved intent yet"

    contradictions = 0
    try:
        from .annotations import ACTIVE_STATUSES, AnnotationStore

        store = AnnotationStore(Path(root) / config.MEMORY_DIR_NAME, root=root)
        contradictions = sum(
            1 for a in store._read()
            if a.get("status") in ACTIVE_STATUSES
            and (a.get("feature") == name or a.get("target") == f"feature:{name}")
            and a.get("type") in ("contradiction", "bug_suspicion", "security_concern"))
    except Exception as exc:
        problems.append(f"annotations unavailable: {type(exc).__name__}")
    dims["open_contradictions"] = contradictions
    why["open_contradictions"] = (f"{contradictions} open contradiction/bug-suspicion annotation(s)"
                                  if contradictions else "no open contradictions recorded")

    stale = bool(problems)
    stale_bits = list(problems)
    if flow:
        try:
            from .flowreview import content_hash as flow_hash

            if flow.get("content_hash") != flow_hash(graph, Path(root), name):
                stale = True
                stale_bits.append("flow review predates the current code/decision")
        except Exception as exc:
            stale = True
            stale_bits.append(f"flow evidence unavailable: {type(exc).__name__}")
    dims["stale_evidence"] = stale
    why["stale_evidence"] = "; ".join(stale_bits) or "No stale artifact detected; absent evidence is still absent"

    # overall: explicit honesty about thin evidence before any judgment
    has_any_evidence = (verdict in ("aligned", "partial", "drift")
                        or bool(tests) or bool(decision) or bool(flow) or bool(contradictions)
                        or bool(feat.get("behavioral_evidence")))
    if not has_any_evidence:
        overall = "insufficient_evidence"
        headline = "No review, no mapped tests, no decision, no flow review — nothing to assess."
    elif (dims["implemented"] == "no" or dims["intent_match"] == "differs"
          or dims["tests_passing"] == "failed" or criteria["status"] == "failed" or contradictions):
        overall = "attention"
        headline = "Evidence points at a gap between intent and reality — see the failing dimensions."
    elif criteria["passed"] and not stale:
        overall = "on_track"
        headline = "Declared criterion tests passed on current inputs; review the links for assertion adequacy."
    else:
        overall = "insufficient_evidence"
        headline = "Some evidence exists but not enough to call it — see what is missing below."

    assert overall in OVERALL
    return {"feature": name, "overall": overall, "headline": headline,
            "dimensions": dims, "explanations": why, "criterion_evidence": criteria,
            "completion_proven": False}
