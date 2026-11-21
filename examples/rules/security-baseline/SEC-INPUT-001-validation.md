# SEC-INPUT-001: Validate all untrusted input on the server with allowlists

- **Severity:** MUST
- **Category:** security
- **Applies to:** All stacks — every trust boundary (HTTP APIs, message consumers, file uploads, CLI arguments, webhooks, environment-provided data from other systems)

## Rule

1. Every input that crosses a trust boundary MUST be validated on the server side before use, even if the client already validates it. Client-side validation is UX, not security.
2. Validation MUST be allowlist-based: define what is accepted (type, length, range, format, enum, charset) and reject everything else. Denylists of "bad characters" MUST NOT be the only control.
3. Input MUST be canonicalized/decoded before validation (e.g. resolve file paths, decode URL encoding once) so the validated form is the form that is used.
4. Sizes MUST be bounded: request body size, string lengths, array lengths, page size, upload size, nesting depth.
5. Validation failures MUST be rejected with a client error (`400`/`422`) and MUST NOT be "fixed up" silently into a different value that is then trusted.
6. Validation does not replace output encoding or parameterization: data MUST still be encoded for its sink (HTML, SQL, shell, path).

## Why

Most injection, path traversal, deserialization and resource-exhaustion bugs start with input that was never constrained (OWASP A05:2025 Injection, API4:2023 Unrestricted Resource Consumption; CWE-20).

## Good example

Python — bounded model and safe path handling:

```python
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

UPLOAD_ROOT = Path("/srv/app/uploads").resolve()


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_account: str = Field(pattern=r"^[A-Z0-9]{10,34}$")
    amount_cents: int = Field(gt=0, le=1_000_000_00)
    currency: str = Field(pattern=r"^(EUR|USD|GBP)$")


def resolve_upload(name: str) -> Path:
    candidate = (UPLOAD_ROOT / name).resolve()     # canonicalize first
    if not candidate.is_relative_to(UPLOAD_ROOT):  # then validate (Python 3.9+)
        raise ValueError("invalid file name")
    return candidate
```

Go — body size limit, strict decoding and root-scoped file access:

```go
func createTransfer(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20) // 1 MiB limit
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()

	var req TransferRequest
	if err := dec.Decode(&req); err != nil {
		http.Error(w, "invalid request body", http.StatusBadRequest)
		return
	}
	if err := req.Validate(); err != nil { // explicit allowlist checks: ranges, enums, formats
		http.Error(w, "invalid request", http.StatusUnprocessableEntity)
		return
	}
	...
}

// Go 1.24+: os.Root confines access to a directory tree, preventing "../" escapes.
func openUpload(dir, name string) (*os.File, error) {
	root, err := os.OpenRoot(dir)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	return root.Open(name)
}
```

## Bad example

```python
def resolve_upload(name: str) -> Path:
    if ".." in name:                          # denylist; misses absolute paths and encodings
        raise ValueError("bad name")
    return Path("/srv/app/uploads") / name    # name="/etc/passwd" escapes the root
```

```go
func createTransfer(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)             // unbounded read: memory exhaustion
	var req map[string]any
	_ = json.Unmarshal(body, &req)            // no schema, error ignored
	amount := req["amount"].(float64)         // unchecked; negative amounts accepted
	...
}
```

## Enforcement

| Method | How |
|---|---|
| Framework / types | Pydantic models (FastAPI), Zod schemas (TypeScript), typed structs + explicit `Validate()` (Go). |
| Linter / SAST | gosec `G304` (file path from variable), `G110` (decompression bomb); Ruff `S` rules; CodeQL path-injection queries. |
| Test | Negative tests per endpoint: oversized body, unknown field, out-of-range number, `../` path, wrong enum value. |
| Fuzzing | Go native fuzzing for parsers ([GO-TEST-001](../go/GO-TEST-001-fuzzing.md)). |
| Code review | Every new endpoint/consumer lists its validation; no denylist-only checks. |

## References

- OWASP Input Validation Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html
- OWASP File Upload Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html
- OWASP ASVS — V5 Validation, Sanitization and Encoding: https://asvs.owasp.org/
- Go — `http.MaxBytesReader`: https://pkg.go.dev/net/http#MaxBytesReader
- Go — `os.Root` (Go 1.24): https://pkg.go.dev/os#Root
- Python — `pathlib.PurePath.is_relative_to`: https://docs.python.org/3/library/pathlib.html#pathlib.PurePath.is_relative_to
- CWE-20 Improper Input Validation: https://cwe.mitre.org/data/definitions/20.html
- CWE-22 Path Traversal: https://cwe.mitre.org/data/definitions/22.html
