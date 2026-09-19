# Jev live connection check

Date: 2026-09-19. Model: `jev-1.13.0`.

## Observed results

1. A live request containing two invented examples authenticated successfully
   and passed Atlas's typed-response validation. Jev scored the sorting example
   0.91 and the unrelated color example 0.02. Reported usage was 597 input and
   38 output tokens; the adapter measured 1,406.06 ms.
2. After the owner explicitly approved shadow processing for this Atlas project,
   a fresh in-memory structural graph restricted to `cms/` supplied 24 candidates
   for “How does Atlas select context for Jev?”. All candidates had current source
   hashes. Only names, relative paths, kinds and the task were sent; summaries
   were empty and source bodies were excluded.
3. The shared selection service returned `shadow` with a real Jev proposal and
   retained the local baseline. Reported usage was 3,864 input and 426 output
   tokens; selection took 1,109.81 ms. This was not a cache replay.
4. The saved graph's SHA-256 was unchanged before and after the structural test.
   The real receipt was saved with an explicit scope limitation and inspected
   in the browser's Context decisions screen.

The viewer was restarted with the saved credential. Project policy is local,
untracked, and set to shadow; the credential is not stored in project policy,
request artifacts, source control or this report.

## Remaining limits

These are connection and integration checks, not a retrieval-quality benchmark.
Two examples and one structural query cannot establish accuracy, calibration,
coding success, speed or cost savings for normal development tasks.

The existing saved map predates source/summary provenance tracking. Its 364 file
nodes lack the hashes required by the new ranking gate. Ordinary queries using
that map therefore still retain local results until a proper refresh. Existing
Anthropic summaries were preserved, not relabelled as current or replaced by
mock output. Jev does not generate their replacements.

A full fresh scan also includes old audit snapshots in the current scope.
Resolve those duplicate inputs before retrieval benchmarking, regenerate useful
summaries with recorded provenance, and then evaluate shadow decisions on
independently labelled tasks before considering assist mode.
