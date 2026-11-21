# SEC-LOG-001: Keep sensitive data out of logs, traces and error reports

- **Severity:** MUST
- **Category:** security
- **Applies to:** All stacks — application logs, access logs, traces/spans, metrics labels, error-tracking events, analytics

## Rule

1. Logs MUST NOT contain: passwords, session IDs, access/refresh tokens, API keys, `Authorization`/`Cookie` header values, private keys, full payment card numbers, CVV, or secrets of any kind.
2. Personal data (email, phone, address, national IDs, health data) MUST NOT be logged unless there is a documented need; when needed it SHOULD be masked or pseudonymized (e.g. user ID instead of email).
3. Code MUST NOT log entire request/response bodies, headers or ORM/domain objects by default; log an explicit allowlist of fields.
4. Logging MUST be structured (key/value or JSON) so that untrusted values cannot forge log lines (CWE-117 log injection).
5. Security-relevant events (login success/failure, permission denied, privilege changes, data export) SHOULD be logged with who/what/when/outcome but without the sensitive values above.
6. Error-tracking and APM tools MUST be configured to scrub sensitive fields before events leave the process.

## Why

Logs are copied to many systems, retained for a long time and readable by far more people than the production database. A token in a log line is a valid credential for anyone who can read the log (OWASP A09:2025 Security Logging and Alerting Failures; CWE-532).

## Good example

Python (standard `logging` with structured `extra`):

```python
import logging

logger = logging.getLogger("auth")


def login(username: str, password: str) -> Session:
    user = authenticate(username, password)
    if user is None:
        logger.warning("login failed", extra={"event": "auth.login_failed", "username_hash": hash_id(username)})
        raise InvalidCredentials()
    logger.info("login succeeded", extra={"event": "auth.login_succeeded", "user_id": user.id})
    return create_session(user)
```

Go (`log/slog`, a secret type that always redacts itself):

```go
type Token string

// LogValue implements slog.LogValuer: the value is never written to logs.
func (Token) LogValue() slog.Value { return slog.StringValue("REDACTED") }

func (Token) String() string { return "REDACTED" } // also covers fmt.Printf("%v")

func handle(logger *slog.Logger, userID int64, tok Token) {
	logger.Info("token refreshed", "user_id", userID, "token", tok) // logs token=REDACTED
}

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil)) // structured: values are escaped
	...
}
```

## Bad example

```python
logger.info(f"login attempt user={username} password={password}")   # credential in logs
logger.debug("request headers: %s", dict(request.headers))         # Authorization and Cookie headers
logger.info("created user %s", user.__dict__)                      # dumps hashed_password, email, ...
```

```go
log.Printf("calling API with key %s", apiKey)
log.Printf("user input: " + r.URL.Query().Get("q")) // non-constant format; "\n" in q forges new log lines
```

## Enforcement

| Method | How |
|---|---|
| Test | Capture logs during auth/payment tests (pytest `caplog`; a `slog` handler writing to a buffer in Go) and assert known secret values are absent. |
| Types | `SecretStr` (Pydantic) and `LogValuer` types in Go make accidental logging print a redacted value. |
| SAST | Semgrep/CodeQL rules for clear-text logging of sensitive data (e.g. CodeQL `py/clear-text-logging-sensitive-data`, `go/clear-text-logging`). |
| `go vet` | `printf` analyzer flags non-constant format strings. |
| Config review | Error-tracking SDKs configured with scrubbing (`before_send` hooks / server-side scrubbing rules); access logs do not record query strings that contain tokens. |
| Code review | New log statements log IDs, not objects or bodies. |

## References

- OWASP Logging Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html
- OWASP Top 10 — A09:2025: https://owasp.org/Top10/2025/A09_2025-Security_Logging_and_Alerting_Failures/
- CWE-532 Insertion of Sensitive Information into Log File: https://cwe.mitre.org/data/definitions/532.html
- CWE-117 Improper Output Neutralization for Logs: https://cwe.mitre.org/data/definitions/117.html
- Go — `log/slog` `LogValuer`: https://pkg.go.dev/log/slog#LogValuer
- Python — Logging HOWTO: https://docs.python.org/3/howto/logging.html
- pytest — `caplog` fixture: https://docs.pytest.org/en/stable/how-to/logging.html
