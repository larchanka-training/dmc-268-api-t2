# GO-TEST-001: Fuzz every parser and decoder of untrusted input

- **Severity:** SHOULD
- **Category:** security
- **Applies to:** Go — functions that parse or decode bytes/strings from outside the process (protocols, file formats, query strings, custom encodings)

## Rule

1. Each exported function that parses untrusted input SHOULD have a native Go fuzz test (`func FuzzXxx(f *testing.F)`).
2. A fuzz target MUST be deterministic and fast, MUST NOT depend on network or shared global state, and SHOULD assert a property (round-trip, invariant, no panic, bounded output) rather than only calling the function.
3. Seed inputs SHOULD be provided with `f.Add` and/or files under `testdata/fuzz/FuzzXxx/`. Failing inputs found by the fuzzer MUST be committed there so they run as regression tests on every `go test`.
4. CI SHOULD run each fuzz target for a bounded time (e.g. `-fuzztime=30s`) on a schedule or on PRs that touch the parser.

## Why

Parsers are where untrusted bytes meet program logic; edge cases that hand-written tests miss (truncated input, huge lengths, invalid UTF-8) cause panics, infinite loops and memory exhaustion — a DoS or worse. Go's native fuzzing generates such inputs automatically, and seed-corpus entries run as ordinary unit tests even without `-fuzz`.

## Good example

```go
package kv

import (
	"reflect"
	"testing"
	"unicode/utf8"
)

// Parse parses "k1=v1;k2=v2" into a map. Encode is its inverse.
// func Parse(s string) (map[string]string, error)
// func Encode(m map[string]string) string

func FuzzParse(f *testing.F) {
	f.Add("a=1;b=2")
	f.Add("")
	f.Add("=;;=")
	f.Fuzz(func(t *testing.T, s string) {
		if !utf8.ValidString(s) {
			t.Skip()
		}
		m, err := Parse(s)
		if err != nil {
			return // invalid input is fine; panics and hangs are not
		}
		// Property: Encode/Parse round-trips.
		m2, err := Parse(Encode(m))
		if err != nil {
			t.Fatalf("Parse(Encode(%v)) failed: %v", m, err)
		}
		if !reflect.DeepEqual(m, m2) {
			t.Fatalf("round-trip mismatch: %v != %v", m, m2)
		}
	})
}
```

Run locally:

```sh
go test ./kv                                   # runs seed corpus as normal tests
go test ./kv -run='^$' -fuzz='^FuzzParse$' -fuzztime=60s
```

## Bad example

```go
func FuzzParse(f *testing.F) {
	f.Fuzz(func(t *testing.T, s string) {
		resp, _ := http.Get("http://localhost:8080/parse?q=" + s) // network: slow, flaky, non-deterministic
		_ = resp
		globalCache[s] = true                                     // shared state between runs
	})
}

// Only hand-picked happy-path inputs; crashes on "a=" or "\xff" are never exercised.
func TestParse(t *testing.T) {
	m, _ := Parse("a=1")
	_ = m
}
```

## Enforcement

| Method | How |
|---|---|
| Test | `go test ./...` runs all seed corpus entries (including committed crashers) in the normal test job. |
| CI (scheduled job) | `go test ./<pkg> -run='^$' -fuzz='^FuzzXxx$' -fuzztime=30s` per fuzz target (`-fuzz` accepts one package per invocation). Upload `testdata/fuzz/` as an artifact on failure. |
| Code review | New or changed parsers come with a fuzz target; found crashers are committed under `testdata/fuzz/`. |
| Continuous fuzzing (optional) | OSS-Fuzz / ClusterFuzzLite support Go native fuzz targets. |

## References

- Go — Fuzzing: https://go.dev/doc/security/fuzz/
- Go tutorial — Getting started with fuzzing: https://go.dev/doc/tutorial/fuzz
- Package `testing` — Fuzzing: https://pkg.go.dev/testing#hdr-Fuzzing
- `go test` testing flags (`-fuzz`, `-fuzztime`): https://pkg.go.dev/cmd/go#hdr-Testing_flags
- ClusterFuzzLite: https://google.github.io/clusterfuzzlite/
