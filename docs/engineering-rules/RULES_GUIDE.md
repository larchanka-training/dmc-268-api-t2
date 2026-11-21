# Engineering Rules Guide

Version: 1.0.0 · Status: Active · Owners: Engineering + Security

This guide defines verifiable engineering rules for four areas: **Python / FastAPI**, **TypeScript / React**, **Go** and a cross-cutting **Security Baseline**. Detailed, copy-ready versions of selected rules live in [`examples/rules/`](examples/rules/README.md).

## Contents

1. [Purpose and scope](#1-purpose-and-scope)
2. [How to read and apply the rules](#2-how-to-read-and-apply-the-rules)
3. [Severity levels: MUST, SHOULD, MAY](#3-severity-levels-must-should-may)
4. [Rule format](#4-rule-format)
5. [General engineering principles](#5-general-engineering-principles)
6. [Python / FastAPI](#6-python--fastapi)
7. [TypeScript / React](#7-typescript--react)
8. [Go](#8-go)
9. [Security Baseline](#9-security-baseline)
10. [Rule matrix: severity × category](#10-rule-matrix-severity--category)
11. [Pre-merge checklist](#11-pre-merge-checklist)
12. [Exceptions and rule updates](#12-exceptions-and-rule-updates)
13. [Rule catalogue](#13-rule-catalogue)
14. [Top 10 rules for the first rollout](#14-top-10-rules-for-the-first-rollout)
15. [CI recommendations](#15-ci-recommendations)

---

## 1. Purpose and scope

**Purpose.** Give engineers, reviewers, CI pipelines and AI coding agents one shared, testable definition of "acceptable code". Every rule is:

- **Atomic** — it describes one behavior; a reviewer can say "violated" or "not violated" for a given diff.
- **Verifiable** — every MUST names an automated check or an explicit manual review step.
- **Grounded** — rules reference official documentation (FastAPI, Pydantic, Python, TypeScript, React, Go) and OWASP/CWE.

**In scope.**

| Area | Covers |
|---|---|
| Python / FastAPI | HTTP API services built with FastAPI, Pydantic v2, SQLAlchemy 2.x, pytest |
| TypeScript / React | Browser applications built with TypeScript and React (function components, hooks) |
| Go | Services, CLIs and libraries built with Go modules (Go 1.22+) |
| Security Baseline | Applies to **every** repository regardless of language |

**Out of scope.** Infrastructure-as-code style guides, mobile apps, data-science notebooks (except SEC-* rules, which still apply), and product/UX conventions.

---

## 2. How to read and apply the rules

1. **Find applicable rules.** All `SEC-*` rules apply to every repository. Stack rules apply when the *Applies to* field matches the code being changed.
2. **Precedence.** If two rules conflict, the stricter one wins. Security Baseline rules take precedence over stack rules. A more specific stack rule refines (never weakens) a baseline rule.
3. **New code vs. legacy code.** All rules apply to new and modified code. Existing code is brought into compliance when touched ("leave it better") or through a tracked remediation plan; CI MAY run new checks only on changed files during migration.
4. **Code review.** Reviewers cite rule IDs in comments (e.g. `GO-ERR-001: error from Close() is ignored`). An unresolved MUST violation blocks merge.
5. **CI.** Automated checks are the primary enforcement. If a rule is enforced by CI, reviewers do not need to re-check it manually.
6. **AI agents.** Agents MUST treat MUST rules as hard constraints. If a task cannot be completed without violating a MUST rule, the agent MUST stop and report the rule ID rather than work around it. Agents SHOULD cite the rule IDs they applied in PR descriptions.

---

## 3. Severity levels: MUST, SHOULD, MAY

The key words are interpreted as described in [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119) and [RFC 8174](https://www.rfc-editor.org/rfc/rfc8174) when written in capitals.

| Level | Meaning | Merge impact | Deviation requires |
|---|---|---|---|
| **MUST** / **MUST NOT** | Absolute requirement. Violations are defects. | Blocks merge. | Formal exception (§12) with owner, approver and expiry. |
| **SHOULD** / **SHOULD NOT** | Strong default. Valid reasons to deviate may exist. | Does not block if justified. | Justification in the PR description or an inline comment. |
| **MAY** | Explicitly permitted option. | None. | Nothing. |

Every MUST in this guide has at least one verification method. SHOULD rules have one where practical. MAY statements appear inside rules to explicitly allow alternatives (e.g. "you MAY use route loaders instead of client fetching").

---

## 4. Rule format

Each rule in this guide uses the same fields:

| Field | Content |
|---|---|
| **ID** | `<STACK>-<AREA>-<NNN>`; permanent, never reused (e.g. `PY-SEC-002`) |
| **Title** | Imperative summary |
| **Severity / Category** | MUST·SHOULD·MAY / correctness·security·performance·maintainability |
| **Context** | Where and when the rule applies |
| **Rule** | The requirement, using MUST / SHOULD / MAY |
| **Why** | Risk prevented or benefit gained |
| **Correct** | Minimal compliant code |
| **Incorrect** | Minimal non-compliant code (unsafe code appears only here) |
| **Verification** | Linter rule, test, CI job or review step |
| **References** | Official documentation, OWASP, CWE |

Prefixes: `PY` Python/FastAPI · `TS` TypeScript/React · `GO` Go · `SEC` Security Baseline.
Areas: `API`, `CODE`, `ERR`, `SEC`, `REACT`, `STATE`, `FMT`, `CTX`, `CONC`, `TEST`, `INPUT`, `AUTHZ`, `SECRET`, `LOG`, `DEP`, `CRYPTO`, `TRANSPORT`.

Rules marked **📄 Detailed example** have a full file in `examples/rules/`.

---

## 5. General engineering principles

These principles explain *why* the rules look the way they do. They are not enforced individually; the numbered rules are.

1. **Automate first.** A rule that a tool can check is configured in CI, not left to review. Review time is for design, logic and what tools cannot see.
2. **Secure by default.** Defaults deny access, validate input, encode output and fail closed. Insecure behavior requires explicit, reviewed opt-in.
3. **Make invalid states unrepresentable.** Prefer types, schemas and enums over comments and conventions.
4. **Validate at boundaries, trust inside.** Validate and normalize data once where it enters the system; internal code works with typed, validated values.
5. **Fail fast and loudly.** Missing configuration, invalid input and unexpected errors surface immediately, not as corrupted state later.
6. **Explicit over implicit.** Dependencies, permissions, timeouts, error handling and public contracts are visible in code.
7. **Small, reviewable changes.** A PR does one thing, includes its tests, and can be reverted on its own.
8. **Tests encode behavior.** Every bug fix adds a regression test; every security rule has at least one negative test.
9. **Reproducible builds.** Lockfiles, pinned tool versions and deterministic CI.
10. **Observable, not leaky.** Log enough to debug and audit, never secrets or unnecessary personal data.

---

## 6. Python / FastAPI

Baseline tooling: Ruff (lint + format), mypy (strict), pytest, pip-audit. Python 3.10+, Pydantic v2.

### PY-API-001 · Validate request input with Pydantic models
**Severity:** MUST · **Category:** security · 📄 [Detailed example](examples/rules/python-fastapi/PY-API-001-input-validation.md)

- **Context:** All FastAPI path operations; path, query, header, cookie and body parameters.
- **Rule:** Every client-supplied value MUST be a typed parameter or Pydantic field with explicit bounds. Body models MUST set `extra="forbid"`. Handlers MUST NOT use `await request.json()` / `request.form()` output without validating it through a model.
- **Why:** Prevents mass assignment, injection preconditions and unbounded resource use (OWASP API3/API4:2023).

```python
# Correct
class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=64)

@app.get("/users")
async def list_users(limit: Annotated[int, Query(ge=1, le=100)] = 20) -> list[UserRead]: ...
```

```python
# Incorrect
@app.post("/users")
async def create_user(request: Request) -> dict:
    data = await request.json()
    return User(**data)  # client controls every field
```

- **Verification:** pytest negative tests (unknown field, out-of-range, oversized → `422`); Semgrep custom rule for `await $REQ.json()`; code review.
- **References:** [FastAPI Request Body](https://fastapi.tiangolo.com/tutorial/body/) · [Query validations](https://fastapi.tiangolo.com/tutorial/query-params-str-validations/) · [Pydantic models](https://pydantic.dev/docs/validation/latest/concepts/models/) · [OWASP Input Validation](https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html)

### PY-API-002 · Provide shared resources through `Depends`
**Severity:** SHOULD · **Category:** maintainability · 📄 [Detailed example](examples/rules/python-fastapi/PY-API-002-dependency-injection.md)

- **Context:** DB sessions, settings, current user, HTTP clients.
- **Rule:** Shared resources SHOULD come from dependencies declared once as `Annotated[..., Depends(...)]`; lifecycle resources SHOULD use `yield` dependencies. Handlers SHOULD NOT construct them inline.
- **Why:** Guaranteed cleanup, one wiring point, and test isolation through `app.dependency_overrides`.

```python
# Correct
def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session

DbSession = Annotated[Session, Depends(get_db)]

@app.get("/items/{item_id}")
def read_item(item_id: int, db: DbSession) -> ItemRead: ...
```

```python
# Incorrect
@app.get("/items/{item_id}")
def read_item(item_id: int) -> dict:
    db = Session(engine)  # never closed on error; cannot be overridden in tests
    ...
```

- **Verification:** Code review; tests run through `dependency_overrides`.
- **References:** [FastAPI Dependencies](https://fastapi.tiangolo.com/tutorial/dependencies/) · [Dependencies with yield](https://fastapi.tiangolo.com/tutorial/dependencies/dependencies-with-yield/) · [Testing dependencies](https://fastapi.tiangolo.com/advanced/testing-dependencies/)

### PY-API-003 · Declare a response model for every endpoint
**Severity:** MUST · **Category:** security

- **Context:** Every path operation that returns data.
- **Rule:** Each endpoint MUST declare its response schema via a return type annotation or `response_model`. Internal models (with password hashes, internal flags, other tenants' data) MUST NOT be the declared response type. Endpoints MUST NOT return raw ORM objects or `dict`s without a declared schema.
- **Why:** FastAPI filters output to the declared model; without it, every attribute added to an internal model later is silently exposed (OWASP API3:2023 Excessive Data Exposure).

```python
# Correct — FastAPI filters the subclass down to the declared type
class UserRead(BaseModel):
    id: UUID
    email: EmailStr

class UserInDB(UserRead):
    hashed_password: str

@app.get("/users/{user_id}")
def get_user(user_id: UUID, db: DbSession) -> UserRead:
    return load_user(db, user_id)  # returns UserInDB; hashed_password is not serialized
```

```python
# Incorrect
@app.get("/users/{user_id}")
def get_user(user_id: UUID, db: DbSession):
    return db.get(User, user_id).__dict__  # leaks hashed_password and internal fields
```

- **Verification:** pytest asserts exact response keys for each endpoint; review checks every route has a return annotation or `response_model`; mypy flags unannotated functions under `strict` (see PY-CODE-001).
- **References:** [FastAPI Response Model — Return Type](https://fastapi.tiangolo.com/tutorial/response-model/) · [OWASP API3:2023](https://api-security.owasp.org/editions/2023/en/0xa3-broken-object-property-level-authorization/)

### PY-API-004 · Do not block the event loop in `async def` endpoints
**Severity:** SHOULD · **Category:** performance

- **Context:** Path operations, dependencies and middleware declared `async def`.
- **Rule:** `async def` code SHOULD NOT call blocking I/O (`requests`, `time.sleep`, synchronous DB drivers, blocking file I/O). Use async libraries (e.g. `httpx.AsyncClient`) or declare the endpoint with plain `def`, which FastAPI runs in a threadpool. CPU-heavy work SHOULD be moved out of the request path.
- **Why:** One blocking call in `async def` stalls every concurrent request on that worker.

```python
# Correct
@app.get("/rates")
async def rates(client: Annotated[httpx.AsyncClient, Depends(get_http_client)]) -> Rates:
    resp = await client.get("https://api.example.com/rates", timeout=5.0)
    resp.raise_for_status()
    return Rates.model_validate(resp.json())

@app.get("/report")
def report(db: DbSession) -> Report:  # sync DB session -> plain def (threadpool)
    ...
```

```python
# Incorrect
@app.get("/rates")
async def rates() -> dict:
    return requests.get("https://api.example.com/rates").json()  # blocks the event loop, no timeout
```

- **Verification:** Ruff `ASYNC` rules (e.g. `ASYNC210` blocking HTTP call, `ASYNC251` `time.sleep` in async function); load test in staging.
- **References:** [FastAPI Concurrency and async/await](https://fastapi.tiangolo.com/async/) · [Ruff rules](https://docs.astral.sh/ruff/rules/)

### PY-ERR-001 · Map errors to explicit HTTP responses; never swallow or leak them
**Severity:** MUST · **Category:** correctness · 📄 [Detailed example](examples/rules/python-fastapi/PY-ERR-001-error-handling.md)

- **Context:** Handlers, services, exception handlers.
- **Rule:** Expected failures MUST raise `HTTPException` or domain exceptions mapped by `@app.exception_handler`. Code MUST NOT use bare `except:` or discard exceptions. Responses MUST NOT include tracebacks or `str(exc)` of unexpected errors. Re-raises MUST chain (`raise ... from exc`). Production MUST NOT run with `debug=True`.
- **Why:** Swallowed errors corrupt state silently; leaked internals aid attackers (CWE-209).

```python
# Correct
if order is None:
    raise HTTPException(status_code=404, detail="Order not found")
try:
    gateway.charge(order.id)
except gateway.TimeoutError as exc:
    raise PaymentGatewayError("payment gateway timed out") from exc
```

```python
# Incorrect
try:
    charge(order_id)
except Exception as exc:
    return {"error": str(exc)}  # 200 OK with internal details
```

- **Verification:** Ruff `E722`, `BLE001`, `S110`, `S112`, `B904`; pytest status-code tests and a "no traceback in 500 body" test.
- **References:** [FastAPI Handling Errors](https://fastapi.tiangolo.com/tutorial/handling-errors/) · [OWASP Error Handling](https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html)

### PY-SEC-001 · Authenticate every non-public endpoint and authorize every object access
**Severity:** MUST · **Category:** security · 📄 [Detailed example](examples/rules/python-fastapi/PY-SEC-001-authz.md)

- **Context:** All routers and endpoints.
- **Rule:** Non-public endpoints MUST depend on an authentication dependency (preferably at `APIRouter(dependencies=[...])` level). Public endpoints MUST be an explicit allowlist. Access by ID MUST be scoped to the caller (owner/tenant). Roles MUST be checked server-side.
- **Why:** BOLA/IDOR is API1:2023; FastAPI endpoints are public unless a security dependency is declared.

```python
# Correct
router = APIRouter(prefix="/invoices", dependencies=[Depends(get_current_user)])

@router.get("/{invoice_id}")
def get_invoice(invoice_id: UUID, user: CurrentUser, db: DbSession) -> InvoiceRead:
    invoice = db.scalar(select(Invoice).where(Invoice.id == invoice_id, Invoice.tenant_id == user.tenant_id))
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return InvoiceRead.model_validate(invoice, from_attributes=True)
```

```python
# Incorrect
@router.get("/{invoice_id}")
def get_invoice(invoice_id: UUID, db: DbSession) -> InvoiceRead:
    return db.get(Invoice, invoice_id)  # any caller, any tenant
```

- **Verification:** pytest authorization matrix (no token → 401, other tenant → 403/404, wrong role → 403); route-audit test that fails when a non-allowlisted route lacks the auth dependency; review.
- **References:** [FastAPI Security](https://fastapi.tiangolo.com/tutorial/security/) · [OWASP API Top 10 2023](https://api-security.owasp.org/editions/2023/en/0x11-t10/) · [OWASP Authorization](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html)

### PY-SEC-002 · Use parameterized SQL only
**Severity:** MUST · **Category:** security

- **Context:** SQLAlchemy Core/ORM, raw DB-API cursors, any SQL string.
- **Rule:** Values MUST be passed as bound parameters (ORM expressions, `text()` with `:name` binds, DB-API parameters). SQL MUST NOT be built with f-strings, `%`, `.format()` or concatenation of input. Dynamic identifiers (sort column) MUST come from an allowlist.
- **Why:** SQL injection (OWASP A05:2025 Injection, CWE-89).

```python
# Correct
stmt = select(User).where(User.email == email)
user = db.scalars(stmt).first()

rows = db.execute(text("SELECT id FROM users WHERE email = :email"), {"email": email})

SORT_COLUMNS = {"email": User.email, "created": User.created_at}
stmt = select(User).order_by(SORT_COLUMNS.get(sort, User.created_at))
```

```python
# Incorrect
db.execute(text(f"SELECT id FROM users WHERE email = '{email}'"))
cursor.execute("SELECT * FROM users ORDER BY " + sort)
```

- **Verification:** Ruff `S608` (hard-coded SQL expression built from strings); CodeQL `py/sql-injection`; review.
- **References:** [SQLAlchemy — Using textual SQL / bound parameters](https://docs.sqlalchemy.org/en/20/tutorial/dbapi_transactions.html#sending-parameters) · [OWASP Query Parameterization](https://cheatsheetseries.owasp.org/cheatsheets/Query_Parameterization_Cheat_Sheet.html)

### PY-SEC-003 · Never deserialize or evaluate untrusted data with unsafe APIs
**Severity:** MUST · **Category:** security

- **Context:** Any code processing data from requests, files, queues or caches that an attacker could influence.
- **Rule:** Code MUST NOT call `pickle.load(s)`, `marshal.loads`, `yaml.load` with an unsafe loader, `eval`, or `exec` on untrusted data. Use `json`, `yaml.safe_load`, Pydantic validation, or `ast.literal_eval` for Python literals.
- **Why:** These APIs execute arbitrary code during deserialization (OWASP A08:2025 Software or Data Integrity Failures; CWE-502, CWE-95).

```python
# Correct
config = yaml.safe_load(stream)
payload = Event.model_validate_json(raw_bytes)
```

```python
# Incorrect
obj = pickle.loads(request_body)
config = yaml.load(stream, Loader=yaml.Loader)
result = eval(user_expression)
```

- **Verification:** Ruff `S301` (pickle), `S506` (unsafe YAML load), `S307` (eval), `S102` (exec); CodeQL `py/unsafe-deserialization`.
- **References:** [Python pickle — security warning](https://docs.python.org/3/library/pickle.html) · [PyYAML safe_load](https://pyyaml.org/wiki/PyYAMLDocumentation) · [OWASP Deserialization](https://cheatsheetseries.owasp.org/cheatsheets/Deserialization_Cheat_Sheet.html)

### PY-CODE-001 · Type-annotate all code and pass mypy in strict mode
**Severity:** SHOULD · **Category:** maintainability

- **Context:** All application packages (tests MAY use a relaxed mypy section).
- **Rule:** Functions SHOULD have full parameter and return annotations, and the project SHOULD pass `mypy` with `strict = true` and the Pydantic plugin. `# type: ignore` MUST include an error code (`# type: ignore[attr-defined]`).
- **Why:** FastAPI and Pydantic are driven by type hints; static checking catches contract drift before runtime.

```toml
# Correct — pyproject.toml
[tool.mypy]
strict = true
plugins = ["pydantic.mypy"]
enable_error_code = ["ignore-without-code"]
```

```python
# Correct
def apply_discount(price_cents: int, percent: int) -> int:
    return price_cents * (100 - percent) // 100

# Incorrect
def apply_discount(price, percent):  # untyped; mypy --strict reports it
    return price * (100 - percent) / 100  # also silently returns float
```

- **Verification:** `mypy` in CI.
- **References:** [mypy configuration](https://mypy.readthedocs.io/en/stable/config_file.html) · [Pydantic mypy plugin](https://pydantic.dev/docs/validation/latest/integrations/dev-tools/mypy/) · [FastAPI Python types intro](https://fastapi.tiangolo.com/python-types/)

---

## 7. TypeScript / React

Baseline tooling: `tsc` (strict), ESLint with typescript-eslint (type-checked configs), eslint-plugin-react, eslint-plugin-react-hooks, the project's test runner, `npm audit`.

### TS-CODE-001 · Compile in strict mode and do not use `any`
**Severity:** MUST · **Category:** correctness · 📄 [Detailed example](examples/rules/typescript-react/TS-CODE-001-strict-types.md)

- **Context:** All TypeScript code.
- **Rule:** `"strict": true` MUST be enabled; explicit `any` MUST NOT be used; external data MUST be validated at runtime (not `as T`); `@ts-ignore` MUST NOT be used; CI MUST run `tsc --noEmit`.
- **Why:** `any` and unchecked casts disable the type system exactly where data is least trustworthy.

```ts
// Correct
const UserSchema = z.object({ id: z.string(), name: z.string() });
const user = UserSchema.parse(await res.json());
```

```ts
// Incorrect
const user = (await res.json()) as User;
function handle(data: any) { return data.items[0].value; }
```

- **Verification:** `tsc --noEmit`; typescript-eslint `no-explicit-any`, `no-unsafe-*`, `ban-ts-comment`.
- **References:** [TSConfig strict](https://www.typescriptlang.org/tsconfig/#strict) · [typescript-eslint no-explicit-any](https://typescript-eslint.io/rules/no-explicit-any/)

### TS-CODE-002 · Handle every promise
**Severity:** MUST · **Category:** correctness

- **Context:** Any code that creates promises: event handlers, effects, module top level.
- **Rule:** A promise MUST be awaited, returned, or given a rejection handler (`.catch`). Async functions MUST NOT be passed where a `void`-returning callback is expected unless wrapped so rejections are handled. Intentional fire-and-forget MUST be marked with `void` and still handle rejection.
- **Why:** Unhandled rejections lose errors silently or crash Node processes; un-awaited writes race with subsequent reads.

```tsx
// Correct
<button type="button" onClick={() => { save(form).catch(showError); }}>Save</button>

async function submit(): Promise<void> {
  await save(form);
  navigate("/done");
}
```

```tsx
// Incorrect
<button onClick={async () => { await save(form); }}>Save</button>  // rejection unhandled
save(form);              // floating promise
navigate("/done");       // runs before save completes
```

- **Verification:** typescript-eslint `@typescript-eslint/no-floating-promises`, `@typescript-eslint/no-misused-promises` (type-checked linting required).
- **References:** [no-floating-promises](https://typescript-eslint.io/rules/no-floating-promises/) · [no-misused-promises](https://typescript-eslint.io/rules/no-misused-promises/)

### TS-REACT-001 · Typed, immutable props contracts and pure render
**Severity:** SHOULD · **Category:** maintainability · 📄 [Detailed example](examples/rules/typescript-react/TS-REACT-001-component-contracts.md)

- **Context:** Function components.
- **Rule:** Exported components SHOULD declare a named props type; mutually exclusive props SHOULD be discriminated unions. Components MUST NOT mutate props and MUST NOT perform side effects during render (React requirement).
- **Why:** Explicit contracts surface breaking changes at compile time; purity is required for correct rendering.

```tsx
// Correct
interface UserCardProps { readonly user: Readonly<{ id: string; name: string }>; readonly onSelect: (id: string) => void }
export function UserCard({ user, onSelect }: UserCardProps) {
  return <button type="button" onClick={() => onSelect(user.id)}>{user.name}</button>;
}
```

```tsx
// Incorrect
export function UserCard(props: any) {
  props.user.name = props.user.name.trim();  // mutates props
  fetch("/api/track");                        // side effect in render
  return <div>{props.user.name}</div>;
}
```

- **Verification:** `tsc` with `readonly`; `<StrictMode>` in development; review.
- **References:** [Keeping Components Pure](https://react.dev/learn/keeping-components-pure) · [Rules of React](https://react.dev/reference/rules)

### TS-REACT-002 · Follow the Rules of Hooks
**Severity:** MUST · **Category:** correctness

- **Context:** Components and custom hooks.
- **Rule:** Hooks MUST be called only at the top level of a component or custom hook (not in conditions, loops, nested functions or after an early return). Effect/memo/callback dependency arrays MUST list every reactive value used.
- **Why:** React identifies hooks by call order; conditional calls corrupt state. Missing dependencies cause stale values.

```tsx
// Correct
function Profile({ userId }: { readonly userId: string | null }) {
  const [tab, setTab] = useState<"info" | "posts">("info");
  useEffect(() => { if (userId !== null) track(userId); }, [userId]);
  if (userId === null) return <SignIn />;
  return <Tabs value={tab} onChange={setTab} />;
}
```

```tsx
// Incorrect
function Profile({ userId }: { userId: string | null }) {
  if (userId === null) return <SignIn />;
  const [tab, setTab] = useState("info");   // hook after early return
  useEffect(() => { track(userId); }, []);  // missing dependency
  ...
}
```

- **Verification:** eslint-plugin-react-hooks `react-hooks/rules-of-hooks` (error) and `react-hooks/exhaustive-deps` (error).
- **References:** [Rules of Hooks](https://react.dev/reference/rules/rules-of-hooks) · [Removing Effect dependencies](https://react.dev/learn/removing-effect-dependencies)

### TS-REACT-003 · Use stable, unique keys in lists
**Severity:** SHOULD · **Category:** correctness

- **Context:** Any JSX produced from an array.
- **Rule:** Each list item MUST have a `key` (React requirement); the key SHOULD be a stable ID from the data. Array indexes SHOULD NOT be used as keys when items can be reordered, inserted or removed. Keys MUST NOT be generated during render (`Math.random()`, `crypto.randomUUID()`).
- **Why:** Unstable keys cause state to attach to the wrong item and force needless remounts.

```tsx
// Correct
{todos.map((todo) => <TodoItem key={todo.id} todo={todo} />)}
```

```tsx
// Incorrect
{todos.map((todo, i) => <TodoItem key={i} todo={todo} />)}           // breaks on reorder
{todos.map((todo) => <TodoItem key={Math.random()} todo={todo} />)}  // remounts every render
```

- **Verification:** eslint-plugin-react `react/jsx-key` (error), `react/no-array-index-key` (warn).
- **References:** [Rendering lists — keys](https://react.dev/learn/rendering-lists#keeping-list-items-in-order-with-key)

### TS-SEC-001 · Render untrusted data only through safe sinks
**Severity:** MUST · **Category:** security · 📄 [Detailed example](examples/rules/typescript-react/TS-SEC-001-untrusted-data.md)

- **Context:** Rendering, links, dynamic code, `postMessage`.
- **Rule:** Untrusted data MUST be rendered as JSX text. `dangerouslySetInnerHTML` MUST only receive sanitized HTML (e.g. DOMPurify). URLs from untrusted data MUST pass a scheme allowlist. `eval`/`new Function`/raw DOM HTML sinks MUST NOT receive untrusted strings. `message` handlers MUST check `event.origin`.
- **Why:** XSS (OWASP A05:2025 Injection, CWE-79).

```tsx
// Correct
const clean = DOMPurify.sanitize(html);
// eslint-disable-next-line react/no-danger -- sanitized above (TS-SEC-001)
return <article dangerouslySetInnerHTML={{ __html: clean }} />;
```

```tsx
// Incorrect
return <div dangerouslySetInnerHTML={{ __html: comment.body }} />;
return <a href={user.website}>site</a>;  // allows javascript: URLs
```

- **Verification:** `react/no-danger`, `react/jsx-no-script-url`, `react/jsx-no-target-blank`, `no-eval`, `no-new-func`, `@typescript-eslint/no-implied-eval`; CodeQL XSS queries; CSP header.
- **References:** [React dangerouslySetInnerHTML](https://react.dev/reference/react-dom/components/common#dangerously-setting-the-inner-html) · [OWASP XSS Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html)

### TS-SEC-002 · Do not store credentials in Web Storage
**Severity:** SHOULD · **Category:** security

- **Context:** Authentication in browser applications.
- **Rule:** Session identifiers, access tokens and refresh tokens SHOULD NOT be stored in `localStorage`/`sessionStorage`. Prefer server-set cookies with `HttpOnly`, `Secure` and an appropriate `SameSite` attribute (plus CSRF protection where cookies authenticate state-changing requests), or keep short-lived tokens in memory only. Secrets (API keys) MUST NOT be shipped in client bundles at all (see SEC-SECRET-001).
- **Why:** Any XSS can read Web Storage and exfiltrate long-lived credentials; `HttpOnly` cookies are not readable from JavaScript.

```ts
// Correct — the server sets: Set-Cookie: session=...; HttpOnly; Secure; SameSite=Lax; Path=/
await fetch("/api/login", {
  method: "POST",
  credentials: "same-origin",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ email, password }),
});
```

```ts
// Incorrect
localStorage.setItem("access_token", token);
```

- **Verification:** ESLint `no-restricted-syntax` with a selector such as `CallExpression[callee.object.name='localStorage'][callee.property.name='setItem'][arguments.0.value=/[Tt]oken/]`; review of auth code.
- **References:** [OWASP HTML5 Security — Local Storage](https://cheatsheetseries.owasp.org/cheatsheets/HTML5_Security_Cheat_Sheet.html#local-storage) · [OWASP Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html) · [MDN Set-Cookie](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie)

### TS-STATE-001 · Fetch server data with cancellation, explicit states and runtime validation
**Severity:** SHOULD · **Category:** correctness · 📄 [Detailed example](examples/rules/typescript-react/TS-STATE-001-data-fetching.md)

- **Context:** Components and hooks that load remote data.
- **Rule:** Server state SHOULD go through a data-fetching library (e.g. TanStack Query) or one shared hook. Effect-based fetching MUST list dependencies and cancel/ignore stale responses. Responses MUST be checked (`res.ok`) and validated. Loading and error states SHOULD be rendered explicitly. You MAY use framework loaders/Server Components instead.
- **Why:** Prevents race conditions, silent failures and contract drift.

```tsx
// Correct
const { data, error, isPending } = useQuery({
  queryKey: ["user", userId],
  queryFn: ({ signal }) => fetchUser(userId, signal),  // fetchUser checks res.ok and parses with Zod
});
```

```tsx
// Incorrect
useEffect(() => {
  fetch(`/api/users/${userId}`).then((r) => r.json()).then((d) => setUser(d as User));
}, []);  // no cleanup, no res.ok, unchecked cast, missing dependency
```

- **Verification:** `react-hooks/exhaustive-deps`; `no-floating-promises`; component tests for loading/error/success and fast prop changes.
- **References:** [Fetching data with Effects](https://react.dev/reference/react/useEffect#fetching-data-with-effects) · [TanStack Query](https://tanstack.com/query/latest/docs/framework/react/overview)

---

## 8. Go

Baseline tooling: `gofmt`/`goimports`, `go vet`, golangci-lint (v2 config), `go test -race`, `govulncheck`. Go 1.22+.

### GO-FMT-001 · Format all code with `gofmt`
**Severity:** MUST · **Category:** maintainability

- **Context:** All `.go` files.
- **Rule:** Code MUST be formatted with `gofmt` (or `goimports`, which also formats). CI MUST fail if `gofmt -l .` prints any file.
- **Why:** Removes formatting debates and diff noise; the Go ecosystem assumes gofmt output.

```sh
# Correct — CI check
out="$(gofmt -l .)"; if [ -n "$out" ]; then echo "$out"; exit 1; fi
```

```go
// Incorrect — not gofmt'd
func add(a int,b int)int{return a+b}
```

- **Verification:** `gofmt -l` in CI; golangci-lint formatters `gofmt`/`goimports`.
- **References:** [gofmt](https://pkg.go.dev/cmd/gofmt) · [Effective Go — Formatting](https://go.dev/doc/effective_go#formatting)

### GO-ERR-001 · Check, wrap and inspect errors correctly
**Severity:** MUST · **Category:** correctness · 📄 [Detailed example](examples/rules/go/GO-ERR-001-error-handling.md)

- **Context:** All Go code.
- **Rule:** Every error MUST be checked; wrap with `%w`; compare with `errors.Is`/`errors.As`; handle once (log *or* return); no `panic` for expected failures.
- **Why:** Ignored or flattened errors produce silent corruption and break error inspection.

```go
// Correct
data, err := os.ReadFile(path)
if err != nil {
	return nil, fmt.Errorf("read config %q: %w", path, err)
}
if errors.Is(err, fs.ErrNotExist) { /* ... */ }
```

```go
// Incorrect
data, _ := os.ReadFile(path)
if err == fs.ErrNotExist { /* fails for wrapped errors */ }
```

- **Verification:** golangci-lint `errcheck`, `errorlint`; `go vet`.
- **References:** [Working with Errors in Go 1.13](https://go.dev/blog/go1.13-errors) · [errors package](https://pkg.go.dev/errors)

### GO-CTX-001 · Accept and propagate `context.Context`
**Severity:** SHOULD · **Category:** correctness

- **Context:** Functions that do I/O, call other services, or may block.
- **Rule:** Such functions SHOULD take `ctx context.Context` as the first parameter and pass it down. HTTP handlers SHOULD use `r.Context()`. Outbound calls SHOULD use context-aware APIs (`http.NewRequestWithContext`, `QueryContext`). Contexts SHOULD NOT be stored in structs. `context.Background()` SHOULD only be used in `main`, initialization and tests.
- **Why:** Cancellation and deadlines propagate, so abandoned requests stop consuming resources.

```go
// Correct
func (c *Client) GetUser(ctx context.Context, id string) (*User, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+"/users/"+url.PathEscape(id), nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.http.Do(req)
	...
}
```

```go
// Incorrect
func (c *Client) GetUser(id string) (*User, error) {
	resp, err := http.Get(c.baseURL + "/users/" + id) // no cancellation, default client without timeout
	...
}
```

- **Verification:** golangci-lint `noctx` (HTTP requests without context), `contextcheck`; review.
- **References:** [context package](https://pkg.go.dev/context) · [Go Concurrency Patterns: Context](https://go.dev/blog/context) · [Code Review Comments — Contexts](https://go.dev/wiki/CodeReviewComments#contexts)

### GO-CONC-001 · Synchronize shared state; test with `-race`
**Severity:** MUST · **Category:** correctness · 📄 [Detailed example](examples/rules/go/GO-CONC-001-race-safety.md)

- **Context:** Code reachable from multiple goroutines.
- **Rule:** Shared mutable state MUST be protected (mutex, atomic, channel ownership); maps MUST NOT be written concurrently without sync; `sync` types MUST NOT be copied; CI MUST run `go test -race ./...`.
- **Why:** Data races are undefined behavior under the Go memory model.

```go
// Correct
func (c *Counter) Inc(k string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.counts[k]++
}
```

```go
// Incorrect
var cache = map[string]string{}
func handler(w http.ResponseWriter, r *http.Request) { cache[r.URL.Path] = "seen" }
```

- **Verification:** `go test -race ./...`; `go vet` (`copylocks`).
- **References:** [Data Race Detector](https://go.dev/doc/articles/race_detector) · [The Go Memory Model](https://go.dev/ref/mem)

### GO-CONC-002 · Give every goroutine a defined exit
**Severity:** SHOULD · **Category:** correctness

- **Context:** Code that starts goroutines.
- **Rule:** Every goroutine SHOULD have an owner that can stop it (context cancellation or channel close) and wait for it (`sync.WaitGroup`, `errgroup.Group`). Fan-out SHOULD be bounded (e.g. `errgroup.Group.SetLimit`). Sends on channels SHOULD NOT block forever when the receiver has gone away.
- **Why:** Leaked goroutines accumulate memory and file descriptors and hide errors.

```go
// Correct
func fetchAll(ctx context.Context, urls []string) ([]string, error) {
	g, ctx := errgroup.WithContext(ctx)
	g.SetLimit(8)
	results := make([]string, len(urls))
	for i, u := range urls { // Go 1.22+: i and u are per-iteration variables
		g.Go(func() error {
			body, err := fetch(ctx, u)
			if err != nil {
				return fmt.Errorf("fetch %s: %w", u, err)
			}
			results[i] = body
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		return nil, err
	}
	return results, nil
}
```

```go
// Incorrect
func startPoller() {
	go func() {
		for {
			poll()            // no way to stop; leaks in tests and on shutdown
			time.Sleep(time.Second)
		}
	}()
}
```

- **Verification:** `go.uber.org/goleak` in tests (`goleak.VerifyTestMain(m)`); review.
- **References:** [errgroup](https://pkg.go.dev/golang.org/x/sync/errgroup) · [Go Concurrency Patterns: Pipelines and cancellation](https://go.dev/blog/pipelines) · [goleak](https://github.com/uber-go/goleak)

### GO-SEC-001 · Build SQL only with placeholders and allowlisted identifiers
**Severity:** MUST · **Category:** security · 📄 [Detailed example](examples/rules/go/GO-SEC-001-sql-injection.md)

- **Context:** `database/sql`, pgx, sqlx.
- **Rule:** Values MUST be query arguments (`$1`/`?`); no `fmt.Sprintf`/`+` with input. Identifiers MUST come from an allowlist. `rows.Err()` MUST be checked and `rows.Close()` deferred.
- **Why:** SQL injection (CWE-89).

```go
// Correct
row := db.QueryRowContext(ctx, "SELECT id, email FROM users WHERE email = $1", email)
```

```go
// Incorrect
db.QueryRow(fmt.Sprintf("SELECT id FROM users WHERE email = '%s'", email))
```

- **Verification:** gosec `G201`/`G202`; CodeQL `go/sql-injection`; golangci-lint `rowserrcheck`, `sqlclosecheck`.
- **References:** [Avoiding SQL injection risk](https://go.dev/doc/database/sql-injection) · [OWASP SQL Injection Prevention](https://cheatsheetseries.owasp.org/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.html)

### GO-SEC-002 · Set timeouts and size limits on HTTP servers and clients
**Severity:** MUST · **Category:** security

- **Context:** `net/http` servers and outbound HTTP clients.
- **Rule:** Servers MUST be created with `http.Server` and a non-zero `ReadHeaderTimeout`; they SHOULD also set `ReadTimeout`, `WriteTimeout`, `IdleTimeout` and `MaxHeaderBytes`. `http.ListenAndServe`/`http.Serve` helpers (no timeouts) MUST NOT be used in production. Request bodies MUST be bounded with `http.MaxBytesReader`. Outbound `http.Client`s MUST set `Timeout` (or use per-request context deadlines); `http.DefaultClient` SHOULD NOT be used for external calls.
- **Why:** Missing timeouts allow Slowloris-style resource exhaustion (CWE-400) and hung outbound calls.

```go
// Correct
srv := &http.Server{
	Addr:              ":8080",
	Handler:           mux,
	ReadHeaderTimeout: 5 * time.Second,
	ReadTimeout:       15 * time.Second,
	WriteTimeout:      15 * time.Second,
	IdleTimeout:       60 * time.Second,
	MaxHeaderBytes:    1 << 20,
}
client := &http.Client{Timeout: 10 * time.Second}
```

```go
// Incorrect
log.Fatal(http.ListenAndServe(":8080", mux)) // no timeouts
resp, err := http.Get(externalURL)           // DefaultClient: no timeout
```

- **Verification:** gosec `G112` (missing `ReadHeaderTimeout`), `G114` (`net/http` serve functions without timeouts); review.
- **References:** [net/http Server](https://pkg.go.dev/net/http#Server) · [http.MaxBytesReader](https://pkg.go.dev/net/http#MaxBytesReader) · [gosec rules](https://github.com/securego/gosec#available-rules)

### GO-TEST-001 · Fuzz parsers of untrusted input
**Severity:** SHOULD · **Category:** security · 📄 [Detailed example](examples/rules/go/GO-TEST-001-fuzzing.md)

- **Context:** Functions parsing/decoding external bytes or strings.
- **Rule:** Such functions SHOULD have a native fuzz test asserting a property; found crashers MUST be committed to `testdata/fuzz/`; CI SHOULD run bounded fuzzing.
- **Why:** Finds panics, hangs and memory blowups that hand-written tests miss.

```go
// Correct
func FuzzParse(f *testing.F) {
	f.Add("a=1;b=2")
	f.Fuzz(func(t *testing.T, s string) {
		m, err := Parse(s)
		if err != nil {
			return
		}
		if _, err := Parse(Encode(m)); err != nil {
			t.Fatalf("round-trip failed for %q: %v", s, err)
		}
	})
}
```

```go
// Incorrect — fuzz target with network I/O and shared state
f.Fuzz(func(t *testing.T, s string) { http.Get("http://localhost:8080/?q=" + s); cache[s] = true })
```

- **Verification:** `go test ./...` runs seed corpus; scheduled `go test -run='^$' -fuzz='^FuzzParse$' -fuzztime=30s ./pkg`.
- **References:** [Go Fuzzing](https://go.dev/doc/security/fuzz/) · [Fuzzing tutorial](https://go.dev/doc/tutorial/fuzz)

---

## 9. Security Baseline

Applies to every repository. Mapped to OWASP Top 10 (2025) and OWASP ASVS.

### SEC-INPUT-001 · Validate all untrusted input on the server with allowlists
**Severity:** MUST · **Category:** security · 📄 [Detailed example](examples/rules/security-baseline/SEC-INPUT-001-validation.md)

- **Context:** Every trust boundary: APIs, queues, uploads, webhooks, CLI args.
- **Rule:** Validate server-side with allowlists (type, length, range, format, enum); canonicalize before validating; bound all sizes; reject (don't silently fix) invalid input; still encode output for its sink.
- **Why:** Root cause of injection, traversal and resource exhaustion (CWE-20).

```go
// Correct
r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
dec := json.NewDecoder(r.Body)
dec.DisallowUnknownFields()
```

```python
# Incorrect
if ".." in name:            # denylist only
    raise ValueError()
path = UPLOAD_DIR / name    # absolute paths still escape
```

- **Verification:** Schema validation + negative tests; gosec `G304`; CodeQL path-injection queries; review.
- **References:** [OWASP Input Validation](https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html) · [OWASP ASVS](https://asvs.owasp.org/)

### SEC-AUTHZ-001 · Deny by default and grant least privilege
**Severity:** MUST · **Category:** security · 📄 [Detailed example](examples/rules/security-baseline/SEC-AUTHZ-001-least-privilege.md)

- **Context:** App authorization, DB users, service accounts, CI tokens, containers.
- **Rule:** Deny unless explicitly granted; enforce server-side on every request including object level; each identity gets minimal permissions; CI declares minimal `permissions`; containers SHOULD run as non-root.
- **Why:** Broken Access Control is OWASP A01:2025; least privilege limits blast radius.

```yaml
# Correct — GitHub Actions
permissions:
  contents: read
```

```yaml
# Incorrect
permissions: write-all
```

- **Verification:** Authorization matrix tests; zizmor / OpenSSF Scorecard `Token-Permissions`; Hadolint `DL3002`; review.
- **References:** [OWASP Authorization](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html) · [GITHUB_TOKEN permissions](https://docs.github.com/en/actions/tutorials/authenticate-with-github_token#modifying-the-permissions-for-the-github_token)

### SEC-SECRET-001 · Never commit secrets
**Severity:** MUST · **Category:** security · 📄 [Detailed example](examples/rules/security-baseline/SEC-SECRET-001-no-hardcoded-secrets.md)

- **Context:** Code, config, Dockerfiles, CI, tests, docs, frontend bundles.
- **Rule:** No secrets in repo/images/logs; load at runtime from env or a secret manager; fail fast if missing (no fallback values); `.env` git-ignored; secret scanning in CI; leaked secrets are revoked and rotated first.
- **Why:** CWE-798; leaked credentials are a top initial-access vector.

```python
# Correct
class Settings(BaseSettings):
    api_key: SecretStr  # required, from environment
```

```python
# Incorrect
API_KEY = "<real key pasted here>"
DB_URL = os.getenv("DB_URL", "postgresql://admin:admin@db/prod")
```

- **Verification:** Gitleaks in CI; GitHub secret scanning + push protection; Ruff `S105`/`S106`/`S107`; gosec `G101`.
- **References:** [OWASP Secrets Management](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html) · [GitHub secret scanning](https://docs.github.com/en/code-security/how-tos/secure-your-secrets)

### SEC-LOG-001 · Keep sensitive data out of logs
**Severity:** MUST · **Category:** security · 📄 [Detailed example](examples/rules/security-baseline/SEC-LOG-001-sensitive-data.md)

- **Context:** Logs, traces, metrics labels, error trackers.
- **Rule:** Never log passwords, tokens, session IDs, keys, auth headers, full card data; minimize/mask PII; log field allowlists not whole objects; use structured logging; scrub error-tracking events.
- **Why:** CWE-532; logs are widely accessible and long-lived.

```go
// Correct
type Token string
func (Token) LogValue() slog.Value { return slog.StringValue("REDACTED") }
```

```python
# Incorrect
logger.info(f"login user={username} password={password}")
```

- **Verification:** Log-capture tests (`caplog`, buffer handler); `SecretStr` / `LogValuer`; CodeQL clear-text-logging queries; review.
- **References:** [OWASP Logging](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html) · [slog.LogValuer](https://pkg.go.dev/log/slog#LogValuer)

### SEC-DEP-001 · Pin dependencies and scan them for known vulnerabilities
**Severity:** MUST · **Category:** security · 📄 [Detailed example](examples/rules/security-baseline/SEC-DEP-001-dependency-scanning.md)

- **Context:** Python, npm, Go modules, base images, CI actions.
- **Rule:** Commit lockfiles; install reproducibly; scan on every PR and on a schedule; fail on High/Critical (or reachable `govulncheck` findings) unless excepted; automated updates SHOULD be enabled; actions SHOULD be SHA-pinned.
- **Why:** OWASP A03:2025 Software Supply Chain Failures.

```yaml
# Correct
- run: npm ci
- run: npm audit --audit-level=high
```

```yaml
# Incorrect
- run: npm install
- uses: some-org/some-action@main
```

- **Verification:** `pip-audit`, `npm audit`, `govulncheck`, `actions/dependency-review-action`; Dependabot/Renovate.
- **References:** [OWASP Vulnerable Dependency Management](https://cheatsheetseries.owasp.org/cheatsheets/Vulnerable_Dependency_Management_Cheat_Sheet.html) · [govulncheck](https://go.dev/doc/security/vuln/)

### SEC-CRYPTO-001 · Use vetted cryptography: strong password hashing and a CSPRNG
**Severity:** MUST · **Category:** security

- **Context:** Password storage, token/ID generation, hashing, encryption.
- **Rule:** Passwords MUST be hashed with Argon2id, scrypt or bcrypt via a maintained library (never MD5/SHA-1/SHA-256 alone, never reversible encryption). Security tokens MUST be generated with a CSPRNG (`secrets` in Python, `crypto/rand` in Go, `crypto.getRandomValues`/`crypto.randomUUID` in browsers). MD5/SHA-1 MUST NOT be used for security purposes. Custom cryptographic algorithms or protocols MUST NOT be implemented.
- **Why:** Weak hashing makes leaked password databases crackable; predictable tokens allow session/reset hijacking (OWASP A04:2025 Cryptographic Failures; CWE-916, CWE-338).

```python
# Correct (argon2-cffi)
from argon2 import PasswordHasher
ph = PasswordHasher()
stored = ph.hash(password)
ph.verify(stored, candidate)  # raises argon2.exceptions.VerifyMismatchError on mismatch

reset_token = secrets.token_urlsafe(32)
```

```go
// Correct
hash, err := bcrypt.GenerateFromPassword([]byte(password), bcrypt.DefaultCost) // golang.org/x/crypto/bcrypt
b := make([]byte, 32)
if _, err := rand.Read(b); err != nil { // crypto/rand
	return err
}
token := base64.RawURLEncoding.EncodeToString(b)
```

```python
# Incorrect
hashed = hashlib.md5(password.encode()).hexdigest()
token = "".join(random.choice(string.ascii_letters) for _ in range(16))  # predictable PRNG
```

- **Verification:** Ruff `S324` (insecure hash functions), `S311` (non-cryptographic `random`); gosec `G401` (MD5/SHA1), `G404` (`math/rand` for security), `G501`/`G505` (blocklisted crypto imports); review.
- **References:** [OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html) · [Python secrets](https://docs.python.org/3/library/secrets.html) · [Go crypto/rand](https://pkg.go.dev/crypto/rand) · [x/crypto/bcrypt](https://pkg.go.dev/golang.org/x/crypto/bcrypt)

### SEC-TRANSPORT-001 · Encrypt data in transit and verify certificates
**Severity:** MUST · **Category:** security

- **Context:** All network communication: browser ↔ API, service ↔ service, service ↔ database/broker.
- **Rule:** External traffic MUST use TLS 1.2+ (HTTPS). TLS certificate verification MUST NOT be disabled (`verify=False`, `InsecureSkipVerify: true`, `NODE_TLS_REJECT_UNAUTHORIZED=0`) outside local tests with self-signed fixtures. Public web apps SHOULD send `Strict-Transport-Security`. Internal service traffic SHOULD use TLS or mTLS.
- **Why:** Prevents interception and tampering (OWASP A04:2025 Cryptographic Failures; CWE-295, CWE-319).

```go
// Correct
tlsCfg := &tls.Config{MinVersion: tls.VersionTLS12}
```

```python
# Correct
httpx.get("https://api.example.com/rates", timeout=5.0)  # verification is on by default
```

```python
# Incorrect
requests.get(url, verify=False)
```

```go
// Incorrect
&tls.Config{InsecureSkipVerify: true}
```

- **Verification:** Ruff `S501` (request without certificate validation); gosec `G402` (insecure TLS settings); review of deployment config.
- **References:** [OWASP Transport Layer Security](https://cheatsheetseries.owasp.org/cheatsheets/Transport_Layer_Security_Cheat_Sheet.html) · [OWASP HTTP Strict Transport Security](https://cheatsheetseries.owasp.org/cheatsheets/HTTP_Strict_Transport_Security_Cheat_Sheet.html) · [Go crypto/tls](https://pkg.go.dev/crypto/tls)

---

## 10. Rule matrix: severity × category

| Category \ Severity | MUST | SHOULD | MAY | Total |
|---|---|---|---|---|
| **security** | 15 | 2 | 0 | 17 |
| **correctness** | 6 | 4 | 0 | 10 |
| **performance** | 0 | 1 | 0 | 1 |
| **maintainability** | 1 | 3 | 0 | 4 |
| **Total** | **22** | **10** | **0** | **32** |

Rule IDs per cell:

| Category | MUST | SHOULD |
|---|---|---|
| security | PY-API-001, PY-API-003, PY-SEC-001, PY-SEC-002, PY-SEC-003, TS-SEC-001, GO-SEC-001, GO-SEC-002, SEC-INPUT-001, SEC-AUTHZ-001, SEC-SECRET-001, SEC-LOG-001, SEC-DEP-001, SEC-CRYPTO-001, SEC-TRANSPORT-001 | TS-SEC-002, GO-TEST-001 |
| correctness | PY-ERR-001, TS-CODE-001, TS-CODE-002, TS-REACT-002, GO-ERR-001, GO-CONC-001 | TS-REACT-003, TS-STATE-001, GO-CTX-001, GO-CONC-002 |
| performance | — | PY-API-004 |
| maintainability | GO-FMT-001 | PY-API-002, PY-CODE-001, TS-REACT-001 |

No standalone MAY rules exist: MAY is used inside rules to explicitly allow alternatives (e.g. `@ts-expect-error` with a description in TS-CODE-001, framework loaders in TS-STATE-001, `panic` for impossible states in GO-ERR-001).

---

## 11. Pre-merge checklist

Authors complete this before requesting review; reviewers verify the items CI cannot.

**All repositories**

- [ ] CI is green: format, lint, type-check, tests, dependency scan, secret scan.
- [ ] The PR does one thing; description explains *what* and *why* and lists any rule exceptions (§12).
- [ ] New behavior has tests; bug fixes have a regression test.
- [ ] No secrets, `.env` files, keys or real personal data in the diff (SEC-SECRET-001).
- [ ] New/changed inputs are validated with bounds (SEC-INPUT-001).
- [ ] New endpoints/operations have authentication and object-level authorization (SEC-AUTHZ-001).
- [ ] New log statements log IDs, not secrets, bodies or whole objects (SEC-LOG-001).
- [ ] New dependencies are justified, pinned in the lockfile and pass the vulnerability scan (SEC-DEP-001).
- [ ] No disabled TLS verification, weak hashes or non-crypto randomness for security (SEC-TRANSPORT-001, SEC-CRYPTO-001).

**Python / FastAPI**

- [ ] `ruff format --check`, `ruff check`, `mypy`, `pytest` pass.
- [ ] Every endpoint has typed inputs with bounds and a declared response model (PY-API-001, PY-API-003).
- [ ] Non-public endpoints depend on auth; queries by ID are scoped (PY-SEC-001).
- [ ] No f-string SQL, `pickle`/unsafe YAML/`eval` on untrusted data (PY-SEC-002, PY-SEC-003).
- [ ] No blocking calls inside `async def` (PY-API-004).

**TypeScript / React**

- [ ] `tsc --noEmit` and `eslint . --max-warnings=0` pass; tests pass.
- [ ] No `any`, `@ts-ignore` or unchecked `as` on external data (TS-CODE-001).
- [ ] No floating promises (TS-CODE-002); hooks rules satisfied (TS-REACT-002).
- [ ] Any `dangerouslySetInnerHTML` is sanitized and justified; user-controlled URLs are allowlisted (TS-SEC-001).
- [ ] No tokens in `localStorage`/`sessionStorage` (TS-SEC-002).

**Go**

- [ ] `gofmt -l .` is empty; `go vet ./...`, `golangci-lint run`, `go test -race ./...`, `govulncheck ./...` pass.
- [ ] All errors handled and wrapped with `%w` (GO-ERR-001).
- [ ] Shared state synchronized; goroutines have an exit path (GO-CONC-001, GO-CONC-002).
- [ ] SQL uses placeholders (GO-SEC-001); HTTP servers/clients have timeouts (GO-SEC-002).
- [ ] New parsers have a fuzz target (GO-TEST-001).

---

## 12. Exceptions and rule updates

### 12.1 When an exception is allowed

- **SHOULD rules:** the author MAY deviate with a one-line justification in the PR description or an inline comment. No further approval needed.
- **MUST rules:** deviation requires a recorded exception approved before merge.

### 12.2 Exception record

Exceptions to MUST rules are recorded in `EXCEPTIONS.md` at the repository root (or the team's issue tracker) with:

| Field | Example |
|---|---|
| Rule ID | `GO-SEC-001` |
| Scope | `internal/report/query.go:42` (as narrow as possible) |
| Justification | Column name comes from a compile-time constant map; gosec cannot see the allowlist. |
| Compensating control | Unit test enumerating all map values. |
| Owner | Team or person responsible |
| Approved by | Code owner; **plus** security reviewer for `category: security` |
| Ticket | `SEC-123` |
| Expires | `2027-03-31` (max 90 days for security MUST rules; max 12 months otherwise) |

### 12.3 Inline suppression format

Suppressions MUST name the specific rule (never blanket-disable a linter) and reference the rule ID and justification:

```python
# RULE-EXCEPTION PY-SEC-002: identifier from SORT_COLUMNS allowlist; ticket SEC-123; expires 2027-03-31
query = "SELECT id FROM users ORDER BY " + column  # noqa: S608
```

```ts
// eslint-disable-next-line react/no-danger -- TS-SEC-001: sanitized with DOMPurify above; ticket SEC-124
```

```ts
// @ts-expect-error TS-CODE-001: upstream types missing overload; remove after lib v3 (ticket FE-88)
```

```go
//nolint:gosec // G202, GO-SEC-001: column from sortColumns allowlist; ticket SEC-125
```

Enforce the format with tooling where available: golangci-lint `nolintlint` with `require-explanation: true` and `require-specific: true`; typescript-eslint `ban-ts-comment` with `allow-with-description`; ESLint's `--report-unused-disable-directives`; mypy `enable_error_code = ["ignore-without-code"]` and `warn_unused_ignores` (part of `strict`). Dependency vulnerability exceptions use the scanner's own mechanism (e.g. `pip-audit --ignore-vuln <ID>`, `allow-ghsas` in `dependency-review-action`) and are mirrored in `EXCEPTIONS.md`.

### 12.4 Expiry and review

- CI or a scheduled job SHOULD fail/alert when an exception's expiry date has passed.
- Expired exceptions are either renewed (with fresh approval) or fixed.
- Exceptions are reviewed in the quarterly rules review.

### 12.5 Changing the rules

1. **Propose** via a PR that changes `RULES_GUIDE.md`, the matching `examples/rules/` file and CI configuration together. Include motivation, examples and the enforcement method.
2. **Review** by at least one maintainer per affected stack; security-category changes also need a security reviewer.
3. **Version** this guide with semantic versioning:
   - MAJOR — new MUST rule, or a rule's severity is raised.
   - MINOR — new SHOULD/MAY rule, new enforcement, clarified scope.
   - PATCH — wording, links, examples.
4. **Roll out** new automated MUST checks in *warn* mode for at least two weeks (report-only in CI), then switch to *error*. Announce in the team channel with migration guidance.
5. **Deprecate** rather than delete: mark the rule *Deprecated (vX.Y)*, keep the ID, remove enforcement, and never reuse the number.
6. **Review cadence:** quarterly, and after any security incident or major framework release (FastAPI, React, TypeScript, Go).

---

## 13. Rule catalogue

| ID | Title | Severity | Category | Verification |
|---|---|---|---|---|
| PY-API-001 | Validate request input with Pydantic models | MUST | security | pytest 422 tests; Semgrep custom rule; review |
| PY-API-002 | Provide shared resources through `Depends` | SHOULD | maintainability | Review; `dependency_overrides` tests |
| PY-API-003 | Declare a response model for every endpoint | MUST | security | pytest response-key tests; review; mypy |
| PY-API-004 | Do not block the event loop in `async def` | SHOULD | performance | Ruff `ASYNC` rules; load test |
| PY-ERR-001 | Map errors to HTTP responses; never swallow or leak | MUST | correctness | Ruff `E722`, `BLE001`, `S110`, `S112`, `B904`; pytest |
| PY-SEC-001 | Authenticate every endpoint, authorize every object | MUST | security | pytest authz matrix; route-audit test |
| PY-SEC-002 | Parameterized SQL only | MUST | security | Ruff `S608`; CodeQL `py/sql-injection` |
| PY-SEC-003 | No unsafe deserialization / eval | MUST | security | Ruff `S301`, `S506`, `S307`, `S102`; CodeQL |
| PY-CODE-001 | Type-annotate; pass mypy strict | SHOULD | maintainability | `mypy` |
| TS-CODE-001 | Strict mode, no `any` | MUST | correctness | `tsc --noEmit`; `no-explicit-any`, `no-unsafe-*` |
| TS-CODE-002 | Handle every promise | MUST | correctness | `no-floating-promises`, `no-misused-promises` |
| TS-REACT-001 | Typed, immutable props; pure render | SHOULD | maintainability | `tsc`; StrictMode; review |
| TS-REACT-002 | Rules of Hooks | MUST | correctness | `react-hooks/rules-of-hooks`, `react-hooks/exhaustive-deps` |
| TS-REACT-003 | Stable, unique list keys | SHOULD | correctness | `react/jsx-key`, `react/no-array-index-key` |
| TS-SEC-001 | Render untrusted data only through safe sinks | MUST | security | `react/no-danger`, `react/jsx-no-script-url`, `no-eval`; CodeQL |
| TS-SEC-002 | No credentials in Web Storage | SHOULD | security | `no-restricted-syntax`; review |
| TS-STATE-001 | Fetch with cancellation, states, validation | SHOULD | correctness | `exhaustive-deps`; component tests |
| GO-FMT-001 | Format with gofmt | MUST | maintainability | `gofmt -l` |
| GO-ERR-001 | Check, wrap, inspect errors | MUST | correctness | `errcheck`, `errorlint`, `go vet` |
| GO-CTX-001 | Accept and propagate `context.Context` | SHOULD | correctness | `noctx`, `contextcheck`; review |
| GO-CONC-001 | Synchronize shared state; `-race` | MUST | correctness | `go test -race`; `go vet` copylocks |
| GO-CONC-002 | Every goroutine has a defined exit | SHOULD | correctness | goleak; review |
| GO-SEC-001 | Placeholders and allowlisted identifiers in SQL | MUST | security | gosec `G201`/`G202`; CodeQL |
| GO-SEC-002 | HTTP timeouts and size limits | MUST | security | gosec `G112`, `G114`; review |
| GO-TEST-001 | Fuzz parsers of untrusted input | SHOULD | security | `go test -fuzz` CI job; review |
| SEC-INPUT-001 | Server-side allowlist validation | MUST | security | Schemas + negative tests; gosec `G304`; CodeQL |
| SEC-AUTHZ-001 | Deny by default, least privilege | MUST | security | Authz tests; zizmor/Scorecard; Hadolint `DL3002` |
| SEC-SECRET-001 | Never commit secrets | MUST | security | Gitleaks; push protection; Ruff `S105`–`S107`; gosec `G101` |
| SEC-LOG-001 | No sensitive data in logs | MUST | security | Log-capture tests; CodeQL; review |
| SEC-DEP-001 | Pin and scan dependencies | MUST | security | `pip-audit`, `npm audit`, `govulncheck`, dependency review |
| SEC-CRYPTO-001 | Vetted crypto, strong password hashing, CSPRNG | MUST | security | Ruff `S324`, `S311`; gosec `G401`, `G404`, `G501` |
| SEC-TRANSPORT-001 | TLS everywhere, verify certificates | MUST | security | Ruff `S501`; gosec `G402`; review |

---

## 14. Top 10 rules for the first rollout

Chosen for highest risk reduction per unit of effort: each is mostly automatable on day one.

| # | Rule | Why first | Day-one enforcement |
|---|---|---|---|
| 1 | **SEC-SECRET-001** No committed secrets | A single leak is an immediate breach; cheap to detect | Gitleaks in CI + GitHub push protection |
| 2 | **SEC-DEP-001** Pin and scan dependencies | Known CVEs are the easiest attacks to automate | `pip-audit` / `npm audit` / `govulncheck` + Dependabot |
| 3 | **PY-SEC-001** Authn + object-level authz (FastAPI) | BOLA is the #1 API risk | Route-audit test + authz matrix tests |
| 4 | **PY-SEC-002** Parameterized SQL (Python) | Injection = full data compromise | Ruff `S608` |
| 5 | **GO-SEC-001** Parameterized SQL (Go) | Same risk in Go services | gosec `G201`/`G202` via golangci-lint |
| 6 | **TS-SEC-001** Safe rendering of untrusted data | XSS compromises every user session | `react/no-danger`, `react/jsx-no-script-url`, `no-eval` |
| 7 | **PY-API-001** Pydantic validation with bounds | Closes mass assignment and resource exhaustion | pytest negative tests + review |
| 8 | **TS-CODE-001** Strict TypeScript, no `any` | Foundation for every other TS rule | `tsc --noEmit` + `no-explicit-any` |
| 9 | **GO-ERR-001** Check and wrap errors | Silent failures are the most common Go defect | `errcheck` + `errorlint` |
| 10 | **GO-CONC-001** Race-free code, `-race` in CI | Races cause rare, severe production bugs | `go test -race ./...` |

Suggested sequence: week 1 — rules 1–2 (repository-wide, no code changes needed); weeks 2–3 — rules 4, 5, 6, 8, 9, 10 in *warn* mode, then *error*; weeks 3–4 — rules 3 and 7 (need tests written per service).

---

## 15. CI recommendations

All examples are for GitHub Actions; the commands work the same in other CI systems. Action and tool versions shown were verified against their latest releases on 2026-11-21 — pin them to a commit SHA in production (SEC-DEP-001) and let Dependabot update them.

### 15.1 Python: Ruff, mypy, pytest, pip-audit

```toml
# pyproject.toml
[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = [
  "E", "F", "W",   # pycodestyle / Pyflakes
  "I",             # isort
  "B",             # flake8-bugbear (incl. B904)
  "BLE",           # blind except
  "S",             # flake8-bandit (security)
  "ASYNC",         # blocking calls in async code
  "UP",            # pyupgrade
]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101"]  # assert is expected in tests

[tool.mypy]
strict = true
plugins = ["pydantic.mypy"]
enable_error_code = ["ignore-without-code"]
```

```yaml
  python:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with: { python-version: "3.12" }
      - run: pip install -r requirements-dev.txt   # pinned ruff, mypy, pytest, pip-audit
      - run: ruff format --check .
      - run: ruff check .
      - run: mypy .
      - run: pytest -q
      - run: pip-audit -r requirements.txt
```

### 15.2 TypeScript / React: tsc, ESLint, tests, npm audit

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

```js
// eslint.config.js
import js from "@eslint/js";
import { defineConfig } from "eslint/config";
import tseslint from "typescript-eslint";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";

export default defineConfig(
  js.configs.recommended,
  tseslint.configs.strictTypeChecked,
  reactHooks.configs.flat.recommended,
  {
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
    settings: { react: { version: "detect" } },
    plugins: { react },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "error",  // the preset uses "warn"; TS-REACT-002 is MUST
      "react/jsx-key": "error",
      "react/no-array-index-key": "warn",
      "react/no-danger": "error",
      "react/jsx-no-script-url": "error",
      "react/jsx-no-target-blank": "error",
      "no-eval": "error",
      "no-new-func": "error",
      "@typescript-eslint/ban-ts-comment": ["error", { "ts-expect-error": "allow-with-description" }],
    },
  },
);
```

Version compatibility to check when pinning tool versions (from each package's `peerDependencies` at the time of writing):

- **typescript-eslint 8.x** supports TypeScript `>=4.8.4 <6.1.0`. Typed linting (required for `no-unsafe-*`, `no-floating-promises`, `no-misused-promises`) does not support TypeScript 7 yet. Pin `typescript` to 6.0.x for linting until it does.
- **eslint-plugin-react 7.37.x** supports ESLint up to 9.x. Pin `eslint` to 9.x while using it, or move the `react/*` rules to a plugin that supports ESLint 10.
- `tseslint.config()` is deprecated; use `defineConfig` from `eslint/config` as shown.

```yaml
  web:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-node@v7
        with: { node-version: "24", cache: "npm" }
      - run: npm ci
      - run: npx tsc --noEmit
      - run: npx eslint . --max-warnings=0 --report-unused-disable-directives
      - run: npm test
      - run: npm audit --audit-level=high
```

### 15.3 Go: gofmt, go vet, golangci-lint, race tests, govulncheck, fuzzing

```yaml
# .golangci.yml (golangci-lint v2 format)
version: "2"
linters:
  default: standard        # errcheck, govet, ineffassign, staticcheck, unused
  enable:
    - gosec
    - errorlint
    - noctx
    - contextcheck
    - bodyclose
    - rowserrcheck
    - sqlclosecheck
    - nolintlint
  settings:
    nolintlint:
      require-explanation: true
      require-specific: true
formatters:
  enable:
    - gofmt
    - goimports
```

```yaml
  go:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-go@v7
        with: { go-version-file: go.mod }
      - name: gofmt
        run: |
          out="$(gofmt -l .)"
          if [ -n "$out" ]; then echo "Unformatted files:"; echo "$out"; exit 1; fi
      - run: go vet ./...
      - uses: golangci/golangci-lint-action@v9
        with: { version: v2.15.0 }  # pin; update via Dependabot/Renovate
      - run: go test -race ./...
      - run: go install golang.org/x/vuln/cmd/govulncheck@latest
      - run: govulncheck ./...

  go-fuzz:                          # scheduled, not on every PR
    if: github.event_name == 'schedule'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-go@v7
        with: { go-version-file: go.mod }
      - run: go test ./internal/kv -run='^$' -fuzz='^FuzzParse$' -fuzztime=5m
```

### 15.4 SAST, secret scanning and dependency review (all repositories)

```yaml
name: security
on:
  pull_request:
  push: { branches: [main] }
  schedule: [{ cron: "0 6 * * 1" }]
permissions:
  contents: read

jobs:
  secrets:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with: { fetch-depth: 0 }
      - uses: gitleaks/gitleaks-action@v3
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          # Organization-owned repositories also need GITLEAKS_LICENSE (see the action's README).

  codeql:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      security-events: write
      actions: read
    strategy:
      matrix:
        include:
          - language: python
            build-mode: none
          - language: javascript-typescript
            build-mode: none
          - language: go
            build-mode: autobuild
    steps:
      - uses: actions/checkout@v7
      - uses: github/codeql-action/init@v4
        with:
          languages: ${{ matrix.language }}
          build-mode: ${{ matrix.build-mode }}
          queries: security-extended
      - uses: github/codeql-action/analyze@v4

  semgrep:
    runs-on: ubuntu-latest
    container: semgrep/semgrep
    steps:
      - uses: actions/checkout@v7
      - run: semgrep scan --config p/default --config .semgrep/ --error

  dependency-review:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/dependency-review-action@v5
        with:
          fail-on-severity: high
```

Additional recommendations:

- **Required checks.** Mark the lint, type-check, test, dependency-scan and secret-scan jobs as required status checks in branch protection.
- **Fast feedback.** Run Ruff, ESLint, gofmt and Gitleaks as pre-commit hooks locally; CI remains the source of truth.
- **Changed-files mode during migration.** When adopting a rule on a legacy codebase, golangci-lint `--new-from-rev=origin/main` and running Ruff/ESLint only on changed files let new code comply without blocking on old findings.
- **Container images.** Scan built images (e.g. Trivy) and lint Dockerfiles (Hadolint) where containers are produced.
- **Workflow security.** Run `zizmor` on `.github/workflows/` to catch excessive permissions and injection in workflow expressions (SEC-AUTHZ-001).
- **Tool versions.** Pin linter and scanner versions (requirements-dev lockfile, `package-lock.json`, explicit golangci-lint version) so CI results are reproducible; update them through Dependabot/Renovate PRs.

---

## References (general)

- RFC 2119 / RFC 8174 — Requirement levels: https://www.rfc-editor.org/rfc/rfc2119 · https://www.rfc-editor.org/rfc/rfc8174
- OWASP Top 10: https://owasp.org/Top10/2025/
- OWASP API Security Top 10 (2023): https://api-security.owasp.org/editions/2023/en/0x11-t10/
- OWASP ASVS: https://asvs.owasp.org/
- OWASP Cheat Sheet Series: https://cheatsheetseries.owasp.org/
- FastAPI: https://fastapi.tiangolo.com/ · Pydantic: https://pydantic.dev/docs/validation/latest/get-started/ · Ruff: https://docs.astral.sh/ruff/ · mypy: https://mypy.readthedocs.io/
- TypeScript: https://www.typescriptlang.org/docs/ · typescript-eslint: https://typescript-eslint.io/ · React: https://react.dev/
- Go: https://go.dev/doc/ · golangci-lint: https://golangci-lint.run/ · gosec: https://github.com/securego/gosec · govulncheck: https://go.dev/doc/security/vuln/
