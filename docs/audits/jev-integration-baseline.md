# Jev integration baseline and review constraints

Read-only source assessment on 2026-09-19, branch `codex/jev-integration`.
The existing working tree contains substantial prior changes; this document
describes the current source, not a clean released revision. No remote inference
or test suite was run for this baseline.

## Integration boundary

Atlas already processes summaries, discovery, and source reviews behind its
MCP/CLI/UI surfaces. Jev should be an optional typed decision provider, initially
for relevance decisions over a local candidate shortlist. It should not replace
the broad text-generation provider, graph construction, graph facts, source
hashes, test execution, approved intent, or evidence-based completion gates.

`CodebaseMemory.query_intent` in `cms/memory.py` is deterministic keyword
ranking. Keep that primitive available unchanged. A root-aware selection
service should return both results and an explicit receipt; do not put mutable
`last_receipt` state on the shared memory object cached by MCP and the UI.

Consumers that should share selection semantics and receipt fields:

- `cms/mcp.py`: `query_codebase`, task prompt export, intent capture.
- `cms/cli.py`: `query` and existing task-prompt commands.
- `cms/ui.py`: `/api/query` and prompt/chat surfaces.
- `cms/chat.py`: `build_evidence`, so the answer and its context use one result.
- `cms/prompt_export.py`: `build_task_pack`, which is also used by intent capture.

Do not automatically add remote calls to internal feature discovery merely
because it also calls `query_intent`.

## Correctness boundaries

1. **No-match is distinct from failure.** A complete set of valid `irrelevant`
   decisions can produce an empty assisted selection. A timeout, invalid output,
   partial evaluation, or missing credentials should produce a labelled local
   fallback. Shadow mode always preserves local results while recording the
   proposed selection.
2. **User requirements survive selection.** Literal goal paths are recorded by
   `_declared_paths` and interpreted as mandatory in `build_alignment`.
   Relevance decisions must never remove or redefine those requirements. Pins
   derived from explicit paths should be identified separately from model picks;
   an absent/unmapped path must be reported without inventing a graph node.
3. **Cached relevance is not source freshness.** The graph can remain unchanged
   after a source edit. Existing file nodes carry SHA-256 content hashes. Check
   current source identity where available; distinguish current, changed,
   missing, and unknown rather than calling a matching cache key current truth.
   Unknown source validity must not turn a semantic selection into verification.
4. **Cache exact inputs and policy.** Include the actual bounded candidate text,
   query, candidate identifiers/order, model identity, decision schema/prompt
   version, policy mode and thresholds, source/freshness state, and endpoint
   identity. If truncation changes, invalidate the cache. Keep credentials out
   of keys, receipts, logs, and saved project configuration.
5. **Keep evidence provenance.** Receipts should identify local candidates,
   returned/accepted decisions, actual selection, model/version, mode, cache
   reuse, failure/no-match reason, source limitations, and timing/call counts.
   Do not call lexical weights, discrete relevance classes, or model confidence
   calibrated probabilities or proof of correctness.
6. **Preserve ordinary query compatibility.** MCP `query_codebase` currently
   returns a list and existing clients/tests depend on it. Expose a richer
   context/receipt operation or an explicit opt-in response contract, while
   keeping ordinary lexical access usable, especially with the policy off.

## Human interaction risk

The viewer currently invokes `/api/query` after a 220 ms typing debounce in
`cms/ui_assets/index.html`. Automatically evaluating each candidate remotely on
each keystroke would create avoidable cost and latency. Keep typeahead local and
offer an explicit assisted search/submit action, with visible mode, fallback,
and a compact receipt. The current search function also lacks a response-order
guard; older slow responses must not overwrite results for a newer query.

## Tests that establish the feature's claims

- Off mode makes zero provider calls and preserves lexical ordering.
- Shadow records the proposed decisions but preserves lexical ordering.
- Assist chooses a clearly relevant candidate over a lexical distractor.
- Valid all-irrelevant results are no-match; malformed/partial/failed inference
  is labelled fallback; neither invents candidates.
- Unknown IDs, wrong types, invalid classes, and unbounded responses are rejected.
- Explicit requested paths and unmapped requested paths survive model rejection.
- Source edits/deletion, summary changes, model/prompt/policy changes invalidate
  or bypass cached decisions appropriately.
- The same selection and receipt reach human search, agent context, task export,
  and chat evidence without mutation of the graph or approved intent.
- Concurrent distinct queries cannot exchange receipts or overwrite each other.
- Typeahead stays local and an older response cannot replace a newer query.

Relevant regression areas are `test_query.py`, `test_prompt_export.py`,
`test_chat.py`, `test_mcp.py`, `test_align.py`, and focused query tests in
`test_ui_server.py`, plus new isolated provider/selection tests using a fake
transport. Run the new tests first, then the affected regression subset. Keep
live provider evaluation separate from offline correctness tests.

## Runtime observation

`python` resolves to `C:\Python314\python.exe` (3.14.3), whose default environment
lacks the project's dependencies. The workspace `.venv` launcher references a
removed Python installation. The bundled Codex Python also lacks the project
packages. Adding the existing workspace `.venv/Lib/site-packages` to the system
Python's module search path successfully imports networkx 3.6.1, pytest 9.1.1,
typer 0.26.8, and pathspec 1.1.1. That is an available offline verification route;
only imports were checked, so compatibility of other binary dependencies and
the full test suite remains unverified.
