"""Characterize the existing Code/Diff result union through the public service.

No new response DTO is needed: serialize the service's declared return type.
All findings below are literal provider completions, not fabricated service data.
"""

import json
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError
from test_code_simulation import (
    FakeLLM,
    forbid_external_io,  # noqa: F401 - fixture
    request_rules,
)
from test_diff_simulation import PATCH

from app.api.rule_simulations import (
    SimulationCodeInput,
    SimulationDiffInput,
    SimulationRules,
)
from app.review.simulation import (
    CodeSimulationResult,
    CodeSimulationService,
    DiffSimulationResult,
)


@pytest.fixture(params=["code", "old", "new"])
def simulation_case(
    request: pytest.FixtureRequest,
) -> tuple[SimulationCodeInput | SimulationDiffInput, dict[str, Any]]:
    item = {
        "path": "snippet.py",
        "start_line": 1,
        "end_line": 2,
        "rule_ref": "SEC-1",
        "title": "Unsafe evaluation",
        "explanation": "User input is executed.",
        "evidence": "eval(user_input)\ntail",
    }
    if request.param == "code":
        return (
            SimulationCodeInput(
                kind="code", path="snippet.py", source="eval(user_input)\ntail\n"
            ),
            item,
        )
    item |= (
        {
            "side": "old",
            "start_line": 11,
            "end_line": 12,
            "evidence": "safe(value)\ntail",
        }
        if request.param == "old"
        else {"side": "new", "start_line": 21, "end_line": 22}
    )
    return SimulationDiffInput(kind="diff", unified_diff=PATCH), item


async def test_result_union_serializes_only_available_finding_fields(
    simulation_case: tuple[SimulationCodeInput | SimulationDiffInput, dict[str, Any]],
) -> None:
    code, item = simulation_case
    result = await CodeSimulationService(
        FakeLLM(json.dumps({"findings": [item]}))
    ).simulate(code, request_rules())

    expected = {"findings": [item], "rejected_count": 0}
    assert json.loads(result.model_dump_json()) == expected
    # A future typed consumer must preserve Diff.side as well as the base fields.
    contract: TypeAdapter[CodeSimulationResult | DiffSimulationResult] = TypeAdapter(
        CodeSimulationResult | DiffSimulationResult
    )
    assert json.loads(contract.dump_json(result)) == expected


async def test_result_preserves_each_ref_and_omits_rejected_finding_data(
    simulation_case: tuple[SimulationCodeInput | SimulationDiffInput, dict[str, Any]],
) -> None:
    code, item = simulation_case
    rules = SimulationRules(
        format="rules-md/1",
        markdown=(
            "# Security\n## SEC-1: No eval\nDo not evaluate user input.\n"
            "## SEC-2: Second criterion\nUse a safe parser.\n"
        ),
    )
    second = item | {"rule_ref": "SEC-2"}
    rejected = item | {"rule_ref": "SEC-99", "title": "private-rejected-sentinel"}
    result = await CodeSimulationService(
        FakeLLM(json.dumps({"findings": [second, rejected, item]}))
    ).simulate(code, rules)

    assert json.loads(result.model_dump_json()) == {
        "findings": [second, item],
        "rejected_count": 1,
    }


async def test_empty_result_has_no_invented_summary_or_audit_data(
    simulation_case: tuple[SimulationCodeInput | SimulationDiffInput, dict[str, Any]],
) -> None:
    code, _ = simulation_case
    result = await CodeSimulationService(FakeLLM('{"findings":[]}')).simulate(
        code, request_rules()
    )

    assert json.loads(result.model_dump_json()) == {
        "findings": [],
        "rejected_count": 0,
    }


async def test_title_over_limit_after_masking_rejects_only_that_finding(
    simulation_case: tuple[SimulationCodeInput | SimulationDiffInput, dict[str, Any]],
) -> None:
    code, item = simulation_case
    # A one-character secret expands to three asterisks: 200 -> 202.
    oversized = item | {"title": "x" * 194 + " pwd=x"}
    last = item | {"title": "Second valid finding"}
    result = await CodeSimulationService(
        FakeLLM(json.dumps({"findings": [item, oversized, last]}))
    ).simulate(code, request_rules())

    assert json.loads(result.model_dump_json()) == {
        "findings": [item, last],
        "rejected_count": 1,
    }


