# When to Mock

Mock at **system boundaries** only:

- External APIs (payment, email, etc.)
- Databases (sometimes - prefer test DB)
- Time/randomness
- File system (sometimes)

Don't mock:

- Your own classes/modules
- Internal collaborators
- Anything you control

## Designing for Mockability

At system boundaries, design interfaces that are easy to mock:

**1. Use dependency injection**

Pass external dependencies in rather than creating them internally. Inject a payment client (or similar) into the function/service instead of constructing a vendor SDK inside the unit under test.

**2. Prefer SDK-style interfaces over generic fetchers**

Create specific functions for each external operation instead of one generic function with conditional logic. Each operation returns one specific shape so tests do not need branching mocks.

Stack samples: [tests-ui-typescript.md](tests-ui-typescript.md) (UI fetch/SDK shape) and [tests-python-fastapi.md](tests-python-fastapi.md) (API boundary notes).
