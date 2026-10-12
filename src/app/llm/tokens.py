"""Token estimation shared by chunking, prompt building and the run budget."""

import math
from typing import Protocol


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


class ConservativeTokenCounter:
    """Over-estimates tokens from UTF-8 size: primary and fallback models use
    different tokenizers, so no single exact tokenizer applies (spec §5.3)."""

    def __init__(self, bytes_per_token: float = 3, safety_margin: float = 0.15) -> None:
        self._bytes_per_token = bytes_per_token
        self._factor = 1 + safety_margin

    def count(self, text: str) -> int:
        tokens = math.ceil(len(text.encode("utf-8")) / self._bytes_per_token)
        return math.ceil(tokens * self._factor)
