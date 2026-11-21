# TS-REACT-001: Give every component an explicit, typed and immutable props contract

- **Severity:** SHOULD
- **Category:** maintainability
- **Applies to:** TypeScript / React — function components

## Rule

1. Every exported component SHOULD declare a named props type (`interface XxxProps` or `type XxxProps`) with no `any` and no index signatures used as an escape hatch.
2. Props SHOULD be treated as read-only: components MUST NOT mutate props or objects reachable from props (this part is a React rule and is non-negotiable).
3. Mutually exclusive prop combinations SHOULD be modelled with discriminated unions rather than several optional booleans.
4. Callbacks SHOULD be typed with the exact data they pass (`onSelect: (id: string) => void`), not `(...args: any[]) => any`.
5. Components MUST be pure during render: no side effects (network calls, subscriptions, DOM mutation, writes to module-level variables) in the render body; side effects go into event handlers or `useEffect`.
6. Optional props SHOULD have defaults via destructuring, not via `defaultProps` (deprecated for function components).

## Why

- The props type is the component's public API; explicit contracts make breaking changes visible to `tsc` at every call site.
- React assumes components are pure and props are immutable; violating this causes stale UI and bugs that only appear under Strict Mode, concurrent rendering or memoization.
- Discriminated unions make invalid states unrepresentable (e.g. a "link" button with no `href`).

## Good example

```tsx
type Variant = "primary" | "secondary";

export interface UserCardProps {
  readonly user: Readonly<{ id: string; name: string; avatarUrl?: string }>;
  readonly onSelect: (userId: string) => void;
  readonly variant?: Variant;
}

export function UserCard({ user, onSelect, variant = "primary" }: UserCardProps) {
  return (
    <button type="button" className={`card card--${variant}`} onClick={() => onSelect(user.id)}>
      {user.name}
    </button>
  );
}

// Discriminated union: an action is either a link or a button, never both.
type ActionProps =
  | { readonly kind: "link"; readonly href: string; readonly label: string }
  | { readonly kind: "button"; readonly onClick: () => void; readonly label: string };

export function Action(props: ActionProps) {
  if (props.kind === "link") {
    return <a href={props.href}>{props.label}</a>;
  }
  return (
    <button type="button" onClick={props.onClick}>
      {props.label}
    </button>
  );
}

// Derive a sorted copy instead of mutating the prop.
export function SortedList({ items }: { readonly items: readonly string[] }) {
  const sorted = [...items].sort((a, b) => a.localeCompare(b));
  return (
    <ul>
      {sorted.map((item) => (
        <li key={item}>{item}</li>
      ))}
    </ul>
  );
}
```

## Bad example

```tsx
let renderCount = 0;

export function UserCard(props: any) {          // no contract
  renderCount++;                                // side effect during render
  props.user.name = props.user.name.trim();     // mutates a prop
  fetch(`/api/track?u=${props.user.id}`);       // network call during render
  return <div onClick={props.onClick}>{props.user.name}</div>;
}

export function SortedList({ items }: { items: string[] }) {
  items.sort();                                 // mutates the parent's array in place
  return <ul>{items.map((i) => <li key={i}>{i}</li>)}</ul>;
}

// Optional booleans allow invalid combinations such as isLink without href.
interface ActionProps { isLink?: boolean; href?: string; onClick?: () => void; label: string }
```

## Enforcement

| Method | How |
|---|---|
| Compiler | `tsc --noEmit` with `strict`; `readonly` turns many prop mutations into compile errors. |
| Linter | `@typescript-eslint/no-explicit-any`; `react-hooks` plugin rules; `react/no-unstable-nested-components` (eslint-plugin-react). |
| Runtime (dev) | Wrap the app in `<StrictMode>`; React double-invokes render in development to surface impure components. |
| Code review | Exported components have a named props type; no side effects in render bodies. |

## References

- React — Keeping Components Pure: https://react.dev/learn/keeping-components-pure
- React — Rules of React (components and hooks must be pure; props are immutable): https://react.dev/reference/rules
- React — StrictMode: https://react.dev/reference/react/StrictMode
- React — Using TypeScript: https://react.dev/learn/typescript
- TypeScript Handbook — Discriminated unions: https://www.typescriptlang.org/docs/handbook/2/narrowing.html#discriminated-unions
