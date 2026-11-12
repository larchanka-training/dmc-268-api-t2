"""Rule simulation boundary; execution requires an explicit dependency override."""

import json
import re
from contextlib import suppress
from ntpath import splitdrive
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, Response

from app.adapters.personal_github import (
    PersonalCredentialUnavailableError,
    personal_github_connection,
)
from app.api.auth import VerifiedMutationIdentity, get_verified_mutation_identity
from app.api.errors import ApiError
from app.api.repository_settings import _require_admin
from app.context.rules import ParsedRules, parse_rules
from app.core.config import Settings, get_settings
from app.db.models import Repository, RepositoryAccess
from app.db.session import get_session
from app.review.schemas.finding import RelPath
from app.review.simulation import (
    CodeSimulationResult,
    CodeSimulationService,
    DiffSimulationResult,
)
from app.vcs.diff import InvalidDiff, parse_diff

router = APIRouter(prefix="/api/v1/repositories")
MAX_SIMULATION_BODY_BYTES = 262144
MAX_SIMULATION_PATH_BYTES = 512
MAX_SIMULATION_SOURCE_BYTES = 65536
MAX_SIMULATION_SOURCE_LINES = 500
MAX_SIMULATION_DIFF_BYTES = 131072
MAX_RULE_DIAGNOSTICS = 20
MAX_RULE_DIAGNOSTICS_BYTES = 8192


class SimulationModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class SimulationCodeInput(SimulationModel):
    kind: Literal["code"]
    path: RelPath
    source: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, path: str) -> str:
        if splitdrive(path)[0]:
            msg = "path must be repository-relative"
            raise ValueError(msg)
        if re.search(r"[\x00-\x1f\x7f]", path):
            msg = "path must not contain ASCII control characters"
            raise ValueError(msg)
        if len(path.encode("utf-8")) > MAX_SIMULATION_PATH_BYTES:
            msg = "path exceeds the byte limit"
            raise ValueError(msg)
        return path

    @field_validator("source")
    @classmethod
    def validate_source(cls, source: str) -> str:
        if not source.strip():
            msg = "source must not be blank"
            raise ValueError(msg)
        if len(source.encode("utf-8")) > MAX_SIMULATION_SOURCE_BYTES:
            msg = "source exceeds the byte limit"
            raise ValueError(msg)
        # CRLF is one boundary; a terminal newline adds no empty logical line.
        if len(source.splitlines()) > MAX_SIMULATION_SOURCE_LINES:
            msg = "source exceeds the logical line limit"
            raise ValueError(msg)
        return source


class SimulationDiffInput(SimulationModel):
    kind: Literal["diff"]
    unified_diff: str

    @field_validator("unified_diff")
    @classmethod
    def validate_diff(cls, diff: str) -> str:
        if not diff.strip():
            msg = "unified_diff must not be blank"
            raise ValueError(msg)
        if len(diff.encode("utf-8")) > MAX_SIMULATION_DIFF_BYTES:
            msg = "unified_diff exceeds the byte limit"
            raise ValueError(msg)
        return diff


class SimulationRules(SimulationModel):
    format: Literal["rules-md/1"]
    markdown: str


class SimulationRequest(SimulationModel):
    input: Annotated[
        SimulationCodeInput | SimulationDiffInput, Field(discriminator="kind")
    ]
    rules: SimulationRules


_RULE_DIAGNOSTICS = {
    "duplicate_ref": ("RULES_DUPLICATE_REF", "Rule references must be unique."),
    "invalid_structure": (
        "RULES_INVALID_SECTION",
        "Unsupported top-level rules section.",
    ),
    "invalid_severity": (
        "RULES_INVALID_SEVERITY",
        "Use a supported severity value.",
    ),
    "invalid_ignore": (
        "RULES_INVALID_IGNORE",
        "Use the supported Ignore entry format.",
    ),
    "unclosed_fence": ("RULES_UNCLOSED_FENCE", "Close the Markdown code fence."),
    "file_too_large": ("RULES_TOO_LARGE", "Rules exceed the UTF-8 byte limit."),
    "combined_rule_limit": (
        "RULES_CANONICAL_LIMIT",
        "Parsed rules exceed the instruction limit.",
    ),
}
_INVALID_RULE_DIAGNOSTIC = ("RULES_INVALID", "Rules could not be validated.")


def _project_rule_diagnostic(raw: str) -> dict[str, object]:
    code, message = _INVALID_RULE_DIAGNOSTIC
    line = None
    match = re.fullmatch(r"([a-z_]+):([0-9]+)", raw) if isinstance(raw, str) else None
    if match and match[1] in _RULE_DIAGNOSTICS:
        try:
            line = int(match[2]) or None
        except ValueError:
            pass  # Invalid/oversized internal coordinates never reach the response.
        else:
            code, message = _RULE_DIAGNOSTICS[match[1]]
    return {"code": code, "line": line, "message": message}


def project_rules_diagnostics(
    rules: ParsedRules,
) -> tuple[list[dict[str, object]], bool]:
    """Project rejected parser results into bounded, static public diagnostics."""
    if not rules.diagnostics:
        code, message = (
            ("RULES_NO_ACTIVE_CRITERIA", "Add at least one active rule criterion.")
            if rules.state in {"empty", "valid"}
            else _INVALID_RULE_DIAGNOSTIC
        )
        return [{"code": code, "line": None, "message": message}], False
    diagnostics = []
    for raw in rules.diagnostics[:MAX_RULE_DIAGNOSTICS]:
        diagnostics.append(_project_rule_diagnostic(raw))
        if (
            len(json.dumps(diagnostics, ensure_ascii=False).encode("utf-8"))
            > MAX_RULE_DIAGNOSTICS_BYTES
        ):
            diagnostics.pop()
            break
    return diagnostics, len(rules.diagnostics) > len(diagnostics)


