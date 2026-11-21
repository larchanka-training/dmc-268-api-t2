# PY-API-001: Validate all request input with Pydantic models

- **Severity:** MUST
- **Category:** security
- **Applies to:** Python / FastAPI — HTTP layer (path, query, header, cookie and body parameters)

## Rule

Every value an endpoint receives from a client MUST be declared as a typed parameter or a Pydantic model with explicit constraints (length, range, format, enum). Request bodies MUST be Pydantic models configured with `extra="forbid"`. Handlers MUST NOT read raw input via `await request.json()`, `request.query_params[...]` or `request.form()` and then use it without validation.

## Why

- FastAPI validates declared parameters before the handler runs and returns `422` for invalid input, so the handler only sees well-formed data.
- Unbounded strings, lists and integers enable resource exhaustion and are a common precondition for injection (OWASP A05:2025 Injection, API4:2023 Unrestricted Resource Consumption).
- `extra="forbid"` blocks mass-assignment (OWASP API3:2023 Broken Object Property Level Authorization): a client cannot smuggle `is_admin=true` into a model that is later spread into an ORM object.

## Good example

```python
from typing import Annotated, Literal
from uuid import UUID

from fastapi import FastAPI, Path, Query
from pydantic import BaseModel, ConfigDict, EmailStr, Field  # EmailStr needs the "email-validator" package

app = FastAPI()


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: EmailStr
    display_name: str = Field(min_length=1, max_length=64)
    role: Literal["viewer", "editor"] = "viewer"
    tags: list[Annotated[str, Field(max_length=32)]] = Field(default_factory=list, max_length=10)


class UserRead(BaseModel):
    id: UUID
    email: EmailStr
    display_name: str
    role: str


@app.post("/users", status_code=201)
async def create_user(payload: UserCreate) -> UserRead:
    ...


@app.get("/users/{user_id}")
async def get_user(user_id: Annotated[UUID, Path()]) -> UserRead:
    ...


@app.get("/users")
async def list_users(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    sort: Literal["created_at", "email"] = "created_at",
) -> list[UserRead]:
    ...
```

## Bad example

```python
from fastapi import FastAPI, Request

app = FastAPI()


@app.post("/users")
async def create_user(request: Request) -> dict:
    data = await request.json()          # no schema, no limits, no type checks
    user = User(**data)                  # mass assignment: client controls every column
    ...


@app.get("/users")
async def list_users(limit: int = 20, sort: str = "created_at") -> list[dict]:
    # limit is unbounded (limit=10_000_000), sort is free text later used in ORDER BY
    ...
```

## Enforcement

| Method | How |
|---|---|
| Test (pytest) | For each endpoint, send an unknown field, an out-of-range value and an oversized string; assert `422`. |
| SAST (Semgrep, custom rule) | Flag raw body access in route handlers (see rule below). |
| Code review | Reviewer checks that every `str`/`int`/`list` field has a bound and every body model sets `extra="forbid"`. |

```python
from fastapi.testclient import TestClient


def test_create_user_rejects_unknown_field(client: TestClient) -> None:
    response = client.post(
        "/users",
        json={"email": "a@example.com", "display_name": "A", "is_admin": True},
    )
    assert response.status_code == 422


def test_list_users_rejects_huge_limit(client: TestClient) -> None:
    assert client.get("/users", params={"limit": 1_000_000}).status_code == 422
```

```yaml
# semgrep rule (custom, store under .semgrep/)
rules:
  - id: fastapi-raw-request-json
    languages: [python]
    severity: WARNING
    message: "Read the body through a Pydantic model parameter instead of request.json() (PY-API-001)"
    pattern: await $REQ.json()
```

## References

- FastAPI — Request Body: https://fastapi.tiangolo.com/tutorial/body/
- FastAPI — Query Parameters and String Validations: https://fastapi.tiangolo.com/tutorial/query-params-str-validations/
- FastAPI — Query Parameter Models (`extra="forbid"`): https://fastapi.tiangolo.com/tutorial/query-param-models/
- Pydantic — Models / Configuration: https://pydantic.dev/docs/validation/latest/concepts/models/ , https://pydantic.dev/docs/validation/latest/api/pydantic/config/
- OWASP Input Validation Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html
- OWASP API Security Top 10 (2023) — API3, API4: https://api-security.owasp.org/editions/2023/en/0x11-t10/
- Related: [SEC-INPUT-001](../security-baseline/SEC-INPUT-001-validation.md)
