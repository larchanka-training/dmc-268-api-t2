"""Attested Reflection input renderer; receives the actually rendered context."""

import json
import re
from typing import Any


def render(context: str, findings: list[dict[str, Any]]) -> str:
    tags = (
        "repository_instructions|pr_metadata|diff|invalid_answer|repo_rules|"
        "findings|source_context|system|developer|assistant|user"
    )
    data = re.sub(
        rf"<\s*(/?)\s*({tags})\b[^>]*>",
        r"[\1\2]",
        json.dumps(findings, ensure_ascii=False),
        flags=re.IGNORECASE,
    )
    data = re.sub(
        r"\[/?INST\]|<</?SYS>>|<\|(?:im_start|im_end|system|assistant|user|endoftext|start_header_id|end_header_id|eot_id)\|>",
        "[role marker]",
        data,
        flags=re.IGNORECASE,
    )
    return context + "\n\n<findings>\n" + data + "\n</findings>"
