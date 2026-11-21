# SEC-DEP-001: Pin dependencies and scan them for known vulnerabilities in CI

- **Severity:** MUST
- **Category:** security
- **Applies to:** All stacks — Python packages, npm packages, Go modules, container base images, CI actions

## Rule

1. Every project MUST commit a lockfile (`uv.lock`/`poetry.lock`/pinned `requirements.txt` with hashes, `package-lock.json`/`pnpm-lock.yaml`/`yarn.lock`, `go.sum`).
2. CI MUST install from the lockfile reproducibly (`npm ci`, `pip install --require-hashes -r requirements.txt`, `uv sync --locked`, Go module verification via `go.sum`).
3. CI MUST scan dependencies for known vulnerabilities on every PR and on a schedule: `pip-audit` (Python), `npm audit` (npm), `govulncheck` (Go), or an equivalent multi-ecosystem tool (e.g. OSV-Scanner, Trivy).
4. A known vulnerability at or above an agreed threshold (default: High/Critical, or any reachable vuln reported by `govulncheck`) MUST fail the build unless an exception is recorded (see the guide's exception process).
5. Automated update PRs (Dependabot or Renovate) SHOULD be enabled for all ecosystems including CI actions and base images.
6. Third-party CI actions SHOULD be pinned to a full commit SHA.
7. New dependencies SHOULD be reviewed for maintenance status, license and necessity before being added (e.g. via GitHub's dependency review on PRs).

## Why

Using components with known vulnerabilities falls under OWASP Top 10 A03:2025 Software Supply Chain Failures, and unpinned dependencies or actions are a supply-chain risk: a compromised upstream release flows straight into the build.

## Good example

```yaml
# .github/workflows/deps.yml
name: dependency-scan
on:
  pull_request:
  schedule:
    - cron: "0 6 * * 1"
permissions:
  contents: read

jobs:
  python:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with: { python-version: "3.12" }
      - run: pip install pip-audit
      - run: pip-audit -r requirements.txt

  node:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-node@v7
        with: { node-version: "24" }
      - run: npm ci
      - run: npm audit --audit-level=high

  go:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-go@v7
        with: { go-version-file: go.mod }
      - run: go install golang.org/x/vuln/cmd/govulncheck@latest
      - run: govulncheck ./...

  review:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/dependency-review-action@v5
        with:
          fail-on-severity: high
```

```yaml
# .github/dependabot.yml
version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
    schedule: { interval: "weekly" }
  - package-ecosystem: "npm"
    directory: "/"
    schedule: { interval: "weekly" }
  - package-ecosystem: "gomod"
    directory: "/"
    schedule: { interval: "weekly" }
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule: { interval: "weekly" }
  - package-ecosystem: "docker"
    directory: "/"
    schedule: { interval: "weekly" }
```

## Bad example

```yaml
steps:
  - uses: some-org/some-action@main          # mutable ref: upstream can change what runs
  - run: npm install                         # ignores/rewrites the lockfile
  - run: pip install fastapi requests        # unpinned, unaudited
  - run: curl -sSL https://example.com/install.sh | sh   # unverified remote script
```

```text
requirements.txt
fastapi            # no version: every build may resolve differently
```

## Enforcement

| Method | How |
|---|---|
| CI | Jobs above fail the PR on vulnerabilities at/above the threshold. |
| Platform | Dependabot alerts + security updates enabled; `dependency-review-action` on PRs. |
| Branch protection | Dependency-scan jobs are required status checks. |
| Code review | Lockfile changes are reviewed together with manifest changes; new dependencies justified in the PR description. |

## References

- OWASP Top 10 — A03:2025 Software Supply Chain Failures: https://owasp.org/Top10/2025/A03_2025-Software_Supply_Chain_Failures/
- OWASP Vulnerable Dependency Management Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Vulnerable_Dependency_Management_Cheat_Sheet.html
- pip-audit: https://github.com/pypa/pip-audit
- pip — Secure installs (hash-checking mode): https://pip.pypa.io/en/stable/topics/secure-installs/
- npm audit: https://docs.npmjs.com/cli/commands/npm-audit
- npm ci: https://docs.npmjs.com/cli/commands/npm-ci
- Go — Vulnerability management / govulncheck: https://go.dev/doc/security/vuln/
- OSV-Scanner: https://google.github.io/osv-scanner/
- GitHub — Dependabot: https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain
- GitHub — Dependency review action: https://github.com/actions/dependency-review-action
- GitHub — Security hardening for GitHub Actions (pin actions to SHA): https://docs.github.com/en/actions/reference/security/secure-use
