# examples/rules

Reference implementations of selected rules from [RULES_GUIDE.md](../../RULES_GUIDE.md). Each file is one atomic rule with a severity, a correct and an incorrect example, an enforcement method, and official references.

These files are meant to be:

- **Read by people** during code review: link the rule ID in review comments (e.g. `Violates PY-SEC-001`).
- **Loaded by AI coding agents** as context: each file is self-contained and short enough to include in a prompt.
- **Mapped to CI checks**: the *Enforcement* section names the linter rule, test or job that verifies the rule.

## Layout

```text
examples/rules/
  README.md
  python-fastapi/
    PY-API-001-input-validation.md
    PY-API-002-dependency-injection.md
    PY-ERR-001-error-handling.md
    PY-SEC-001-authz.md
  typescript-react/
    TS-CODE-001-strict-types.md
    TS-REACT-001-component-contracts.md
    TS-SEC-001-untrusted-data.md
    TS-STATE-001-data-fetching.md
  go/
    GO-ERR-001-error-handling.md
    GO-CONC-001-race-safety.md
    GO-SEC-001-sql-injection.md
    GO-TEST-001-fuzzing.md
  security-baseline/
    SEC-INPUT-001-validation.md
    SEC-AUTHZ-001-least-privilege.md
    SEC-SECRET-001-no-hardcoded-secrets.md
    SEC-LOG-001-sensitive-data.md
    SEC-DEP-001-dependency-scanning.md
```

## Rule ID scheme

`<STACK>-<AREA>-<NNN>`

| Prefix | Stack | Areas used |
|---|---|---|
| `PY` | Python / FastAPI | `API`, `ERR`, `SEC`, `CODE` |
| `TS` | TypeScript / React | `CODE`, `REACT`, `SEC`, `STATE` |
| `GO` | Go | `FMT`, `ERR`, `CTX`, `CONC`, `SEC`, `TEST` |
| `SEC` | Security baseline (all stacks) | `INPUT`, `AUTHZ`, `SECRET`, `LOG`, `DEP`, `CRYPTO`, `TRANSPORT` |

IDs are permanent. A retired rule keeps its ID (marked *Deprecated*); the number is never reused.

## File template

Copy this when adding a rule. File name: `<RULE-ID>-<short-slug>.md`.

````markdown
# RULE-ID: Title in imperative form

- **Severity:** MUST | SHOULD | MAY
- **Category:** correctness | security | performance | maintainability
- **Applies to:** stack / layer

## Rule

Short imperative statement(s) using MUST / SHOULD / MAY. One rule = one checkable behavior.

## Why

Risk prevented or benefit gained. Link to OWASP / CWE where relevant.

## Good example

```lang
minimal correct code
```

## Bad example

```lang
minimal incorrect code
```

## Enforcement

| Method | How |
|---|---|
| Linter / Test / CI / Code review | exact rule name, test, or command |

## References

- Official documentation links only
````

Authoring checklist:

- [ ] The rule is atomic: a reviewer can answer "violated / not violated" for a given diff.
- [ ] Every MUST has at least one enforcement method (automated preferred; manual review is acceptable when stated).
- [ ] The good example is safe to copy as-is. Unsafe code appears **only** in the bad example.
- [ ] Linter rule names, CLI flags and APIs are verified against the official docs linked in *References*.
- [ ] The rule is added to the table below and to the matrix in `RULES_GUIDE.md`.

## Rules in this directory

| ID | Title | Severity | Category | Primary check |
|---|---|---|---|---|
| [PY-API-001](python-fastapi/PY-API-001-input-validation.md) | Validate request input with Pydantic models | MUST | security | pytest negative tests (422), Semgrep custom rule, review |
| [PY-API-002](python-fastapi/PY-API-002-dependency-injection.md) | Provide shared resources through `Depends` | SHOULD | maintainability | Review, tests via `dependency_overrides` |
| [PY-ERR-001](python-fastapi/PY-ERR-001-error-handling.md) | Map errors to HTTP responses; never swallow or leak | MUST | correctness | Ruff `E722`, `BLE001`, `S110`, `S112`, `B904`; pytest |
| [PY-SEC-001](python-fastapi/PY-SEC-001-authz.md) | Authenticate every endpoint, authorize every object | MUST | security | pytest authz matrix + route audit test |
| [TS-CODE-001](typescript-react/TS-CODE-001-strict-types.md) | Strict mode, no `any` | MUST | correctness | `tsc --noEmit`, typescript-eslint `no-explicit-any`, `no-unsafe-*` |
| [TS-REACT-001](typescript-react/TS-REACT-001-component-contracts.md) | Typed, immutable props contracts; pure render | SHOULD | maintainability | `tsc`, StrictMode, review |
| [TS-SEC-001](typescript-react/TS-SEC-001-untrusted-data.md) | Render untrusted data only through safe sinks | MUST | security | `react/no-danger`, `react/jsx-no-script-url`, `no-eval`, CodeQL |
| [TS-STATE-001](typescript-react/TS-STATE-001-data-fetching.md) | Fetch with cancellation, explicit states, validation | SHOULD | correctness | `react-hooks/exhaustive-deps`, component tests |
| [GO-ERR-001](go/GO-ERR-001-error-handling.md) | Check, wrap (`%w`) and inspect errors correctly | MUST | correctness | golangci-lint `errcheck`, `errorlint`; `go vet` |
| [GO-CONC-001](go/GO-CONC-001-race-safety.md) | Synchronize shared state; test with `-race` | MUST | correctness | `go test -race ./...`, `go vet` copylocks |
| [GO-SEC-001](go/GO-SEC-001-sql-injection.md) | Placeholders and allowlisted identifiers in SQL | MUST | security | gosec `G201`/`G202`, CodeQL `go/sql-injection` |
| [GO-TEST-001](go/GO-TEST-001-fuzzing.md) | Fuzz parsers of untrusted input | SHOULD | security | `go test -fuzz` CI job, review |
| [SEC-INPUT-001](security-baseline/SEC-INPUT-001-validation.md) | Server-side allowlist validation of all input | MUST | security | Schemas + negative tests, gosec `G304`, review |
| [SEC-AUTHZ-001](security-baseline/SEC-AUTHZ-001-least-privilege.md) | Deny by default, least privilege | MUST | security | Authz tests, zizmor / Scorecard, Hadolint `DL3002` |
| [SEC-SECRET-001](security-baseline/SEC-SECRET-001-no-hardcoded-secrets.md) | No hard-coded secrets | MUST | security | Gitleaks, GitHub push protection, Ruff `S105`–`S107`, gosec `G101` |
| [SEC-LOG-001](security-baseline/SEC-LOG-001-sensitive-data.md) | No sensitive data in logs | MUST | security | Log-capture tests, `SecretStr`/`LogValuer`, CodeQL |
| [SEC-DEP-001](security-baseline/SEC-DEP-001-dependency-scanning.md) | Pin and scan dependencies | MUST | security | `pip-audit`, `npm audit`, `govulncheck`, dependency-review-action |

The full catalogue (32 rules, including those without a separate example file) is in [RULES_GUIDE.md § 13 Rule catalogue](../../RULES_GUIDE.md#13-rule-catalogue).

## Using these files with an AI agent

1. Include the relevant stack directory plus `security-baseline/` in the agent's context (e.g. via a project instruction file).
2. Instruct the agent: *"Follow every MUST rule. If a change cannot satisfy a MUST rule, stop and report the rule ID instead of working around it."*
3. Ask the agent to cite rule IDs in its PR description for any rule it relied on or requests an exception for.
