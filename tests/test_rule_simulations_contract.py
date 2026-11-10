"""The public, authoritative contract for the temporarily unavailable Playground."""

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.api.rule_simulations import SimulationRequest
from app.main import create_app

ROOT = Path(__file__).parents[1]
PATH = "/api/v1/repositories/{id}/rule-simulations"


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
    assert "transport gate only" in key["description"]
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


def test_generated_operation_has_no_success_status_and_names_temporary_503() -> None:
    api = create_app().openapi()
    operation = api["paths"][PATH]["post"]
    assert operation["operationId"] == "createRuleSimulation"
    assert set(operation["responses"]) == {"401", "403", "404", "413", "422", "503"}
    assert operation["responses"]["503"]["description"] == "SERVICE_UNAVAILABLE"
    assert operation["responses"]["422"]["description"] == "VALIDATION_FAILED"
    assert "temporarily unavailable" in operation["description"]
