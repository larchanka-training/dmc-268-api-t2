# PY-SEC-001: Authenticate every non-public endpoint and authorize every object access

- **Severity:** MUST
- **Category:** security
- **Applies to:** Python / FastAPI — routers, endpoints, data-access layer

## Rule

1. Every endpoint that is not explicitly public MUST depend on an authentication dependency (e.g. `get_current_user`). Prefer attaching it at the `APIRouter(dependencies=[...])` level so new endpoints inherit it.
2. Public endpoints MUST be listed explicitly (allowlist), not inferred.
3. Any endpoint that reads or modifies a resource by ID MUST verify that the current principal is allowed to access *that specific object* (object-level authorization), typically by scoping the query to the owner/tenant.
4. Role or scope checks MUST be performed server-side in a dependency, never derived from client-supplied fields.
5. Authorization failures MUST return `401` (unauthenticated) or `403`/`404` (unauthorized), never partial data.

## Why

Broken Object Level Authorization (BOLA/IDOR) is #1 in the OWASP API Security Top 10 (2023), and Broken Access Control is #1 in the OWASP Top 10 (2025). FastAPI does not add authentication automatically — an endpoint without a security dependency is public.

## Good example

```python
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token")


def get_current_user(token: Annotated[str, Depends(oauth2_scheme)], db: DbSession) -> User:
    # decode_access_token verifies signature, expiry, issuer and audience with a vetted JWT library
    claims = decode_access_token(token)
    user = db.get(User, claims.subject) if claims is not None else None
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(role: str):
    def checker(user: CurrentUser) -> User:
        if role not in user.roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return user

    return checker


# Every route on this router requires authentication.
router = APIRouter(prefix="/invoices", dependencies=[Depends(get_current_user)])


@router.get("/{invoice_id}")
def get_invoice(invoice_id: UUID, user: CurrentUser, db: DbSession) -> InvoiceRead:
    # Object-level check: the query is scoped to the caller's tenant.
    invoice = db.scalar(
        select(Invoice).where(Invoice.id == invoice_id, Invoice.tenant_id == user.tenant_id)
    )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")  # do not reveal existence
    return InvoiceRead.model_validate(invoice, from_attributes=True)


@router.delete("/{invoice_id}", status_code=204, dependencies=[Depends(require_role("billing_admin"))])
def delete_invoice(invoice_id: UUID, user: CurrentUser, db: DbSession) -> None:
    ...
```

## Bad example

```python
router = APIRouter(prefix="/invoices")  # no auth dependency


@router.get("/{invoice_id}")
def get_invoice(invoice_id: UUID, db: DbSession) -> InvoiceRead:
    return db.get(Invoice, invoice_id)    # any caller can read any invoice (IDOR)


@router.delete("/{invoice_id}")
def delete_invoice(invoice_id: UUID, is_admin: bool, db: DbSession) -> None:
    if is_admin:                          # role comes from the client
        ...
```

## Enforcement

| Method | How |
|---|---|
| Test (pytest) — authz matrix | For each protected endpoint: no token → `401`; user of tenant B accessing tenant A's object → `403`/`404`; wrong role → `403`. |
| Test (pytest) — route audit | Walk all routes and fail if a route outside the public allowlist lacks `get_current_user` in its dependency tree (example below). |
| Code review | Every query by ID is scoped to owner/tenant, or a documented policy check is called. |
| DAST (optional) | OWASP ZAP or equivalent against a staging environment. |

```python
# tests/test_route_auth_audit.py
# Note: uses FastAPI's internal `route.dependant` structure; re-check after FastAPI upgrades.
from fastapi.routing import APIRoute

from app.auth import get_current_user
from app.main import app

PUBLIC_ROUTES = {("GET", "/health"), ("POST", "/auth/token")}


def _calls(dependant) -> set:
    found = {dependant.call}
    for sub in dependant.dependencies:
        found |= _calls(sub)
    return found


def test_all_non_public_routes_require_auth() -> None:
    missing = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            if (method, route.path) in PUBLIC_ROUTES:
                continue
            if get_current_user not in _calls(route.dependant):
                missing.append(f"{method} {route.path}")
    assert not missing, f"Routes without authentication: {missing}"
```

## References

- FastAPI — Security (OAuth2, scopes): https://fastapi.tiangolo.com/tutorial/security/
- FastAPI — Bigger Applications, router dependencies: https://fastapi.tiangolo.com/tutorial/bigger-applications/
- OWASP API Security Top 10 (2023) — API1 BOLA, API5 BFLA: https://api-security.owasp.org/editions/2023/en/0x11-t10/
- OWASP Authorization Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html
- OWASP IDOR Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Insecure_Direct_Object_Reference_Prevention_Cheat_Sheet.html
- Related: [SEC-AUTHZ-001](../security-baseline/SEC-AUTHZ-001-least-privilege.md)
