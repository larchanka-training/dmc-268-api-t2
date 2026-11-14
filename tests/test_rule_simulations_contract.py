"""Public Playground contracts, including success with an injected executor."""

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate
from pydantic import ValidationError

from app.api.rule_simulations import SimulationRequest
from app.main import create_app

ROOT = Path(__file__).parents[1]
PATH = "/api/v1/repositories/{id}/rule-simulations"

# Exact successful HTTP fixture values from test_rule_simulations_integration.py.
FINDING = {
    "path": "playground/example.py",
    "start_line": 2,
    "end_line": 3,
    "rule_ref": "SEC-1",
    "title": "Unsafe evaluation",
    "explanation": "User input is executed.",
    "evidence": "eval(private_source_sentinel)",
}
DIFF_FINDINGS = [
    {
        "path": "before.py",
        "side": "old",
        "start_line": 8,
        "end_line": 9,
        "rule_ref": "SEC-1",
        "title": "Unsafe old evaluation",
        "explanation": "Old input is executed.",
        "evidence": "old_eval(value)",
    },
    {
        "path": "after.py",
        "side": "new",
        "start_line": 20,
        "end_line": 21,
        "rule_ref": "SEC-1",
        "title": "Unsafe new evaluation",
        "explanation": "New input is executed.",
        "evidence": "eval(value)",
    },
]


def document() -> Any:
    return yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))


def test_request_contract_is_strict_discriminated_and_examples_match_dto() -> None:
    api = document()
    request = api["paths"][PATH]["post"]["requestBody"]
    assert request["required"] is True
    media = request["content"]["application/json"]
    assert media["schema"] == {"$ref": "#/components/schemas/SimulationRequest"}
    schemas = api["components"]["schemas"]
    for name, fields in (
        ("SimulationRequest", {"input", "rules"}),
        ("SimulationCodeInput", {"kind", "path", "source"}),
        ("SimulationDiffInput", {"kind", "unified_diff"}),
        ("SimulationRules", {"format", "markdown"}),
    ):
        schema = schemas[name]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == fields == set(schema["properties"])
    union = schemas["SimulationRequest"]["properties"]["input"]
    assert union["discriminator"] == {
        "propertyName": "kind",
        "mapping": {
            "code": "#/components/schemas/SimulationCodeInput",
            "diff": "#/components/schemas/SimulationDiffInput",
        },
    }
    assert union["oneOf"] == [
        {"$ref": "#/components/schemas/SimulationCodeInput"},
        {"$ref": "#/components/schemas/SimulationDiffInput"},
    ]
    for name, field, limit in (
        ("SimulationCodeInput", "path", 512),
        ("SimulationCodeInput", "source", 65536),
        ("SimulationDiffInput", "unified_diff", 131072),
        ("SimulationRules", "markdown", 65536),
    ):
        prop = schemas[name]["properties"][field]
        assert prop["x-max-utf8-bytes"] == limit
        assert "maxLength" not in prop
    assert (
        schemas["SimulationCodeInput"]["properties"]["source"]["x-max-logical-lines"]
        == 500  # noqa: PLR2004 -- existing logical line cap
    )
    assert (
        schemas["SimulationDiffInput"]["properties"]["unified_diff"]["x-max-files"]
        == 50  # noqa: PLR2004 -- existing raw file cap
    )
    assert (
        schemas["SimulationDiffInput"]["properties"]["unified_diff"][
            "x-max-changed-lines"
        ]
        == 2000  # noqa: PLR2004 -- existing changed line cap
    )
    validator = Draft202012Validator(
        {**media["schema"], "components": api["components"]}
    )
    assert set(media["examples"]) == {"code", "diff"}
    for example in media["examples"].values():
        payload = example["value"]
        validator.validate(payload)
        SimulationRequest.model_validate(payload)
        for level in (None, "input", "rules"):
            invalid = copy.deepcopy(payload)
            target = invalid if level is None else invalid[level]
            target["unexpected"] = True
            assert not validator.is_valid(invalid)
            with pytest.raises(ValidationError):
                SimulationRequest.model_validate(invalid)
        invalid = copy.deepcopy(payload)
        invalid["input"]["kind"] = "unknown"
        assert not validator.is_valid(invalid)
        invalid = copy.deepcopy(payload)
        del invalid["rules"]["format"]
        assert not validator.is_valid(invalid)
        invalid = copy.deepcopy(payload)
        invalid["rules"]["markdown"] = 123
        assert not validator.is_valid(invalid)
        invalid = copy.deepcopy(payload)
        invalid["rules"]["format"] = "rules-md/2"
        assert not validator.is_valid(invalid)
    code = copy.deepcopy(media["examples"]["code"]["value"])
    for path in (
        "./a.py",
        "a//b.py",
        "a/../b.py",
        "C:a.py",
        "a\\b.py",
        "a\x7f.py",
        "/a.py",
        "a/",
        "",
    ):
        code["input"]["path"] = path
        assert not validator.is_valid(code), path
        with pytest.raises(ValidationError):
            SimulationRequest.model_validate(code)


