"""Provider schema identities select wire parsers without converting payloads."""

import json
from pathlib import Path

from app.review.schemas import result

REPO = Path(__file__).resolve().parents[2]


def test_explicit_provider_v1_parser_preserves_legacy_few_shots() -> None:
    shots = json.loads((REPO / ".agents/reviewer/v1/few_shots.json").read_text("utf-8"))
    model = result.provider_output_model("schemas/llm-output/v1.json")
    for shot in shots:
        raw = shot["output"]
        parsed = model.model_validate(raw)
        assert parsed.model_dump(mode="json") == raw


def test_explicit_provider_v2_parser_keeps_rule_references() -> None:
    example = json.loads(
        (REPO / "schemas/examples/review-result.v2.json").read_text("utf-8")
    )
    raw = {key: example[key] for key in ("summary", "findings", "limitations")}
    model = result.provider_output_model("schemas/llm-output/v2.json")
    assert model.model_validate(raw).model_dump(mode="json") == raw
