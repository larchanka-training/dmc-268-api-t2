# TS-CODE-001: Compile in strict mode and do not use `any`

- **Severity:** MUST
- **Category:** correctness
- **Applies to:** TypeScript — all application and library code

## Rule

1. `tsconfig.json` MUST enable `"strict": true`. It SHOULD also enable `"noUncheckedIndexedAccess"`, `"noImplicitOverride"` and `"noFallthroughCasesInSwitch"`.
2. Code MUST NOT use explicit `any`. Values of unknown shape MUST be typed `unknown` and narrowed (type guards, `instanceof`, schema validation) before use.
3. Data crossing a trust boundary (HTTP responses, `localStorage`, `postMessage`, URL params, `JSON.parse`) MUST be validated at runtime before being treated as a typed value. A type assertion (`as T`) is not validation.
4. `@ts-ignore` MUST NOT be used. `@ts-expect-error` MAY be used with a description explaining why.
5. CI MUST run `tsc --noEmit` (or the framework's equivalent type-check step) and fail on errors.

## Why

- `strict` enables `strictNullChecks`, `noImplicitAny` and related checks that remove whole classes of runtime errors (`undefined is not a function`, missing null checks).
- `any` disables checking for everything it touches and spreads silently through inference.
- TypeScript types are erased at runtime; an API that changes shape will pass `as User` and crash later, far from the cause.

## Good example

```jsonc
// tsconfig.json (excerpt)
{
  "compilerOptions": {
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "noImplicitOverride": true,
    "noFallthroughCasesInSwitch": true
  }
}
```

```ts
import { z } from "zod";

const UserSchema = z.object({
  id: z.string(),
  name: z.string(),
  email: z.email(),
});
export type User = z.infer<typeof UserSchema>;

export function parseUser(input: unknown): User {
  return UserSchema.parse(input); // throws ZodError on invalid shape
}

export function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  return "Unknown error";
}

const scores: Record<string, number> = { alice: 3 };
const bob = scores["bob"]; // number | undefined with noUncheckedIndexedAccess
const bobScore = bob ?? 0;
```

## Bad example

```ts
export async function getUser(id: string): Promise<User> {
  const res = await fetch(`/api/users/${id}`);
  return (await res.json()) as User; // unchecked cast: runtime shape is unknown
}

function handle(data: any) {       // `any` disables type checking
  return data.items.map((i: any) => i.value.toUpperCase());
}

// @ts-ignore
legacyCall(1, 2, 3);                // hides a real type error with no explanation
```

## Enforcement

| Method | How |
|---|---|
| Compiler | `tsc --noEmit` in CI with `"strict": true`. |
| Linter (typescript-eslint) | `@typescript-eslint/no-explicit-any`, `@typescript-eslint/no-unsafe-assignment`, `@typescript-eslint/no-unsafe-member-access`, `@typescript-eslint/no-unsafe-call`, `@typescript-eslint/no-unsafe-return`, `@typescript-eslint/ban-ts-comment` — all included in `strictTypeChecked` / `recommendedTypeChecked` configs. |
| Code review | Every `as` assertion on external data is replaced by a runtime check. |

```js
// eslint.config.js (excerpt) — typed linting is required for the no-unsafe-* rules
import { defineConfig } from "eslint/config";
import tseslint from "typescript-eslint";

export default defineConfig(
  tseslint.configs.strictTypeChecked,
  {
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
    rules: {
      "@typescript-eslint/ban-ts-comment": ["error", { "ts-expect-error": "allow-with-description" }],
    },
  },
);
```

> Compatibility: typed linting requires a TypeScript version inside typescript-eslint's supported range (its `peerDependencies`; at the time of writing `>=4.8.4 <6.1.0`, so TypeScript 7 is not yet supported). Pin `typescript` accordingly in projects that use typed linting.

## References

- TypeScript — `strict`: https://www.typescriptlang.org/tsconfig/#strict
- TypeScript — `noUncheckedIndexedAccess`: https://www.typescriptlang.org/tsconfig/#noUncheckedIndexedAccess
- TypeScript Handbook — Narrowing: https://www.typescriptlang.org/docs/handbook/2/narrowing.html
- typescript-eslint — `no-explicit-any`: https://typescript-eslint.io/rules/no-explicit-any/
- typescript-eslint — Typed linting: https://typescript-eslint.io/getting-started/typed-linting/
- Zod: https://zod.dev/
