# TDD appendix — API / Python / FastAPI

Stack-labeled samples for FastAPI + pytest (and similar) API work. Methodology stays in [tests.md](tests.md) and [SKILL.md](SKILL.md).

## Good — observable behaviour through the public seam

Prefer HTTP handlers or service interfaces the product owns — not private helpers.

```python
# GOOD: Tests observable behavior via public API
def test_user_can_checkout_with_valid_cart(client):
    cart_id = client.post("/carts", json={"items": [{"sku": "a", "qty": 1}]}).json()["id"]
    result = client.post(f"/carts/{cart_id}/checkout", json={"payment_method": "card"})
    assert result.status_code == 200
    assert result.json()["status"] == "confirmed"
```

## Bad — implementation-coupled

```python
# BAD: Asserts on internal collaborator calls
def test_checkout_calls_payment_service(mocker):
    payment = mocker.patch("app.services.checkout.payment_service.process")
    checkout(cart, payment_method)
    payment.assert_called_once_with(cart.total)
```

## Bad vs good — side channel

```python
# BAD: Bypasses interface to verify persistence
def test_create_user_saves_to_database(db_session):
    create_user(name="Alice")
    row = db_session.execute(text("SELECT * FROM users WHERE name = :n"), {"n": "Alice"}).first()
    assert row is not None

# GOOD: Verifies through the public contract
def test_create_user_makes_user_retrievable(client):
    created = client.post("/users", json={"name": "Alice"}).json()
    retrieved = client.get(f"/users/{created['id']}").json()
    assert retrieved["name"] == "Alice"
```

## Bad vs good — tautology

```python
# BAD: Expected value recomputed the way production code does
def test_calculate_total_sums_line_items():
    items = [{"price": 10}, {"price": 5}]
    expected = sum(i["price"] for i in items)
    assert calculate_total(items) == expected

# GOOD: Independent known literal
def test_calculate_total_sums_line_items():
    assert calculate_total([{"price": 10}, {"price": 5}]) == 15
```

## Mocking at API boundaries

Mock external systems (payment provider, email, third-party HTTP) at the edge. Prefer injecting a narrow client interface rather than patching deep internals. Do not mock your own domain services when testing through the HTTP/service seam.
