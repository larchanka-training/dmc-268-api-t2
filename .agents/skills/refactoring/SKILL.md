---
name: refactoring
description: "Then Make it Right — quality gates and code judo after green. Invoke only when the user says /refactoring or explicitly asks for a disciplined refactor pass."
disable-model-invocation: true
---

# Refactoring ("Then Make it Right")

Refactoring is the disciplined process of changing internal code structure without changing its external observable behavior. It follows the Red $\to$ Green minimal implementation ("Make it Work") to ensure code health, architectural integrity, and long-term maintainability across any programming language.

## The Core Rule

1. **Pin what the code does.** Ensure existing tests (or characterization tests) cover the current behavior.
2. **Change the structure.** Refactor code toward cleaner abstractions, lower complexity, single responsibility, and **radical structural simplification**.
3. **Prove the behavior did not change.** All tests must stay green. A failing test during refactoring is a regression, not progress.

---

## Interim gates (this Team 2 toolkit)

Read product `AGENTS.md` **before** assuming mutation/coverage tooling exists.

| Situation | Required gates | Skip / defer |
|---|---|---|
| **UI** with Vitest | `pnpm test` green; `pnpm build` (or `pnpm validate`); simplification-gate; optional `jscpd` if already available | Mutation (Stryker etc.) only if already in the project toolchain — do not add new mutation deps in-pass |
| **API** with pytest | Existing suite green; complexity/clone checks if tools are installed; simplification-gate | Mutation (`mutmut` etc.) only if already in the project toolchain — do not add new mutation deps in-pass |
| Runner + mutation already wired | Full matrix below | — |

Always keep **simplification-gate** (“code judo”). Prefer deleting complexity over rearranging it.

## The Quality Gates

To prevent subjective or superficial refactoring, enforce both automated metrics and structural simplification gates **when the stack supports them**:

### 1. `risk-gate` (Complexity & CRAP Score)
* **Rule**: Reduce each function under **CRAP 10** by splitting it, by testing it, or both. Consider whether case statements are worth reducing.
* **The Formula**: $\text{CRAP} = \text{CC}^2 \times (1 - \text{coverage})^3 + \text{CC}$.
  * If Cyclomatic Complexity ($\text{CC}$) is $\ge 10$, the function will fail even with 100% test coverage—it *must* be decomposed.

### 2. `clone-check` (Structural Duplication)
* **Rule**: Eliminate structural duplication that ranks **0.8 or above** (or $\ge 80\%$).
* Extract repeated logic, algorithmic patterns, or mirrored workflows into clean, reusable functions or shared classes. Check if a canonical helper already exists in the project before creating a new utility.

### 3. `mutant-gate` (Mutation Testing)
* **Rule**: Cover the uncovered. Kill the survivors. Delete dead code.
* Injects programmatic mutations (inverting comparison operators, arithmetic, or booleans) to verify that tests actually assert behavior.
  * **Killed mutant**: Test suite fails (Good—test verified behavior).
  * **Surviving mutant**: Test suite passes (Bad—weak assertion or dead code).

### 4. `simplification-gate` (Conceptual Reduction & "Code Judo")
* **Rule**: Delete complexity rather than rearranging it. A refactor that leaves the same number of concepts or merely moves messy code into helper functions has failed.
* **No thin wrappers**: Delete identity wrappers and pass-through abstractions that add indirection without buying clarity or protection.
* **Anti-spaghetti**: Clean up ad-hoc flags, booleans, and scattered special cases introduced during quick bug fixes or Green phases.
* **File decomposition**: Decompose files approaching or exceeding 1,000 lines into focused, cohesive modules.

---

## Native Tooling Matrix by Language

Execute the corresponding native CLI tool for the project being refactored:

