---
name: tdd
description: Red-green TDD at agreed seams. Follow when user says /tdd, asks for test-first, or AGENTS.md Execute phase applies to a behaviour change.
disable-model-invocation: true
---

# Test-Driven Development

TDD is the red → green loop. This skill is the reference that makes that loop produce tests worth keeping: what a good test is, where tests go, the anti-patterns, and the rules of the loop. Every section applies on every cycle: consult them before and during the loop, not after.

When exploring the codebase, read `CONTEXT.md` (if it exists) so test names and interface vocabulary match the project's domain language, and respect ADRs in the area you're touching. Also follow always-on seams and quality commands in `AGENTS.md`.

## What a good test is

Tests verify behavior through public interfaces, not implementation details. Code can change entirely; tests shouldn't. A good test reads like a specification: "user can checkout with valid cart" tells you exactly what capability exists, and it survives refactors because it doesn't care about internal structure.

See [tests.md](tests.md) for methodology, [tests-ui-typescript.md](tests-ui-typescript.md) / [tests-python-fastapi.md](tests-python-fastapi.md) for stack samples, and [mocking.md](mocking.md) for mocking guidelines.

## Seams: where tests go

A **seam** is the public boundary you test at: the interface where you observe behavior without reaching inside. Tests live at seams, never against internals.

**Test only at agreed seams.** Prefer seams already stated in grill outcome, spec Testing Decisions, tickets, or conversation. Write them down once if missing, then proceed — ask the user **once** only when no seam is documented and more than one plausible public boundary exists. Do not re-confirm seams that are already agreed.

You can't test everything, so seams focus effort on critical paths and complex logic instead of every edge case. Coverage at the seam: see [tests.md](tests.md).

## Anti-patterns

- **Implementation-coupled**: mocks internal collaborators, tests private methods, or verifies through a side channel (querying the database instead of using the interface). The tell: the test breaks when you refactor but behavior hasn't changed.
- **Tautological**: the assertion recomputes the expected value the way the code does (`expect(add(a, b)).toBe(a + b)`, a snapshot derived by hand the same way, a constant asserted equal to itself), so it passes by construction and can never disagree with the code. Expected values must come from an independent source of truth: a known-good literal, a worked example, the spec.
- **Horizontal slicing**: writing all tests first, then all implementation. Bulk tests verify _imagined_ behavior: you test the _shape_ of things rather than user-facing behavior, the tests go insensitive to real changes, and you commit to test structure before understanding the implementation. Work in **vertical slices** instead: one test → one implementation → repeat, each test a **tracer bullet** that responds to what the last cycle taught you.

## Rules of the loop

- **Red before green.** Write the failing test first, then only enough code to pass it. Don't anticipate future tests or add speculative features.
- **One slice at a time.** One seam, one test, one minimal implementation per cycle.
- **Hard stop after each green.** Add exactly **one** next agreed AC test at the seam and run it. If it is **already green**, you overshot: delete speculative production code or split the last change until that test is a **real red**. Do not add further cases or more production code while the next case is green. “I need the full correct handler for this case” is still overshoot if later AC cases would already pass.
- **Evidence before production edits.** Before changing production code for a case, the turn must show failing test-runner output for **that** case (after fixing any false red from a bad fixture/double).
- **Refactoring is not part of the loop.** Prefer a separate review pass (AGENTS.md Close / `/project-code-review`), not the red → green implementation cycle.
