# GO-SEC-001: Build SQL only with placeholders and allowlisted identifiers

- **Severity:** MUST
- **Category:** security
- **Applies to:** Go — `database/sql`, `pgx`, `sqlx` and any code that issues SQL

## Rule

1. Values in SQL statements MUST be passed as query arguments using the driver's placeholder syntax (`$1` for PostgreSQL drivers, `?` for MySQL/SQLite drivers). They MUST NOT be concatenated or formatted into the query string (`+`, `fmt.Sprintf`, `strings.Builder`).
2. Identifiers that cannot be parameterized (column names for `ORDER BY`, table names, sort direction) MUST be chosen from a hard-coded allowlist keyed by user input, never inserted from input directly.
3. Queries SHOULD use the `...Context` variants (`QueryContext`, `ExecContext`, `QueryRowContext`) so they are cancelled with the request (see GO-CTX-001 in the guide).
4. `rows.Err()` MUST be checked after iteration and `rows.Close()` MUST be deferred.

## Why

SQL injection lets an attacker read, modify or delete data and sometimes execute commands on the database host (OWASP A05:2025 Injection; CWE-89). With placeholders, the driver sends the value separately from the SQL text, so it cannot change the query structure.

## Good example

```go
package users

import (
	"context"
	"database/sql"
	"fmt"
)

type User struct {
	ID    int64
	Email string
}

var sortColumns = map[string]string{ // allowlist: user input -> SQL identifier
	"email":   "email",
	"created": "created_at",
}

func FindByEmail(ctx context.Context, db *sql.DB, email string) (*User, error) {
	var u User
	err := db.QueryRowContext(ctx,
		"SELECT id, email FROM users WHERE email = $1", email,
	).Scan(&u.ID, &u.Email)
	if err != nil {
		return nil, fmt.Errorf("find user by email: %w", err)
	}
	return &u, nil
}

func List(ctx context.Context, db *sql.DB, sortKey string, limit int) ([]User, error) {
	col, ok := sortColumns[sortKey]
	if !ok {
		col = "created_at"
	}
	// col comes only from the allowlist above; values remain placeholders.
	query := "SELECT id, email FROM users ORDER BY " + col + " LIMIT $1"
	rows, err := db.QueryContext(ctx, query, limit)
	if err != nil {
		return nil, fmt.Errorf("list users: %w", err)
	}
	defer rows.Close()

	var out []User
	for rows.Next() {
		var u User
		if err := rows.Scan(&u.ID, &u.Email); err != nil {
			return nil, fmt.Errorf("scan user: %w", err)
		}
		out = append(out, u)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate users: %w", err)
	}
	return out, nil
}
```

## Bad example

```go
func FindByEmail(db *sql.DB, email string) (*User, error) {
	// email = "' OR '1'='1" returns the first user; worse payloads modify data.
	query := fmt.Sprintf("SELECT id, email FROM users WHERE email = '%s'", email)
	row := db.QueryRow(query)
	...
}

func List(db *sql.DB, sort string) (*sql.Rows, error) {
	return db.Query("SELECT id, email FROM users ORDER BY " + sort) // identifier from input
}
```

## Enforcement

| Method | How |
|---|---|
| SAST (gosec, also via golangci-lint) | `G201` (SQL built with format string), `G202` (SQL built with string concatenation). Allowlisted identifier concatenation needs a `//nolint:gosec // reason` with justification. |
| SAST (CodeQL) | `go/sql-injection` query in the default Go query suite. |
| Linter | `rowserrcheck` and `sqlclosecheck` (golangci-lint) for `rows.Err()` / `Close()`. |
| Test | Integration test passes `' OR '1'='1` and `"; DROP TABLE users; --` as input and asserts no extra rows / no error from schema change. |

```yaml
# .golangci.yml (v2 format, excerpt)
version: "2"
linters:
  default: standard
  enable: [gosec, rowserrcheck, sqlclosecheck]
```

## References

- Go — Avoiding SQL injection risk: https://go.dev/doc/database/sql-injection
- Go — Querying for data: https://go.dev/doc/database/querying
- Package `database/sql`: https://pkg.go.dev/database/sql
- gosec rules: https://github.com/securego/gosec#available-rules
- OWASP SQL Injection Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.html
- OWASP Query Parameterization Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Query_Parameterization_Cheat_Sheet.html
- CWE-89: https://cwe.mitre.org/data/definitions/89.html
