# Jev integration: focused flow audit

Date: 2026-09-19. Branch: `codex/jev-integration`.
Base commit: `eab1ac47a9ab8d508e7f08c5aab1ce985abd8b7e` plus the current working tree.

## Verdict

**PASS for the tested integration contracts and local human flows; PARTIAL for
the broader goal of better AI development and owner understanding.** The new
selection path, conservative cloud boundary, durable receipts and downstream
freshness labels passed offline verification. A real browser confirmed the
human flow. No live Jev inference, retrieval-quality study, coding-effectiveness
comparison or owner-comprehension study was performed. These results do not
establish that Jev improves Atlas's answers, coding success, costs or speed.

## Scope and environment

This is a focused audit of Jev context selection and its dependent agent/user
surfaces, not a full audit of every Atlas feature. The checkout already contained
substantial changes and untracked files; those were preserved. No commit or push
was made. This verifies the source branch; an existing desktop executable was
not rebuilt.

- Python 3.14.3 with dependencies from the workspace `.venv/Lib/site-packages`.
  The existing virtual-environment launcher referenced a removed interpreter.
- Test `Path.home()` isolated to `.memory/jev-work/test-home` before imports;
  test projects use temporary directories. No real global library/journal writes.
- HTTP integration uses the actual Atlas server and captured fake transport.
  Synthetic credentials and raw-source canaries check outbound minimization.
- Browser: Codex in-app browser, disposable local project under
  `.memory/jev-work/browser-demo`, Jev off, mock source summaries. Its server
  and browser tab were closed after verification.
- No live Jev calls, paid inference, external repository disclosure or automatic
  cloud enablement. This checkout has no `.atlas-decisions.json`.

## Goals and evidence

| Goal | Source | Evidence | Verdict / remaining gap |
|---|---|---|---|
| Streamline AI coding | User-stated | Shared selection service and paired retrieval diagnostic | PARTIAL: actual task outcomes, effort and cost unmeasured |
| Preserve skill-based agent access | User-stated | MCP query, intent, brief and answer tests; existing list contract retained | PASS for tested contracts |
| Keep the owner informed | User-stated | Browser preview, ranking comparison, exact receipt history and warnings | PARTIAL: controls work; comprehension benefit requires people |
| High accuracy and honest uncertainty | User-stated | Source/narrative provenance, strict response validation, fallback and downstream warning tests | PASS for these safeguards; semantic accuracy unmeasured |
| Prevent unintended cloud disclosure | Implementation requirement | Off mode, explicit policy, current scope/ignore checks, minimized captured request | PASS for tested boundaries; allowed summaries and task text still disclose information |
| Distinguish delivered context from verification | Product design | Receipts never mutate graph facts, intent approval or verification status | PASS for tested paths; no claim to observe third-party agent reading |

## Flow inventory and as-built paths

| Entry | Path | Reads / writes | External call | Observed result |
|---|---|---|---|---|
| MCP query / CLI context | `cms/mcp.py`, `cms/cli.py` → `select_context` in `cms/context_selection.py` | Graph, source/policy facts; bounded cache/history | Optional Jev only | Valid IDs, protected targets, explicit fallback and receipt |
| Human preview | `/context` → POST `/api/context/preview` in `cms/ui.py` → same selector | Local graph and receipt stores | Explicit preview obeys policy | Rendered selected/proposed/local ranks and freshness |
| History / exact receipt | GET `/api/context/history` and `/api/context/receipt` | Reads saved decisions | None | Exact historical snapshot; missing/invalid links explained |
| Typeahead / ordinary export navigation | Existing local query / local-only export | Local memory | No Jev | Typing and GET navigation do not start cloud ranking |
| Task brief / declared intent | `build_task_pack` in `cms/prompt_export.py`, `declare_intent` in `cms/mcp.py` | Selected context, feature evidence, compact receipt | Optional selection | Freshness and analysis limits survive actual exported prompts |
| Ask Atlas | `build_evidence` in `cms/chat.py` → answer provider | Selected evidence; saved chat with receipt reference | Existing answer provider plus optional selection | Captured answering prompt includes stale-source warnings and receipt |
| Labelled evaluation | `evaluate` in `cms/context_evaluation.py` | Caller labels; embedded receipts in report, no history pollution | Existing project policy only | Complete preflight, explicit metric denominators, failures retained |

The selector creates a bounded lexical/graph shortlist, preserves mapped literal
targets, checks source and summary provenance, and applies current scope/ignore
rules. `JevDecisionProvider` in `cms/decision.py` sends bounded task/candidate
metadata, validates typed independent relevance results and returns no invented
explanation. The selector rechecks policy/source identity, applies the selected
mode and records the actual delivery. Detailed responsibility and adoption
boundaries are in [the integration plan](../JEV_INTEGRATION.md).

## Runtime evidence

