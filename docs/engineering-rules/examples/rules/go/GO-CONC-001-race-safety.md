# GO-CONC-001: Synchronize all shared mutable state and run tests with `-race`

- **Severity:** MUST
- **Category:** correctness
- **Applies to:** Go — any code reachable from more than one goroutine (HTTP handlers, workers, caches)

## Rule

1. Memory that is written by one goroutine and read or written by another MUST be protected by a `sync.Mutex`/`sync.RWMutex`, a `sync/atomic` type, or ownership transfer via channels.
2. Built-in maps MUST NOT be accessed concurrently with any write without synchronization.
3. Types containing a `sync.Mutex` (or other `sync` types) MUST NOT be copied after first use; use pointer receivers.
4. CI MUST run `go test -race ./...` and treat a reported race as a test failure.
5. Each type that is safe for concurrent use SHOULD say so in its doc comment; types that are not SHOULD also say so.

## Why

A data race is undefined behavior under the Go memory model: corrupted values, crashes (`fatal error: concurrent map writes`), or security bugs that appear only under load. The race detector finds races that actually execute during tests, so it must run routinely.

## Good example

```go
package stats

import (
	"sync"
	"sync/atomic"
)

// Counter is safe for concurrent use.
type Counter struct {
	mu     sync.Mutex
	counts map[string]int
}

func NewCounter() *Counter {
	return &Counter{counts: make(map[string]int)}
}

func (c *Counter) Inc(key string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.counts[key]++
}

func (c *Counter) Get(key string) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.counts[key]
}

// Requests is a lock-free counter, safe for concurrent use.
var Requests atomic.Int64
```

Race test:

```go
func TestCounterConcurrent(t *testing.T) {
	c := NewCounter()
	var wg sync.WaitGroup
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			c.Inc("k")
		}()
	}
	wg.Wait()
	if got := c.Get("k"); got != 100 {
		t.Fatalf("Get = %d, want 100", got)
	}
}
```

## Bad example

```go
var cache = map[string]string{}        // shared, unsynchronized

func handler(w http.ResponseWriter, r *http.Request) {
	cache[r.URL.Path] = "seen"         // concurrent map writes from many requests
}

type Counter struct {
	mu sync.Mutex
	n  int
}

func (c Counter) Inc() {               // value receiver copies the mutex: no protection
	c.mu.Lock()
	defer c.mu.Unlock()
	c.n++
}

var hits int

func track() { hits++ }                // non-atomic increment from multiple goroutines
```

## Enforcement

| Method | How |
|---|---|
| Test (race detector) | `go test -race ./...` in CI on every PR (requires cgo-capable toolchain on the runner). |
| `go vet` | `copylocks` analyzer flags copied mutexes (runs by default in `go vet` and in `go test`). |
| Code review | Every package-level `var` of map/slice/struct type that is mutated has documented synchronization. |

## References

- Go — Data Race Detector: https://go.dev/doc/articles/race_detector
- The Go Memory Model: https://go.dev/ref/mem
- Package `sync`: https://pkg.go.dev/sync
- Package `sync/atomic`: https://pkg.go.dev/sync/atomic
- `go vet` analyzers (copylocks): https://pkg.go.dev/cmd/vet
- Go FAQ — Why are map operations not defined to be atomic?: https://go.dev/doc/faq#atomic_maps
