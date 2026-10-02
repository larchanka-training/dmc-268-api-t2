"""Conservative token estimate (spec §5.3)."""

import pytest

from app.llm.tokens import ConservativeTokenCounter


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("abcdef", 3),  # 6 bytes -> 2 tokens -> x1.15 -> 3
        ("привет", 5),  # 12 UTF-8 bytes -> 4 tokens -> x1.15 -> 5
    ],
)
def test_counts_utf8_bytes_with_safety_margin(text: str, expected: int) -> None:
    assert ConservativeTokenCounter().count(text) == expected