def test_transport_contract_requires_session_origin_csrf_and_preliminary_key() -> None:
    api = document()
    operation = api["paths"][PATH]["post"]
    assert operation["security"] == [{"cookieAuth": [], "csrfHeader": []}]
    assert api["components"]["securitySchemes"]["cookieAuth"]["in"] == "cookie"
    parameters = [
        api["components"]["parameters"][p["$ref"].rsplit("/", 1)[-1]]
        if "$ref" in p
        else p
        for p in operation["parameters"]
    ]
    headers = {p["name"]: p for p in parameters if p["in"] == "header"}
    assert set(headers) == {"Origin", "X-CSRF-Token", "Idempotency-Key"}
    assert all(p["required"] for p in headers.values())
    key = headers["Idempotency-Key"]
    assert "exact raw bytes" in key["description"]
    assert "result replay" in key["description"]
    validator = Draft202012Validator(key["schema"])
    for value in ("not-a-uuid", " old key ", "018e4a00-0000-7000-8000-000000000000"):
        validator.validate(value)
    for value in ("", " \t ", "key,key", ",key", "key,"):
        assert not validator.is_valid(value)
    assert api["components"]["parameters"]["IdempotencyKey"] == {
        "name": "Idempotency-Key",
        "in": "header",
        "required": True,
        "schema": {"type": "string", "minLength": 1},
    }
    body = operation["requestBody"]
    assert set(body["content"]) == {"application/json"}
    assert "charset" in body["description"]
    assert "exactly one" in body["description"]


def test_body_too_large_error_is_in_the_closed_authoritative_error_schema() -> None:
    api = document()
    schema = api["components"]["schemas"]["Error"]
    assert schema["additionalProperties"] is False
    Draft202012Validator({**schema, "components": api["components"]}).validate(
        {
            "code": "SIMULATION_INPUT_TOO_LARGE",
            "message": "Simulation input is too large.",
            "request_id": "req_contract",
            "retryable": False,
            "retry_after": None,
        }
    )


def test_422_contract_allows_generic_and_only_specialized_rules_diagnostics() -> None:
    api = document()
    response = api["paths"][PATH]["post"]["responses"]["422"]
    schema = response["content"]["application/json"]["schema"]
    assert schema == {
        "oneOf": [
            {"$ref": "#/components/schemas/Error"},
            {"$ref": "#/components/schemas/SimulationRulesValidationError"},
        ]
    }
    generic = {
        "code": "VALIDATION_FAILED",
        "message": "Request is invalid.",
        "request_id": "req_contract",
        "retryable": False,
        "retry_after": None,
    }
    specialized = generic | {
        "diagnostics": [
            {
                "code": "RULES_DUPLICATE_REF",
                "line": 7,
                "message": "Rule references must be unique.",
            }
        ],
        "diagnostics_truncated": False,
    }
    validator = Draft202012Validator({**schema, "components": api["components"]})
    validator.validate(generic)
    validator.validate(specialized)
    assert not validator.is_valid(specialized | {"raw": "private draft"})
    assert not validator.is_valid(specialized | {"diagnostics_truncated": None})
    error = Draft202012Validator(
        {"$ref": "#/components/schemas/Error", "components": api["components"]}
    )
    assert not error.is_valid(specialized)


def test_generated_operation_documents_success_and_disabled_by_default_executor() -> (
    None
):
    api = create_app().openapi()
    operation = api["paths"][PATH]["post"]
    assert operation["operationId"] == "createRuleSimulation"
    assert set(operation["responses"]) == {
        "200",
        "401",
        "403",
        "404",
        "413",
        "422",
        "503",
    }
    success = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert success["anyOf"] == [
        {"$ref": "#/components/schemas/CodeSimulationResult"},
        {"$ref": "#/components/schemas/DiffSimulationResult"},
    ]
    assert "oneOf" not in success
    assert next(iter(operation["responses"])) == "200"
    assert operation["responses"]["503"]["description"] == "SERVICE_UNAVAILABLE"
    assert operation["responses"]["422"]["description"] == "VALIDATION_FAILED"
    description = operation["description"]
    assert "provided executor" in description
    assert "disabled by default" in description
    assert "503 SERVICE_UNAVAILABLE" in description
    assert "Staging execution is not enabled" in description
    assert "configured admission coordinator" in description
    assert "Duplicate keys suppress new dispatch" in description
    assert "result replay is not implemented" in description
    assert "durable exactly-once" in description