| Ecosystem | `risk-gate` ($\text{CRAP} / \text{CC} < 10$) | `clone-check` (Duplication $< 0.8$) | `mutant-gate` (Mutation Kill) |
| :--- | :--- | :--- | :--- |
| **Universal (150+ langs)** | Check linter complexity rules | `npx jscpd --threshold 80 <path>` | — |
| **TypeScript / JavaScript** | `npx eslint --rule 'complexity: ["error", 10]'` | `npx jscpd --threshold 80 src/` | `npx stryker run` |
| **Go** | `gocyclo -over 10 .` or `golangci-lint run` | `npx jscpd --threshold 80 .` | `go-mutesting ./...` |
| **Rust** | `cargo clippy -- -D clippy::cognitive_complexity` | `npx jscpd --threshold 80 src/` | `cargo mutants` |
| **Python** | `radon cc -s -n B .` or `flake8 --max-complexity 10` | `npx jscpd --threshold 80 .` | `mutmut run` |
| **Java / JVM** | Checkstyle `CyclomaticComplexity` or Sonar | `npx jscpd --threshold 80 src/` | `mvn org.pitest:pitest-maven:mutationCoverage` |

---

## Architectural Disciplines

Refactoring must drive the code toward the following architectural standards:

### Delete Complexity Rather Than Rearranging It ("Code Judo")
* Look for opportunities to reframe the model so that whole branches, helpers, modes, or layers disappear entirely.
* Prefer solutions that make the code feel inevitable in hindsight.
* Assume there is often a structural move that uses the existing architecture more cleanly rather than layering on new code.

### Direct & Boring Over Generic Magic
* Prefer direct, legible code over "clever" indirection, generic wrappers, or loose typing (`any`/`unknown`).
* Strip out identity abstractions and pass-through helpers that add cognitive overhead without real benefits.

### Eliminate Ad-Hoc Branching
* Never allow temporary flags or one-off conditionals to harden into permanent spaghetti.
* Push branching logic into dedicated abstractions, state machines, or typed dispatchers.

### Canonical Reuse & Layer Cleanliness
* Keep logic in the canonical layer. Feature logic must not leak into shared paths.
* Reuse existing canonical utilities instead of creating near-duplicate helpers.

### Model the Domain
* Put the domain in a structure that makes illegal states impossible to represent.
* Maintain a glossary of domain terms in documentation or domain models.
* If existing code is already clear and local, leave it alone.

### Boundary Discipline
* Check data where it enters the system boundaries.
* Inside system boundaries, trust it.
* Keep business logic in pure, deterministic functions without side-effects.

### Type System Discipline
* Make the illegal value impossible to write.
* Parse outside data once, where it comes in (parse, don't validate).
* Do not silence compiler, type-checker, or linter warnings with suppressions.

### Make Operations Idempotent & Atomic
* Ensure running an operation twice, or running it after a crash halfway through, results in the exact same state.
* Structure related updates into atomic operations rather than leaving state half-applied.

### Separate Before Serializing Shared State
* If two components do not need the same mutable state, give each its own.
* If they do need shared state, allow only one writer, and enforce that single writer strictly in the structure.
* Avoid unnecessary sequential orchestration when independent tasks can execute in parallel.

---

## Step-by-Step Refactoring Workflow

1. **Verify Baseline**:
   Run the product suite **or** interim gate from `AGENTS.md`. If no runner exists, record the build/lint baseline; do not block the pass inventing an unexecutable suite. Characterization tests only when they can run.
2. **Run Quality Gates (stack-aware)**:
   Apply Interim gates above. Where tools exist: `risk-gate` and `clone-check` for CRAP/dup hotspots; flag files ≥ ~1,000 lines.
3. **Execute Surgical Refactorings & Simplifications**:
   * Search for the "code judo" move: reframe data structures or dispatch logic to delete complexity.
   * Split complex methods into cohesive pure helper functions and decompose files past 1,000 lines.
   * Strip out thin pass-through wrappers and ad-hoc boolean conditionals.
   * Eliminate duplication by reusing canonical helpers.
   * Apply domain and type system discipline.
4. **Mutation (optional)**:
   Run `mutant-gate` **only** if the project already has mutation tooling. Otherwise skip and note it in Close.
5. **Prove Invariance**:
   Re-run the same baseline gate (full suite or build/lint). Behaviour must not regress.

