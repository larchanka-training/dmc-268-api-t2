# TS-STATE-001: Fetch server data with cancellation, explicit states and runtime validation

- **Severity:** SHOULD
- **Category:** correctness
- **Applies to:** TypeScript / React — components and hooks that load remote data

## Rule

1. Server state SHOULD be loaded through a data-fetching library (e.g. TanStack Query) or a single shared custom hook — not ad-hoc `fetch` calls scattered across components.
2. If `useEffect` is used for fetching, the effect MUST list all dependencies and MUST cancel or ignore stale responses in its cleanup (`AbortController` or an `ignore` flag).
3. Every fetch MUST check `response.ok` and MUST validate the response body at runtime before storing it as typed state (see [TS-CODE-001](TS-CODE-001-strict-types.md)).
4. Components SHOULD render explicit loading, error and success states; errors MUST NOT be silently dropped.
5. Derived values SHOULD be computed during render, not copied into additional state.
6. You MAY use framework-provided data loading (e.g. route loaders or Server Components) instead of client-side fetching when the framework supports it.

## Why

- Without cleanup, a slow response for an old `userId` can overwrite the newer one (race condition), and updates after unmount waste work.
- Unchecked `res.json()` turns API contract drift into crashes far from the cause.
- A library handles caching, deduplication, retries and cancellation consistently, removing a large amount of hand-written effect code.

## Good example

With TanStack Query (v5):

```tsx
import { useQuery } from "@tanstack/react-query";
import { z } from "zod";

const UserSchema = z.object({ id: z.string(), name: z.string() });
type User = z.infer<typeof UserSchema>;

async function fetchUser(userId: string, signal: AbortSignal): Promise<User> {
  const res = await fetch(`/api/users/${encodeURIComponent(userId)}`, { signal });
  if (!res.ok) {
    throw new Error(`GET /api/users failed with status ${res.status}`);
  }
  return UserSchema.parse(await res.json());
}

export function useUser(userId: string) {
  return useQuery({
    queryKey: ["user", userId],
    queryFn: ({ signal }) => fetchUser(userId, signal),
  });
}

export function UserProfile({ userId }: { readonly userId: string }) {
  const { data, error, isPending } = useUser(userId);
  if (isPending) return <p>Loading…</p>;
  if (error) return <p role="alert">Could not load the user.</p>;
  return <h1>{data.name}</h1>;
}
```

Without a library (effect with cleanup):

```tsx
import { useEffect, useState } from "react";

type State =
  | { status: "loading" }
  | { status: "error" }
  | { status: "success"; user: User };

export function UserProfile({ userId }: { readonly userId: string }) {
  const [state, setState] = useState<State>({ status: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    fetchUser(userId, controller.signal).then(
      (user) => setState({ status: "success", user }),
      (err: unknown) => {
        if (!controller.signal.aborted) {
          reportError(err);
          setState({ status: "error" });
        }
      },
    );
    return () => controller.abort();
  }, [userId]);

  if (state.status === "loading") return <p>Loading…</p>;
  if (state.status === "error") return <p role="alert">Could not load the user.</p>;
  return <h1>{state.user.name}</h1>;
}
```

## Bad example

```tsx
export function UserProfile({ userId }: { userId: string }) {
  const [user, setUser] = useState<User>();
  const [upperName, setUpperName] = useState("");          // derived state copied into state

  useEffect(() => {
    fetch(`/api/users/${userId}`)                            // no abort: stale responses race
      .then((r) => r.json())                                 // no res.ok check
      .then((data) => {
        setUser(data as User);                               // unchecked cast
        setUpperName(data.name.toUpperCase());
      });                                                    // errors are silently dropped
  }, []);                                                    // missing dependency: userId

  return <h1>{upperName}</h1>;                               // no loading/error state
}
```

## Enforcement

| Method | How |
|---|---|
| Linter | `react-hooks/exhaustive-deps` (error); `@typescript-eslint/no-floating-promises` catches un-handled promise chains. |
| Test | Component tests (e.g. React Testing Library + a mocked network layer) cover loading, error and success; a test switches `userId` quickly and asserts the latest user is shown. |
| Code review | No raw `fetch` in components outside the shared data layer; every response parsed by a schema. |

## References

- React — Fetching data with Effects: https://react.dev/reference/react/useEffect#fetching-data-with-effects
- React — You Might Not Need an Effect: https://react.dev/learn/you-might-not-need-an-effect
- React — Choosing the State Structure (avoid redundant state): https://react.dev/learn/choosing-the-state-structure
- TanStack Query — Overview: https://tanstack.com/query/latest/docs/framework/react/overview
- TanStack Query — Query Cancellation: https://tanstack.com/query/latest/docs/framework/react/guides/query-cancellation
- MDN — AbortController: https://developer.mozilla.org/en-US/docs/Web/API/AbortController
- Zod: https://zod.dev/