async def test_explanation_over_limit_after_masking_rejects_only_that_finding(
    simulation_case: tuple[SimulationCodeInput | SimulationDiffInput, dict[str, Any]],
) -> None:
    code, item = simulation_case
    oversized = item | {"explanation": "x" * 1994 + " pwd=x"}
    last = item | {"title": "Second valid finding"}
    result = await CodeSimulationService(
        FakeLLM(json.dumps({"findings": [item, oversized, last]}))
    ).simulate(code, request_rules())

    assert json.loads(result.model_dump_json()) == {
        "findings": [item, last],
        "rejected_count": 1,
    }


async def test_evidence_over_limit_after_masking_rejects_only_that_finding(
    simulation_case: tuple[SimulationCodeInput | SimulationDiffInput, dict[str, Any]],
) -> None:
    code, item = simulation_case
    evidence = "x" * 994 + " pwd=x"
    if code.kind == "code":
        code = SimulationCodeInput(
            kind="code", path="snippet.py", source=f"{evidence}\ntail\n"
        )
    else:
        code = SimulationDiffInput(
            kind="diff",
            unified_diff=PATCH.replace("safe(value)", evidence).replace(
                "eval(user_input)", evidence
            ),
        )
    item = item | {"evidence": "tail"}
    oversized = item | {"evidence": evidence}
    last = item | {"title": "Second valid finding"}
    result = await CodeSimulationService(
        FakeLLM(json.dumps({"findings": [item, oversized, last]}))
    ).simulate(code, request_rules())

    assert json.loads(result.model_dump_json()) == {
        "findings": [item, last],
        "rejected_count": 1,
    }


@pytest.mark.parametrize("result_model", [CodeSimulationResult, DiffSimulationResult])
def test_result_requires_rejected_count(
    result_model: type[CodeSimulationResult | DiffSimulationResult],
) -> None:
    with pytest.raises(ValidationError) as raised:
        result_model.model_validate({"findings": []})
    assert [(e["loc"], e["type"]) for e in raised.value.errors()] == [
        (("rejected_count",), "missing")
    ]


@pytest.mark.parametrize("result_model", [CodeSimulationResult, DiffSimulationResult])
def test_result_rejects_negative_rejected_count(
    result_model: type[CodeSimulationResult | DiffSimulationResult],
) -> None:
    with pytest.raises(ValidationError) as raised:
        result_model.model_validate({"findings": [], "rejected_count": -1})
    assert [(e["loc"], e["type"]) for e in raised.value.errors()] == [
        (("rejected_count",), "greater_than_equal")
    ]


# Characterize inherited strictness and valid values; no new production behavior.
@pytest.mark.parametrize("result_model", [CodeSimulationResult, DiffSimulationResult])
@pytest.mark.parametrize("count", [False, True, 0.0, 1.5, "0", None])
def test_result_rejected_count_requires_a_strict_integer(
    result_model: type[CodeSimulationResult | DiffSimulationResult], count: object
) -> None:
    with pytest.raises(ValidationError) as raised:
        result_model.model_validate({"findings": [], "rejected_count": count})
    assert [(e["loc"], e["type"]) for e in raised.value.errors()] == [
        (("rejected_count",), "int_type")
    ]


@pytest.mark.parametrize("result_model", [CodeSimulationResult, DiffSimulationResult])
@pytest.mark.parametrize("count", [0, 1, 100])
def test_result_accepts_nonnegative_integer_rejected_count(
    result_model: type[CodeSimulationResult | DiffSimulationResult], count: int
) -> None:
    result = result_model.model_validate({"findings": [], "rejected_count": count})
    assert json.loads(result.model_dump_json()) == {
        "findings": [],
        "rejected_count": count,
    }
