# PY-API-002: Provide shared resources through `Depends`

- **Severity:** SHOULD
- **Category:** maintainability
- **Applies to:** Python / FastAPI — routers, endpoints, service wiring

## Rule

Database sessions, settings, authenticated users, HTTP clients and other shared resources SHOULD be obtained through FastAPI dependencies (`Depends`), declared once as `Annotated` aliases and reused. Resources with a lifecycle (sessions, transactions) SHOULD use dependencies with `yield` so cleanup always runs. Handlers SHOULD NOT create such resources inline or import mutable module-level singletons.

## Why

- One place defines how a resource is created and released; handlers stay small and declarative.
- `yield` dependencies guarantee the session is closed even if the handler raises.
- `app.dependency_overrides` lets tests swap the database, settings or current user without monkey-patching.
- Auth dependencies attached to routers make it hard to forget authentication on a new endpoint (see [PY-SEC-001](PY-SEC-001-authz.md)).

## Good example

```python
from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_")

    database_url: SecretStr


@lru_cache
def get_settings() -> Settings:
    return Settings()  # read once from the environment


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    engine = create_engine(get_settings().database_url.get_secret_value(), pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with get_sessionmaker()() as session:  # closed even if the handler raises
        yield session


DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]

app = FastAPI()


@app.get("/items/{item_id}")
def read_item(item_id: int, db: DbSession) -> ItemRead:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return ItemRead.model_validate(item, from_attributes=True)
```

Test using an override:

```python
from collections.abc import Iterator

from fastapi.testclient import TestClient


def override_get_db() -> Iterator[Session]:
    with TestingSessionLocal() as session:
        yield session


def test_read_item_not_found() -> None:
    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        assert client.get("/items/999").status_code == 404
    finally:
        app.dependency_overrides.clear()
```

## Bad example

```python
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

engine = create_engine(os.environ["DATABASE_URL"])
session = Session(engine)          # one shared session for all requests: not thread-safe, never closed


@app.get("/items/{item_id}")
def read_item(item_id: int) -> dict:
    db = Session(engine)            # created inline, not closed on error, impossible to override in tests
    item = db.get(Item, item_id)
    return {"id": item.id, "name": item.name}
```

## Enforcement

| Method | How |
|---|---|
| Code review | No `Session(...)`, `sessionmaker()()`, `Settings()` or `httpx.Client()` construction inside route functions. |
| Test (pytest) | Test suite runs against `app.dependency_overrides`; if a handler cannot be overridden, the test cannot isolate it — this surfaces violations. |
| grep in CI (optional) | `grep -rn "Session(" app/routers/` returning matches fails the job. |

## References

- FastAPI — Dependencies: https://fastapi.tiangolo.com/tutorial/dependencies/
- FastAPI — Dependencies with yield: https://fastapi.tiangolo.com/tutorial/dependencies/dependencies-with-yield/
- FastAPI — Dependencies in path operation decorators / global dependencies: https://fastapi.tiangolo.com/tutorial/dependencies/global-dependencies/
- FastAPI — Testing Dependencies with Overrides: https://fastapi.tiangolo.com/advanced/testing-dependencies/
- FastAPI — Settings and Environment Variables: https://fastapi.tiangolo.com/advanced/settings/
- SQLAlchemy 2.0 — Session basics: https://docs.sqlalchemy.org/en/20/orm/session_basics.html
