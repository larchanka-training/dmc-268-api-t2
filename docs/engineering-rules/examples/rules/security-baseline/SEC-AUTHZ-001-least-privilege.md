# SEC-AUTHZ-001: Deny by default and grant the least privilege needed

- **Severity:** MUST
- **Category:** security
- **Applies to:** All stacks — application authorization, database accounts, service accounts, CI/CD tokens, containers, cloud IAM

## Rule

1. Access control MUST deny by default: a request, role or identity has no access unless a rule explicitly grants it.
2. Authorization MUST be enforced server-side on every request, including object-level checks for resources addressed by ID.
3. Each identity (application DB user, service account, CI token, API key) MUST have only the permissions required for its function. Shared admin/superuser credentials MUST NOT be used by applications.
4. CI workflows MUST declare minimal token permissions (e.g. `permissions: contents: read` in GitHub Actions) and elevate only in the job that needs it.
5. Containers SHOULD run as a non-root user with a read-only root filesystem where feasible.
6. Privileged operations (role changes, deletion, exports) SHOULD be audit-logged (see [SEC-LOG-001](SEC-LOG-001-sensitive-data.md)).

## Why

Broken access control is #1 in the OWASP Top 10 (2025). Least privilege limits the blast radius: a compromised service with read-only access to one schema cannot drop the database or push to the repository.

## Good example

Application — deny by default in a policy function:

```python
from enum import Enum


class Action(Enum):
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"


POLICY: dict[str, set[Action]] = {
    "viewer": {Action.READ},
    "editor": {Action.READ, Action.UPDATE},
    "admin": {Action.READ, Action.UPDATE, Action.DELETE},
}


def is_allowed(user: User, action: Action, doc: Document) -> bool:
    if doc.tenant_id != user.tenant_id:
        return False
    allowed: set[Action] = set()
    for role in user.roles:
        allowed |= POLICY.get(role, set())   # unknown role -> nothing
    return action in allowed
```

Database — dedicated application role:

```sql
CREATE ROLE app_orders LOGIN PASSWORD :'app_password';  -- password injected by the secret manager
GRANT CONNECT ON DATABASE shop TO app_orders;
GRANT USAGE ON SCHEMA orders TO app_orders;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA orders TO app_orders;
-- no DELETE, no DDL, no access to other schemas
```

CI — minimal GitHub Actions token:

```yaml
permissions:
  contents: read          # workflow default

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - run: make test
  release:
    needs: test
    runs-on: ubuntu-latest
    permissions:
      contents: write     # only this job can create releases
    steps:
      - run: make release
```

Container:

```dockerfile
FROM gcr.io/distroless/static-debian12:nonroot
COPY --chown=nonroot:nonroot app /app
USER nonroot
ENTRYPOINT ["/app"]
```

## Bad example

```python
def is_allowed(user: User, action: Action, doc: Document) -> bool:
    if "guest" in user.roles and action is Action.DELETE:
        return False
    return True                           # allow by default; new roles/actions are open
```

```yaml
permissions: write-all                     # every job can push code, edit releases, etc.
```

```text
DATABASE_URL=postgresql://postgres:...@db/shop   # application connects as superuser
```

## Enforcement

| Method | How |
|---|---|
| Test | Authorization matrix tests (role × action × tenant) including "unknown role → denied". |
| CI linting | `zizmor` (GitHub Actions static analysis, includes excessive-permissions checks); OpenSSF Scorecard `Token-Permissions` check. |
| Container linting | Hadolint `DL3002` (last USER should not be root). |
| IaC scanning | Checkov / Trivy config scanning for over-broad IAM policies. |
| Code review | New endpoints declare required permission; new service accounts list their grants. |

## References

- OWASP Authorization Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html
- OWASP Top 10 — A01:2025 Broken Access Control: https://owasp.org/Top10/2025/A01_2025-Broken_Access_Control/
- GitHub Docs — Controlling permissions for GITHUB_TOKEN: https://docs.github.com/en/actions/tutorials/authenticate-with-github_token#modifying-the-permissions-for-the-github_token
- PostgreSQL — GRANT: https://www.postgresql.org/docs/current/sql-grant.html
- OWASP Docker Security Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html
- OpenSSF Scorecard checks: https://github.com/ossf/scorecard/blob/main/docs/checks.md
- zizmor: https://docs.zizmor.sh/
- Hadolint: https://github.com/hadolint/hadolint