@pytest.fixture(scope="module", params=["static", "generated"])
def success_api(request: pytest.FixtureRequest) -> Any:
    return document() if request.param == "static" else create_app().openapi()


def success_validator(api: Any) -> Draft202012Validator:
    schema = api["paths"][PATH]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    return Draft202012Validator({**schema, "components": api["components"]})


def test_success_openapi_is_valid_and_all_references_resolve(success_api: Any) -> None:
    validate(success_api)

    def check_refs(value: Any) -> None:
        if isinstance(value, dict):
            if "$ref" in value:
                ref = value["$ref"]
                assert ref.startswith("#/")
                target = success_api
                for part in ref[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            for child in value.values():
                check_refs(child)
        elif isinstance(value, list):
            for child in value:
                check_refs(child)

    check_refs(success_api)


def test_static_and_generated_success_schemas_have_the_same_constraints() -> None:
    def without_titles(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: without_titles(v)
                for k, v in value.items()
                if not (k == "title" and isinstance(v, str))
            }
        if isinstance(value, list):
            return [without_titles(v) for v in value]
        return value

    static = document()["components"]["schemas"]
    generated = create_app().openapi()["components"]["schemas"]
    for name in (
        "CodeSimulationResult",
        "DiffSimulationResult",
        "CodeFinding",
        "DiffFinding",
    ):
        assert without_titles(static[name]) == without_titles(generated[name])


@pytest.mark.parametrize(
    "findings",
    [[FINDING], [DIFF_FINDINGS[0]], [DIFF_FINDINGS[1]], DIFF_FINDINGS, []],
    ids=["code", "diff-old", "diff-new", "diff-both", "empty"],
)
def test_success_contract_accepts_existing_http_results(
    success_api: Any, findings: list[dict[str, Any]]
) -> None:
    success_validator(success_api).validate({"findings": findings, "rejected_count": 0})


@pytest.mark.parametrize(
    "finding", [FINDING, *DIFF_FINDINGS], ids=["code", "old", "new"]
)
@pytest.mark.parametrize(
    "invalid_case",
    [
        "missing-rejected-count",
        "negative-rejected-count",
        "noninteger-rejected-count",
        "missing-findings",
        "extra-result-field",
        "extra-finding-field",
        "too-many-findings",
        "missing-path",
        "missing-start_line",
        "missing-end_line",
        "missing-rule_ref",
        "missing-title",
        "missing-explanation",
        "missing-evidence",
        "invalid-side",
        "zero-start-line",
        "zero-end-line",
        "empty-rule_ref",
        "empty-title",
        "empty-explanation",
        "empty-evidence",
        "long-rule_ref",
        "long-title",
        "long-explanation",
        "long-evidence",
    ],
)
def test_success_contract_rejects_invalid_http_results(
    success_api: Any, finding: dict[str, Any], invalid_case: str
) -> None:
    response: dict[str, Any] = {
        "findings": [copy.deepcopy(finding)],
        "rejected_count": 1,
    }
    validator = success_validator(success_api)
    validator.validate(response)
    item = response["findings"][0]
    if invalid_case == "missing-rejected-count":
        del response["rejected_count"]
    elif invalid_case == "negative-rejected-count":
        response["rejected_count"] = -1
    elif invalid_case == "noninteger-rejected-count":
        response["rejected_count"] = 0.5
    elif invalid_case == "missing-findings":
        del response["findings"]
    elif invalid_case == "extra-result-field":
        response["model_context"] = "private"
    elif invalid_case == "extra-finding-field":
        item["confidence"] = 1
    elif invalid_case == "too-many-findings":
        response["findings"] *= 101
    elif invalid_case.startswith("missing-"):
        del item[invalid_case.removeprefix("missing-")]
    elif invalid_case == "invalid-side":
        item["side"] = "both"
    elif invalid_case.startswith("zero-"):
        item[invalid_case.removeprefix("zero-").replace("-", "_")] = 0
    elif invalid_case.startswith("empty-"):
        item[invalid_case.removeprefix("empty-")] = ""
    else:
        field = invalid_case.removeprefix("long-")
        limits = {"rule_ref": 32, "title": 200, "explanation": 2000, "evidence": 1000}
        item[field] = "x" * (limits[field] + 1)
    assert not validator.is_valid(response), invalid_case


def test_static_and_generated_execution_descriptions_agree() -> None:
    static = document()["paths"][PATH]["post"]["description"]
    generated = create_app().openapi()["paths"][PATH]["post"]["description"]
    for text in (static, generated):
        assert "configured admission coordinator" in text
        assert "disabled by default" in text
        assert "Duplicate keys suppress new dispatch" in text
        assert "result replay is not implemented" in text
        assert "durable exactly-once" in text
