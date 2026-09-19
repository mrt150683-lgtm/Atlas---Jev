# Lean verification direction for Atlas

Status: design recommendation, 2026-09-19. No Lean integration or proof pipeline is implemented by this note.

## Recommendation

Consider Lean for a few critical, stable rules after measuring the value of the Jev integration.
Do not automatically translate the entire Python/JavaScript application into Lean during development.
Start with a bounded experiment; expand only if the proofs remain useful and maintainable as production code changes.

## What Atlas and Jev establish today

Atlas records graph relationships, source freshness, and test execution evidence.
[`cms/verify.py`](../cms/verify.py) labels its evidence `test_execution` and explicitly warns that passing selected tests does not establish requirement coverage.
Hashes identify inputs and detect changes; they do not establish behavioural correctness.
These checks complement formal verification but are not equivalent to mathematical proofs.

Jev returns typed decisions and probabilities. It does not generate code or proof explanations, and calibration does not guarantee an individual answer is correct.
A coding agent could attempt Lean proofs; Lean would check them. Jev could help select context or prioritise work, but must not be the authority declaring a proof valid. [TypeSafe System One](https://docs.typesafe.ai/concepts/system-one)

## What a Lean proof would mean

Lean can establish a precisely stated property for all inputs covered by its definitions and assumptions.
Its program-verification tools express preconditions, postconditions and loop invariants over Lean programs. [Lean program verification tutorial](https://lean-lang.org/doc/tutorials/latest/mvcgen/)

A correct proof of a Lean model does not automatically verify the production Python or JavaScript implementation.
The translation may omit mutation, exceptions, numeric semantics, concurrency, filesystem effects or runtime dependencies.
A matching source hash ties an artifact to a revision; it does not prove that the model faithfully represents that revision.
Establishing this correspondence is separate work. Aeneas demonstrates a dedicated Rust-to-Lean verification bridge; it is not a general Python/JavaScript translator. [Aeneas](https://lean-lang.org/use-cases/aeneas/)

Use distinct verdicts for **model property proved** and **production implementation verified**.
The second requires an explicitly reviewed correspondence argument or translation mechanism, including its trust assumptions.
Neither verdict means that every requirement or deployment environment has been verified.
The recent `file://` UI launch failure illustrates this boundary: proving selection logic would not establish that the application loads, finds its assets, reaches its API, or works in the supported browser.
Keep real browser acceptance tests against the documented launch method, alongside runtime and integration tests.

## Proposed first proof obligations

1. **Candidate integrity:** every selected context ID belongs to the supplied candidate set; selection never invents an ID or introduces a duplicate.
2. **Mandatory context:** every mandatory ID supplied in the candidate set remains selected, including when mandatory items exceed the ordinary selection limit.
3. **Cache validity:** reuse is permitted only when the declared query, candidate/source identity, model, prompt version and processing-policy identity all match.

Specify edge cases and limits before proving these rules. Start with pure selection/cache predicates, not the entire filesystem or network stack.
A proof that a cloud-permission predicate rejects disallowed requests would still require evidence that every production network path actually enforces it.
Property-based and integration tests remain useful for checking the implementation against a reference model; passing those tests alone is not a proof of equivalence.

## Proposed workflow and acceptance gates

1. Write and review the intended invariant, formal statement, assumptions and production-code boundary.
2. Give the coding agent that fixed specification and the relevant Atlas context; let it propose a proof.
3. Check the proof with a pinned Lean toolchain and dependencies, then independently replay it in CI where practical.
4. Record the result with its exact statement, dependencies and source correspondence; invalidate it when relevant inputs change.
5. Continue normal runtime, integration and browser acceptance checks before shipping.

A valid proof and a theorem that expresses the intended requirement are different questions.
Reject incomplete proofs (`sorry`/`sorryAx`), custom axioms, unchecked admissions and kernel-check bypasses.
Inspect transitive axiom dependencies; allow only explicitly listed and reviewed standard dependencies such as `propext`, `Classical.choice` and `Quot.sound`.
Reject unexpected dependencies rather than treating a successful build as sufficient evidence.
Specification changes must be visible and receive human review; an agent must not silently weaken the claim to make its proof pass.
Run generated proof builds in an isolated environment, and preserve the reviewed statement separately from generated proof attempts.
Lean documents axiom inspection and independent rechecking, and distinguishes proof validity from statement meaning. [Validating Lean proofs](https://lean-lang.org/doc/reference/latest/ValidatingProofs/)

## Proposed proof receipt

These are future fields, not an existing Atlas schema:

- `proof_id`, `feature_ids`, `claim_text`, `theorem_name`, `formal_statement_hash`.
- `scope`: exact definitions/functions covered, input domain, assumptions and explicit exclusions.
- `source_binding`: source paths and hashes, graph/input identity, correspondence method, reviewed bridge version and remaining trust assumptions.
- `artifacts`: specification, model and proof paths/hashes; dependency lock hash; Lean and checker versions.
- `validation`: checker commands/results, transitive axioms, approved dependency list, checked time and relevant CI run.
- `review`: reviewer and approved specification hash; review required again when the specification changes.
- `verdict`: `model_property_proved`, `production_implementation_verified`, `failed`, `stale` or `not_checked`; include the reason and relevant freshness inputs.
- `runtime_evidence`: linked execution/integration/browser receipts, retained as separate evidence with their own scope and freshness.

Invalidate a receipt when its specification, model, proof, bound source, bridge, toolchain or relevant dependencies change.
Do not turn a feature green as “formally verified” merely because one narrowly scoped theorem passed.
If the initial experiment cannot maintain a credible connection to production code, prioritise stronger tests and keep Lean optional.
