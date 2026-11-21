# GO-ERR-001: Check every error, wrap with context, inspect with `errors.Is`/`errors.As`

- **Severity:** MUST
- **Category:** correctness
- **Applies to:** Go — all packages

## Rule

1. Every returned `error` MUST be checked. Discarding it with `_` is allowed only when the operation is documented as unable to fail in that context, with a comment explaining why.
2. When returning an error from a lower layer, code SHOULD add context and MUST use `%w` (not `%v`) in `fmt.Errorf` when callers may need to inspect the cause.
3. Code MUST compare errors with `errors.Is` and extract typed errors with `errors.As`, never with `==` or a type assertion on a possibly wrapped error, and never by matching `err.Error()` strings.
4. An error MUST be handled once: either log it or return it, not both.
5. `panic` MUST NOT be used for expected failures (bad input, I/O, not found). It MAY be used for programmer errors / impossible states and in `init`/`Must*` helpers used with constant input.

## Why

- Ignored errors produce corrupted state and misleading downstream failures.
- `%w` preserves the error chain so `errors.Is(err, fs.ErrNotExist)` keeps working through layers of context.
- Logging and returning the same error produces duplicate, noisy logs without extra information.

## Good example

```go
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
)

var ErrInvalid = errors.New("invalid config")

type Config struct {
	Addr string `json:"addr"`
}

func Load(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read config %q: %w", path, err)
	}
	var cfg Config
	if err := json.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse config %q: %w", path, err)
	}
	if cfg.Addr == "" {
		return nil, fmt.Errorf("config %q: addr is empty: %w", path, ErrInvalid)
	}
	return &cfg, nil
}

// Caller: inspects the chain instead of comparing directly.
func LoadOrDefault(path string) (*Config, error) {
	cfg, err := Load(path)
	switch {
	case errors.Is(err, fs.ErrNotExist):
		return &Config{Addr: ":8080"}, nil
	case err != nil:
		var syntaxErr *json.SyntaxError
		if errors.As(err, &syntaxErr) {
			return nil, fmt.Errorf("config syntax error at offset %d: %w", syntaxErr.Offset, err)
		}
		return nil, err
	}
	return cfg, nil
}
```

## Bad example

```go
func Load(path string) *Config {
	data, _ := os.ReadFile(path)          // error discarded
	var cfg Config
	json.Unmarshal(data, &cfg)            // error not checked
	return &cfg
}

func Load2(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		log.Printf("read failed: %v", err) // logged AND returned
		return nil, fmt.Errorf("read: %v", err) // %v breaks the chain
	}
	...
}

func caller() {
	_, err := Load2("app.json")
	if err == fs.ErrNotExist { /* never true: err is wrapped */ }
	if strings.Contains(err.Error(), "no such file") { /* brittle string matching */ }
}
```

## Enforcement

| Method | How |
|---|---|
| Linter (golangci-lint) | `errcheck` (unchecked errors, in the `standard` set), `errorlint` (`==` comparisons, type assertions on errors, `%v` instead of `%w`). |
| `go vet` | `printf` analyzer reports misuse of `%w` (e.g. in non-`Errorf` calls). |
| Test | Tests assert `errors.Is(err, ErrInvalid)` for error paths, which fails if wrapping loses the chain. |
| Code review | No log-and-return; every `_ =` on an error has a justification comment. |

```yaml
# .golangci.yml (golangci-lint v2 format, excerpt)
version: "2"
linters:
  default: standard
  enable:
    - errorlint
```

## References

- Go blog — Working with Errors in Go 1.13: https://go.dev/blog/go1.13-errors
- Package `errors`: https://pkg.go.dev/errors
- Effective Go — Errors: https://go.dev/doc/effective_go#errors
- Go Code Review Comments — Handle Errors: https://go.dev/wiki/CodeReviewComments#handle-errors
- golangci-lint linters (errcheck, errorlint): https://golangci-lint.run/usage/linters/