def _validate_simulation_headers(request: Request) -> None:
    content_types = request.headers.getlist("content-type")
    if (
        len(content_types) != 1
        or re.fullmatch(
            r"[ \t]*application/json[ \t]*"
            r'(?:;[ \t]*charset[ \t]*=[ \t]*(?:utf-8|"utf-8")[ \t]*)?',
            content_types[0],
            flags=re.IGNORECASE | re.ASCII,
        )
        is None
    ):
        raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.")
    # Transport gate only; key identity, replay and storage are not implemented.
    keys = request.headers.getlist("idempotency-key")
    if len(keys) != 1 or not keys[0].strip() or "," in keys[0]:
        raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.")


async def get_simulation_executor() -> CodeSimulationService | None:
    """Unavailable by default; no production provider is composed here."""
    return None


@router.post(
    "/{id}/rule-simulations",
    status_code=200,
    operation_id="createRuleSimulation",
    description=(
        "A provided executor can return HTTP 200 with a Code or Diff result. "
        "The executor is disabled by default, so a valid request in the normal "
        "configuration returns canonical 503 SERVICE_UNAVAILABLE; dependency "
        "failures can also return 503. Staging execution is not enabled. "
        "Idempotent replay and durable admission are not implemented."
    ),
    responses={
        200: {"model": CodeSimulationResult | DiffSimulationResult},
        401: {"description": "AUTHENTICATION_REQUIRED"},
        403: {"description": "ACCESS_DENIED"},
        404: {"description": "RESOURCE_NOT_FOUND"},
        413: {"description": "SIMULATION_INPUT_TOO_LARGE"},
        422: {"description": "VALIDATION_FAILED"},
        503: {"description": "SERVICE_UNAVAILABLE"},
    },
)
async def create_rule_simulation(  # noqa: PLR0912, PLR0913, PLR0915, PLR0917 -- preserve ordered HTTP guards and explicit injected dependencies
    id: str,
    request: Request,
    identity: Annotated[
        VerifiedMutationIdentity, Depends(get_verified_mutation_identity)
    ],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    executor: Annotated[CodeSimulationService | None, Depends(get_simulation_executor)],
) -> Response:
    try:
        repository_id = UUID(id)
    except ValueError:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.") from None
    try:
        external_id = await session.scalar(
            select(Repository.external_id)
            .join(RepositoryAccess)
            .where(
                Repository.id == repository_id,
                RepositoryAccess.user_id == identity.user_id,
            )
        )
        await session.rollback()
    except SQLAlchemyError, OSError:
        raise ApiError(
            503, "SERVICE_UNAVAILABLE", "Service is unavailable.", retryable=True
        ) from None
    if external_id is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")
    try:
        async with personal_github_connection(
            identity.user_id, settings, session
        ) as reader:
            await _require_admin(reader, external_id)
    except PersonalCredentialUnavailableError:
        raise ApiError(
            503, "SERVICE_UNAVAILABLE", "Service is unavailable.", retryable=True
        ) from None
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_SIMULATION_BODY_BYTES:
            raise ApiError(
                413, "SIMULATION_INPUT_TOO_LARGE", "Simulation input is too large."
            )
        body.extend(chunk)
    _validate_simulation_headers(request)
    try:
        payload = SimulationRequest.model_validate_json(body)
    except ValidationError:
        raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.") from None
    if isinstance(payload.input, SimulationDiffInput):
        try:
            parsed = parse_diff(payload.input.unified_diff)
        except InvalidDiff:
            raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.") from None
        if not any(
            line["kind"] in {"added", "deleted"}
            for file in parsed.files
            for hunk in file["diff"]["hunks"]
            for line in hunk["lines"]
        ):
            raise ApiError(422, "VALIDATION_FAILED", "Request is invalid.")
    rules = parse_rules(payload.rules.markdown, db_instructions=())
    if rules.state != "valid" or rules.diagnostics or not rules.instructions:
        diagnostics, truncated = project_rules_diagnostics(rules)
        raise ApiError(
            422,
            "VALIDATION_FAILED",
            "Request is invalid.",
            diagnostics=diagnostics,
            diagnostics_truncated=truncated,
        )
    if executor is None:
        raise ApiError(503, "SERVICE_UNAVAILABLE", "Service is unavailable.")
    try:
        fresh_external_id = await session.scalar(
            select(Repository.external_id)
            .join(RepositoryAccess)
            .where(
                Repository.id == repository_id,
                RepositoryAccess.user_id == identity.user_id,
            )
        )
        await session.rollback()
    except SQLAlchemyError, OSError:
        raise ApiError(
            503, "SERVICE_UNAVAILABLE", "Service is unavailable.", retryable=True
        ) from None
    if fresh_external_id is None or fresh_external_id != external_id:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource was not found.")
    try:
        async with personal_github_connection(
            identity.user_id, settings, session
        ) as reader:
            await _require_admin(reader, fresh_external_id)
    except PersonalCredentialUnavailableError:
        raise ApiError(
            503, "SERVICE_UNAVAILABLE", "Service is unavailable.", retryable=True
        ) from None
    response = None
    with suppress(Exception):
        result = await executor.simulate(payload.input, payload.rules)
        response = JSONResponse(status_code=200, content=result.model_dump(mode="json"))
    # Raise outside the handler so the service/provider exception is not retained.
    if response is None:
        raise ApiError(503, "SERVICE_UNAVAILABLE", "Service is unavailable.")
    return response
