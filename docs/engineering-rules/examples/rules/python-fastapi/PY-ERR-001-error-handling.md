# PY-ERR-001: Map errors to explicit HTTP responses; never swallow or leak them

- **Severity:** MUST
- **Category:** correctness
- **Applies to:** Python / FastAPI — handlers, services, exception handlers

## Rule

1. Expected failures (not found, conflict, forbidden, validation) MUST be raised as `HTTPException` with the correct status code, or as domain exceptions mapped to a status code by a registered `@app.exception_handler`.
2. Code MUST NOT use bare `except:` or `except Exception:` that discards the error (`pass`, `continue`, or a `200` response).
3. Error responses MUST NOT contain stack traces, SQL, file paths, or `str(exc)` of unexpected exceptions. Unexpected errors MUST be logged server-side and returned as a generic `500`.
4. When re-raising inside `except`, code MUST chain the cause (`raise NewError(...) from exc`).
5. Production deployments MUST NOT run with `FastAPI(debug=True)`.

## Why

- Swallowed exceptions turn failures into silent data corruption and return `200` for failed operations.
- Leaked internals help attackers fingerprint the stack (OWASP A10:2025 Mishandling of Exceptional Conditions; CWE-209).
- Correct status codes let clients and monitoring distinguish client errors (`4xx`) from server errors (`5xx`).

## Good example

```python
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
app = FastAPI()  # debug defaults to False


class OrderNotFoundError(Exception):
    def __init__(self, order_id: int) -> None:
        super().__init__(f"order {order_id} not found")
        self.order_id = order_id


class PaymentGatewayError(Exception):
    pass


@app.exception_handler(OrderNotFoundError)
async def order_not_found_handler(request: Request, exc: OrderNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Order not found"})


def charge(order_id: int) -> None:
    try:
        gateway.charge(order_id)
    except gateway.TimeoutError as exc:
        # keep the original cause for logs and debugging
        raise PaymentGatewayError("payment gateway timed out") from exc


@app.post("/orders/{order_id}/pay", status_code=202)
def pay_order(order_id: int) -> dict[str, str]:
    order = repo.get(order_id)
    if order is None:
        raise OrderNotFoundError(order_id)
    if order.paid:
        raise HTTPException(status_code=409, detail="Order already paid")
    try:
        charge(order_id)
    except PaymentGatewayError:
        logger.exception("payment failed", extra={"order_id": order_id})
        raise HTTPException(status_code=503, detail="Payment temporarily unavailable") from None
    return {"status": "accepted"}
```

Unexpected exceptions are not caught at all: Starlette's server-error middleware logs them and returns a plain `500 Internal Server Error` without details when `debug=False`.

## Bad example

```python
@app.post("/orders/{order_id}/pay")
def pay_order(order_id: int) -> dict:
    try:
        charge(order_id)
    except:                                   # catches KeyboardInterrupt/SystemExit too
        pass                                  # failure is silently ignored
    return {"status": "ok"}                   # 200 even when nothing happened


@app.get("/orders/{order_id}")
def get_order(order_id: int) -> dict:
    try:
        return repo.get(order_id).to_dict()
    except Exception as exc:
        return {"error": str(exc)}            # leaks internals with status 200
```

## Enforcement

| Method | How |
|---|---|
| Linter (Ruff) | `E722` (bare except), `BLE001` (blind except), `S110` (try-except-pass), `S112` (try-except-continue), `B904` (raise without `from` inside except). |
| Test (pytest) | Assert status codes for each error branch; assert a forced unexpected error returns a body without traceback text. |
| Config check | Deployment manifests/settings do not set `debug=True`; reviewed in code review. |

```toml
# pyproject.toml
[tool.ruff.lint]
extend-select = ["E722", "BLE", "S110", "S112", "B904"]
```

```python
from fastapi.testclient import TestClient


def test_unexpected_error_does_not_leak(monkeypatch) -> None:
    def boom(order_id: int) -> None:
        raise RuntimeError("SELECT * FROM orders -- secret detail")

    monkeypatch.setattr("app.main.charge", boom)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/orders/1/pay")
    assert response.status_code == 500
    assert "SELECT" not in response.text
    assert "Traceback" not in response.text
```

## References

- FastAPI — Handling Errors: https://fastapi.tiangolo.com/tutorial/handling-errors/
- Python — Exception chaining (`raise ... from`): https://docs.python.org/3/tutorial/errors.html#exception-chaining
- Ruff rules (E722, BLE001, S110, S112, B904): https://docs.astral.sh/ruff/rules/
- OWASP Error Handling Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html
- CWE-209 Generation of Error Message Containing Sensitive Information: https://cwe.mitre.org/data/definitions/209.html
