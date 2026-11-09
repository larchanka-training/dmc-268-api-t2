# Security
## SEC-1: Tenant isolation
Every query for tenant-owned records must filter tenant_id using the authenticated user's tenant_id. Request-supplied tenant identifiers are untrusted.
# Performance
## PERF-1: Explicit HTTP timeout
Every outbound HTTP request must pass an explicit finite positive timeout argument, including requests made by an injected client.
# Style
## STYLE-1: Public error code prefix
Every public response error code must be an uppercase snake-case string beginning APP_. This applies to the error_code field of returned response dictionaries.