Final regression: **555 Python tests passed in 85.43 seconds**, no failures or
skips. Machine-readable run: `.memory/jev-work/pytest-final.xml`.
**20 Node tests passed**: 15 context-page behavior tests and 5 connection-view
regressions. The earlier full run exposed a Windows rejection-response failure;
it was reproduced and fixed before the final successful run.

Focused coverage includes `test_decision.py`, `test_context_selection.py`,
`test_context_surfaces.py`, `test_context_freshness_surfaces.py`,
`test_context_evaluation.py`, `test_context_ui.cjs` and
`test_viewer_boundaries.py`, plus existing query, brief, chat, MCP and UI tests.

The HTTP fake captures the real outbound wire request: only task and bounded
candidate fields are sent; raw-source and credential canaries do not appear in
it or receipt stores. Cache reuse across UI/MCP was observed, with zero new
reported token usage and historical usage separately retained. Tests cover off,
shadow, assist, invalid/partial outputs, timeout, no-match, explicit targets,
source changes, policy races, cache invalidation and preservation of corrupt stores.

Actual browser observations:

1. Initial page showed Jev off and empty history without inference.
2. Previewing `Explain how retry_request uses decoder.py` returned four items,
   marked `decoder.py` required, and displayed local/delivered ranks.
3. Reload retained the saved decision.
4. After editing the real demo source, a new preview displayed
   `Source changed · refresh required` on affected entries.
5. An exact link to the earlier receipt reopened that decision, including its
   historical-snapshot caveat, instead of substituting the newest entry.
6. With deliberately corrupted disposable history, preview retained usable
   results and displayed `Receipt not saved`; the warning survived history-load
   failure. The original history backup was restored afterward.
7. Desktop screenshots showed a readable comparison layout. No console warnings
   or errors were observed in the ordinary successful flow. Mobile browser layout,
   exhaustive keyboard accessibility and live assisted rendering were not audited.

## Findings and resolution

| ID / impact | Expected versus pre-fix result | Evidence and fix | Final status |
|---|---|---|---|
| JEV-001 / P1 | Narrowed scope must constrain disclosure; previously a fake provider received all four indexed paths when a fresh scan allowed only `storage.py` | Scope/ignore reproduction; current policies, ancestor ignores, feature dependencies and pre/post-request checks now govern payloads | Fixed; regression passed |
| JEV-002 / P1 | Known-stale evidence must remain labelled; brief/chat transformations dropped per-hit freshness | Source inspection; real byte-change fixtures now inspect actual answering prompts and persisted JSON/Markdown exports | Fixed; regression passed |
| JEV-003 / P2 | Agent intent response should expose its receipt; reduced response omitted it | Intent response test now retrieves matching receipt and per-target freshness | Fixed; regression passed |
| JEV-004 / P2 | Evaluation evidence should be recoverable; report exposed IDs for unpersisted receipts | Reports now embed full receipts and explicitly mark them unpersisted | Fixed; regression passed |
| JEV-005 / P2 | Invalid output destination should fail before inference; labels-overwrite check ran afterward | CLI test verifies no remote attempt and unchanged labels | Fixed; regression passed |
| JEV-006 / P2 | Saving failure must be visible; UI announced `Preview saved` despite persistence warning | New test failed before fix; persistent warning and honest preview status verified in tests and browser | Fixed; regression passed |
| JEV-007 / P1 | Feature narratives must match their inputs; old narrative could appear current after index/membership changes | Eight reproduced cases plus positive control; compare narrative fingerprint and graph membership, mark absent stamps unknown | Fixed; regression passed |
| JEV-008 / P2 | Rejected outdated pages should receive usable error; Windows sometimes aborted unread-body connections | Real 8 KiB POST reproduction; flush unchanged rejection, declare close, discard at most 1 MiB for 250 ms without parsing/mutation | Fixed; HTTP regressions and final suite passed |

## What passing tests do and do not establish

These tests establish the selected contracts, actual prompt plumbing, failure
handling and persistence behavior in isolated projects. The browser adds direct
evidence of rendered local flows. Provider fakes do not establish model judgment
quality. A source fingerprint does not establish a correct summary, complete
static dependency graph, working program or satisfied user intent.

Remaining adoption work:

1. Run a pre-labelled held-out retrieval study in shadow mode on permitted,
   freshly indexed repositories; evaluate candidate recall as well as reranking.
2. Run paired coding tasks with fixed model, repository state and behavioral
   acceptance tests. Count missed requirements, complete token usage and elapsed
   time, including preparation, failures and cold/warm caches.
3. Test owner comprehension using concrete evidence-finding and uncertainty
   questions. Build the connected intent → context → changes → checks view.
4. Enable assist only after agreed acceptance conditions pass. Skill selection,
   review triage and background classification remain later experiments.

No open reproduced defect remains in the audited integration after the final
regression. This is not a claim that all Atlas behavior or live Jev performance
has been verified. `ContextDecisions` remains `in_progress` in the feature ledger
because the broader usefulness goals still require measurement.
