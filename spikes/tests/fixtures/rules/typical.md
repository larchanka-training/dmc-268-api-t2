---
version: 1
extends: [".review/shared.md"]
scope: "src/**"
---
# Security

- [SEC-1] severity=critical paths=src/api/**,src/auth/**
  All SQL must use parameterized queries; string-built SQL is a blocker.
- [SEC-2] severity=high allow_inline_ignore=true
  Do not disable TLS certificate verification.
- [SYS-SEC-1] severity=critical

# Performance

- [PERF-1] severity=medium paths=src/db/**
  Avoid queries inside loops; batch them.

# Style

- [STY-1] severity=low allow_inline_ignore=true
  Public functions need a docstring.
- [SYS-STY-1] enabled=false

# Ignore

- vendor/**
- **/*.generated.ts
- docs/legacy/**
