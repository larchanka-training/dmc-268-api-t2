# SEC-SECRET-001: Never commit secrets; load them at runtime from the environment or a secret manager

- **Severity:** MUST
- **Category:** security
- **Applies to:** All stacks — source code, configuration files, Dockerfiles, CI definitions, tests, notebooks, documentation

## Rule

1. Passwords, API keys, tokens, private keys, connection strings with credentials and signing secrets MUST NOT appear in source code, committed config files, container images or CI logs.
2. Secrets MUST be provided at runtime via environment variables injected by the platform, mounted secret files, or a secret manager (e.g. HashiCorp Vault, AWS Secrets Manager, GCP Secret Manager, Azure Key Vault).
3. Applications MUST fail fast at startup if a required secret is missing — no hard-coded fallback value.
4. Secret values SHOULD be held in types that do not print themselves (`pydantic.SecretStr`, a Go type implementing `slog.LogValuer`/`fmt.Stringer` that redacts).
5. `.env` files and key files MUST be in `.gitignore`; an `.env.example` with placeholder values MAY be committed.
6. Every repository MUST run secret scanning in CI (and SHOULD enable push protection). A detected secret MUST be treated as compromised: revoke and rotate it first, then remove it from history.

## Why

Secrets in Git are copied to every clone, fork, CI cache and backup and remain in history after deletion. Leaked credentials are among the most common initial access vectors (OWASP A07:2025 Authentication Failures; CWE-798).

## Good example

Python:

```python
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_")

    database_url: SecretStr          # required: startup fails if APP_DATABASE_URL is unset
    stripe_api_key: SecretStr


settings = Settings()
print(settings.stripe_api_key)       # prints '**********'
client = StripeClient(api_key=settings.stripe_api_key.get_secret_value())
```

Go:

```go
func mustEnv(key string) string {
	v, ok := os.LookupEnv(key)
	if !ok || v == "" {
		log.Fatalf("required environment variable %s is not set", key) // name only, never the value
	}
	return v
}

func main() {
	dsn := mustEnv("APP_DATABASE_URL")
	...
}
```

TypeScript (frontend): anything bundled into the browser is public. Secrets MUST stay on the server; the frontend calls a backend endpoint that uses the secret.

```gitignore
.env
.env.*
!.env.example
*.pem
*.key
```

## Bad example

```python
STRIPE_API_KEY = "<live key pasted here>"                        # hard-coded secret
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://admin:admin@db/prod")  # fallback credential
```

```go
const jwtSecret = "changeme"                                      // signing key in source
```

```ts
// Bundled into client JS and visible to every user.
const OPENAI_KEY = import.meta.env.VITE_OPENAI_KEY;
```

```dockerfile
ENV DB_PASSWORD=supersecret    # stored in an image layer
```

## Enforcement

| Method | How |
|---|---|
| Secret scanning (CI) | Gitleaks (`gitleaks/gitleaks-action@v3` in GitHub Actions, or the `gitleaks` CLI) on every push/PR over the diff and periodically over full history. |
| Platform | GitHub secret scanning with push protection (or the equivalent in GitLab/Bitbucket). |
| Pre-commit (optional) | Gitleaks pre-commit hook so secrets are caught before they leave the laptop. |
| Linter | Ruff `S105`/`S106`/`S107` (hard-coded password strings/args/defaults); gosec `G101` (hard-coded credentials). |
| Container | Image scanning (e.g. Trivy secret scanning) before publishing. |
| Incident process | On detection: revoke → rotate → purge history → review access logs. |

## References

- OWASP Secrets Management Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html
- CWE-798 Use of Hard-coded Credentials: https://cwe.mitre.org/data/definitions/798.html
- GitHub — Secret scanning and push protection: https://docs.github.com/en/code-security/how-tos/secure-your-secrets
- Gitleaks: https://github.com/gitleaks/gitleaks
- Pydantic Settings: https://pydantic.dev/docs/validation/latest/concepts/pydantic_settings/
- Pydantic — `SecretStr`: https://pydantic.dev/docs/validation/latest/api/pydantic/types/#pydantic.types.SecretStr
- Vite — Env variables (exposed to client code): https://vite.dev/guide/env-and-mode
- The Twelve-Factor App — Config: https://12factor.net/config
