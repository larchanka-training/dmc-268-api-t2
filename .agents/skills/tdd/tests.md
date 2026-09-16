# Good and Bad Tests

Methodology here is language-neutral. Stack samples:

- UI / TypeScript: [tests-ui-typescript.md](tests-ui-typescript.md)
- API / Python / FastAPI: [tests-python-fastapi.md](tests-python-fastapi.md)

## Good Tests

**Integration-style**: Test through real interfaces, not mocks of internal parts.

Characteristics:

- Tests behavior users/callers care about
- Uses public API only
- Survives internal refactors
- Describes WHAT, not HOW
- One logical assertion per test

A good test reads like a specification (for example: “user can checkout with a valid cart”) and asserts an observable outcome through the public seam.

## Coverage at the seam

At each agreed seam, prefer a small set of behaviour cases:

1. **Happy path** with representative valid inputs.
2. **Boundary / invalid / empty** inputs the public contract must handle (reject, default, or message) — not every permutation of every argument.
3. **Each distinct public outcome** the contract promises (success shapes, error kinds, empty states) — observed through the seam, not by reading locals.

Do **not** require a test per internal branch. If a branch matters, it must show up as a different **observable** result or side effect at the seam; otherwise delete or simplify the branch.

## Bad Tests

**Implementation-detail tests**: Coupled to internal structure.

Red flags:

- Mocking internal collaborators
- Testing private methods
- Asserting on call counts/order
- Test breaks when refactoring without behavior change
- Test name describes HOW not WHAT
- Verifying through external means instead of the public interface (for example reading a database row when the contract is “create then retrieve via API”)

**Tautological tests**: Expected value restates the implementation, so the test passes by construction.

- Bad: expected value is recomputed the same way the code computes it (reduce/sum in the test matching the production reduce/sum).
- Good: expected value is an independent, known literal or worked example from the spec.
