"""ReviewService.review_chunk end to end over mocked providers (spec §8)."""

import dataclasses
import hashlib
import json
import random
import shutil
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator
from pydantic import SecretStr

from app.application.ports import BuiltContextPayload, LlmGatewayPort
from app.core.config import LLM, Eurorouter, Ollama
from app.llm.budget import BudgetLimits, RunBudget
from app.llm.providers import build_gateway
from app.llm.tokens import ConservativeTokenCounter
from app.review.port_adapter import ChunkReviewFailedError, LlmGatewayAdapter
from app.review.prompt import PromptBuilder, PromptConfigError
from app.review.renderers import v1
from app.review.schemas.context import ContextPayload
from app.review.schemas.result import ChunkStatus, LLMReviewOutput, ReviewChunkOutcome
from app.review.service import ReviewService
from app.review.validation import FindingValidator

_PROMPTS = Path(__file__).resolve().parents[2] / ".agents" / "reviewer"


def _finding(line: int) -> dict[str, Any]:
    return {
        "path": "src/review.py",
        "side": "NEW",
        "start_line": line,
        "end_line": line,
        "category": "correctness",
        "severity": "high",
        "title": f"Проблема в строке {line}",
        "explanation": "ValidationError роняет обработчик.",
        "evidence": f"Строка {line} вызывает model_validate без обработки ошибок.",
        "recommendation": "Перехватить ValidationError.",
        "proposed_diff_fix": None,
    }


def _output(*findings: dict[str, Any]) -> str:
    return json.dumps(
        {"summary": "Сводка", "findings": list(findings), "limitations": []},
        ensure_ascii=False,
    )


class ScriptedProviders:
    """Answers requests in order: a status code or model output text."""

    def __init__(self) -> None:
        self.script: deque[int | str] = deque()
        self.requests: list[dict[str, Any]] = []
        self.hosts: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.hosts.append(request.url.host)
        item = self.script.popleft() if self.script else _output()
        if item == 400:  # noqa: PLR2004
            error = {"error": {"code": "context_length_exceeded"}}
            return httpx.Response(400, json=error)
        if isinstance(item, int):
            return httpx.Response(item)
        if request.url.host == "ollama":
            return httpx.Response(
                200,
                json={"model": "qwen", "message": {"content": item}, "eval_count": 7},
            )
        return httpx.Response(
            200,
            json={
                "model": "euro-routed",
                "choices": [{"message": {"content": item}}],
                "usage": {"prompt_tokens": 1500, "completion_tokens": 120},
            },
        )


async def _no_sleep(_: float) -> None:
    return None


def _settings() -> LLM:
    return LLM(
        eurorouter=Eurorouter(
            base_url="https://euro.example/v1",
            model="euro",
            api_keys=[SecretStr("key-one")],
        ),
        ollama=Ollama(base_url="http://ollama:11434", model="qwen"),
    )


@pytest.fixture
def providers() -> ScriptedProviders:
    return ScriptedProviders()


@pytest.fixture
def service(providers: ScriptedProviders) -> ReviewService:
    settings = _settings()
    counter = ConservativeTokenCounter()
    gateway = build_gateway(
        settings,
        httpx.AsyncClient(transport=httpx.MockTransport(providers.handler)),
        sleep=_no_sleep,
        rng=random.Random(0),
    )
    return ReviewService(
        gateway=gateway,
        prompt_builder=PromptBuilder.load(_PROMPTS, "v1", counter),
        validator=FindingValidator(),
        token_counter=counter,
        settings=settings,
    )


@pytest.fixture
def payload(payload_data: dict[str, Any]) -> ContextPayload:
    return ContextPayload.model_validate(payload_data)


def _budget(max_requests: int = 8) -> RunBudget:
    return RunBudget(
        BudgetLimits(
            max_requests=max_requests,
            max_input_tokens=80_000,
            max_output_tokens=16_000,
            deadline_s=600,
        )
    )


async def test_unverified_builder_cannot_execute_review(
    providers: ScriptedProviders, payload: ContextPayload
) -> None:
    counter = ConservativeTokenCounter()
    service = ReviewService(
        gateway=build_gateway(
            _settings(),
            httpx.AsyncClient(transport=httpx.MockTransport(providers.handler)),
        ),
        prompt_builder=PromptBuilder(prompt_version="v1", system="test", few_shots=[]),
        validator=FindingValidator(),
        token_counter=counter,
        settings=_settings(),
    )
    with pytest.raises(PromptConfigError):
        await service.review_chunk(payload, _budget())
    assert providers.requests == []


@pytest.mark.parametrize("provider", ["eurorouter", "ollama"])
async def test_provider_receives_canonical_frozen_schema(
    service: ReviewService,
    providers: ScriptedProviders,
    payload: ContextPayload,
    provider: str,
) -> None:
    if provider == "ollama":
        providers.script.extend([503, 503])
    providers.script.append(_output())
    result = await service.review_chunk(payload, _budget())
    assert result.status == ChunkStatus.COMPLETED
    sent = providers.requests[-1]
    schema = (
        sent["format"]
        if provider == "ollama"
        else sent["response_format"]["json_schema"]["schema"]
    )
    expected = json.loads(
        (Path(__file__).parent / "fixtures/v1-provider-schema.json").read_text("utf-8")
    )
    assert schema == expected


@pytest.mark.parametrize("provider", ["eurorouter", "ollama"])
async def test_saved_v1_http_is_unchanged_after_current_v2_deployment(
    tmp_path: Path,
    payload: ContextPayload,
    provider: str,
    providers: ScriptedProviders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "reviewer"
    shutil.copytree(_PROMPTS, root)
    shutil.copytree(root / "v1", root / "v2", dirs_exist_ok=True)
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    current = json.loads(json.dumps(manifest["versions"]["v1"]))
    for artifact in current["artifacts"]:
        if artifact["path"].startswith("v1/"):
            artifact["path"] = artifact["path"].replace("v1/", "v2/", 1)
    (root / "v2/system.md").write_text("Current v2 system", "utf-8")
    for artifact in current["artifacts"]:
        if artifact["role"] == "system":
            artifact["sha256"] = hashlib.sha256(
                (root / artifact["path"]).read_bytes()
            ).hexdigest()
    manifest["versions"]["v2"] = current
    (root / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    assert (
        PromptBuilder.load(root, "v2")
        .build(payload, max_input_tokens=10000)
        .messages[0]
        .content
        == "Current v2 system"
    )
    monkeypatch.setattr(v1, "_TRIMMED_BODY_CHARS", 1)
    monkeypatch.setattr(v1, "_serialize_output", lambda _: "CURRENT SERIALIZER")
    monkeypatch.setattr(
        LLMReviewOutput, "model_dump_json", lambda *a, **kw: "CURRENT MODEL"
    )
    descriptor = json.loads(
        (Path(__file__).parent / "fixtures/v1-descriptor.json").read_text("utf-8")
    )
    saved_digest = hashlib.sha256(
        json.dumps(
            descriptor,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    counter = ConservativeTokenCounter()
    settings = _settings().model_copy(update={"prompt_version": "v2"})
    if provider == "ollama":
        settings = settings.model_copy(update={"primary_provider": "ollama"})
    service = ReviewService(
        build_gateway(
            settings,
            httpx.AsyncClient(transport=httpx.MockTransport(providers.handler)),
        ),
        PromptBuilder.load(root, "v1", counter, expected_digest=saved_digest),
        FindingValidator(),
        counter,
        settings,
    )
    providers.script.extend(['{"summary": "x"}', _output()])
    result = await service.review_chunk(payload, _budget())
    assert result.status == ChunkStatus.COMPLETED
    golden = json.loads(
        (Path(__file__).parent / "fixtures/v1-messages.json").read_text("utf-8")
    )
    assert providers.requests[0]["messages"] == golden["initial"]
    assert providers.requests[1]["messages"] == golden["repair"]
    schema = json.loads(
        (Path(__file__).parent / "fixtures/v1-provider-schema.json").read_text("utf-8")
    )
    for request in providers.requests:
        sent_schema = (
            request["format"]
            if provider == "ollama"
            else request["response_format"]["json_schema"]["schema"]
        )
        assert sent_schema == schema


async def test_valid_answer_gives_completed_result(
    service: ReviewService, providers: ScriptedProviders, payload: ContextPayload
) -> None:
    providers.script.append(_output(_finding(39), _finding(38)))

    result = await service.review_chunk(payload, _budget())

    assert isinstance(result, ReviewChunkOutcome)
    assert result.status == ChunkStatus.COMPLETED
    assert [f.start_line for f in result.findings] == [39]
    assert result.rejected_findings_count == 1  # line 38 is a context line
    assert (result.review_id, result.chunk_id) == ("rev_01", "chunk_001")
    assert (result.provider, result.model) == ("eurorouter", "euro-routed")
    assert result.prompt_version == "v1"
    assert result.summary == "Сводка"
    assert result.error_code is None
    assert (result.usage.requests, result.usage.input_tokens) == (1, 1500)


@pytest.mark.parametrize(
    "case",
    [
        ([503, 503, 503], 8, "llm_unavailable"),
        ([422], 8, "provider_request_error"),
        ([], 0, "budget_exceeded"),
    ],
)
async def test_expected_failures_give_failed_result(
    service: ReviewService,
    providers: ScriptedProviders,
    payload: ContextPayload,
    case: tuple[list[int], int, str],
) -> None:
    script, max_requests, error_code = case
    providers.script.extend(script)

    result = await service.review_chunk(payload, _budget(max_requests))

    assert result.status == ChunkStatus.FAILED
    assert result.error_code == error_code
    assert (result.summary, result.findings) == (None, [])
    assert result.usage.requests == len(script)
    assert result.provider == "eurorouter"
    assert result.model == "euro"


async def test_invalid_output_is_repaired_once(
    service: ReviewService, providers: ScriptedProviders, payload: ContextPayload
) -> None:
    providers.script.extend(['{"summary": "x"}', _output(_finding(39))])
    budget = _budget()

    result = await service.review_chunk(payload, budget)

    assert result.status == ChunkStatus.COMPLETED
    assert [f.start_line for f in result.findings] == [39]
    assert result.usage.requests == 2  # noqa: PLR2004
    repair = providers.requests[1]["messages"]
    assert repair[-2] == {"role": "assistant", "content": '{"summary": "x"}'}
    assert "findings: Field required" in repair[-1]["content"]
    assert budget.try_use_format_repair() is False  # the run allowance is spent


async def test_invalid_output_after_repair_fails_the_chunk(
    service: ReviewService, providers: ScriptedProviders, payload: ContextPayload
) -> None:
    providers.script.extend(["not json", "still not json", "and again"])
    budget = _budget()

    first = await service.review_chunk(payload, budget)
    second = await service.review_chunk(payload, budget)  # repair already spent

    assert (first.status, first.error_code) == (
        ChunkStatus.FAILED,
        "invalid_llm_output",
    )
    assert first.usage.requests == 2  # noqa: PLR2004
    assert (second.error_code, second.usage.requests) == ("invalid_llm_output", 1)


async def test_context_overflow_rebuilds_the_prompt_smaller_once(
    service: ReviewService,
    providers: ScriptedProviders,
    payload_data: dict[str, Any],
) -> None:
    # Grow commit messages until the full prompt no longer fits the 7 500-token
    # rebuild limit (0.75 x 10 000) while still fitting 10 000.
    builder = PromptBuilder.load(_PROMPTS, "v1")
    commits: list[str] = payload_data["metadata"]["commit_messages"]
    while (
        builder.build(
            ContextPayload.model_validate(payload_data), max_input_tokens=10**6
        ).estimated_tokens
        <= 7_600  # noqa: PLR2004
    ):
        commits.append(f"Коммит номер {len(commits)}: описание изменений")
    payload = ContextPayload.model_validate(payload_data)
    providers.script.extend([400, _output()])

    result = await service.review_chunk(payload, _budget())

    assert result.status == ChunkStatus.COMPLETED
    assert result.usage.requests == 2  # noqa: PLR2004
    first, second = (json.dumps(r["messages"]) for r in providers.requests)
    assert len(second) < len(first)
    assert "Метаданные PR сокращены из-за лимита токенов." in result.limitations


async def test_repeated_context_overflow_fails_the_chunk(
    service: ReviewService, providers: ScriptedProviders, payload: ContextPayload
) -> None:
    providers.script.extend([400, 400])

    result = await service.review_chunk(payload, _budget())

    assert (result.status, result.error_code) == (
        ChunkStatus.FAILED,
        "context_overflow",
    )
    assert result.usage.requests == 2  # noqa: PLR2004


async def test_fallback_provider_is_recorded_in_the_result(
    service: ReviewService, providers: ScriptedProviders, payload: ContextPayload
) -> None:
    providers.script.extend([503, 503, _output()])

    result = await service.review_chunk(payload, _budget())

    assert result.status == ChunkStatus.COMPLETED
    assert (result.provider, result.model) == ("ollama", "qwen")
    assert providers.hosts == ["euro.example", "euro.example", "ollama"]
    assert "Чанк обработан резервным провайдером (ollama/qwen)." in result.limitations
    assert (result.usage.input_tokens, result.usage.output_tokens) == (None, 7)


async def test_diff_too_large_for_the_prompt_fails_the_chunk(
    service: ReviewService, providers: ScriptedProviders, payload_data: dict[str, Any]
) -> None:
    lines = payload_data["files"][0]["diff"]["hunks"][0]["lines"]
    lines[2]["text"] = "x" * 40_000
    payload = ContextPayload.model_validate(payload_data)

    result = await service.review_chunk(payload, _budget())

    assert (result.status, result.error_code) == (
        ChunkStatus.FAILED,
        "prompt_too_large",
    )
    assert providers.requests == []


def _grow_diff_until(payload_data: dict[str, Any], min_tokens: int) -> ContextPayload:
    """Lengthen the added line until even a minimal-metadata prompt exceeds
    ``min_tokens`` (the diff is never trimmed)."""
    builder = PromptBuilder.load(_PROMPTS, "v1")
    line = payload_data["files"][0]["diff"]["hunks"][0]["lines"][2]
    payload_data["metadata"]["commit_messages"] = []
    while True:
        payload = ContextPayload.model_validate(payload_data)
        if builder.build(payload, max_input_tokens=10**6).estimated_tokens > min_tokens:
            return payload
        line["text"] += " value = compute(value)" * 20


async def test_overflow_with_a_diff_too_large_to_shrink_fails_the_chunk(
    service: ReviewService, providers: ScriptedProviders, payload_data: dict[str, Any]
) -> None:
    # Fits 10 000 tokens, but the diff alone exceeds the 7 500 rebuild limit.
    payload = _grow_diff_until(payload_data, 7_600)
    providers.script.append(400)

    result = await service.review_chunk(payload, _budget())

    assert (result.status, result.error_code) == (
        ChunkStatus.FAILED,
        "context_overflow",
    )
    assert result.usage.requests == 1


async def test_repair_that_does_not_fit_the_input_limit_fails_the_chunk(
    service: ReviewService, providers: ScriptedProviders, payload_data: dict[str, Any]
) -> None:
    # ~9 100 tokens: the prompt fits, prompt + invalid answer + errors does not.
    payload = _grow_diff_until(payload_data, 9_100)
    providers.script.append("x" * 4000)
    budget = _budget()

    result = await service.review_chunk(payload, budget)

    assert (result.status, result.error_code) == (
        ChunkStatus.FAILED,
        "invalid_llm_output",
    )
    assert result.usage.requests == 1
    assert budget.try_use_format_repair() is False  # the allowance was consumed


async def test_failure_without_any_answer_reports_the_primary_provider(
    providers: ScriptedProviders, payload: ContextPayload
) -> None:
    settings = _settings().model_copy(
        update={"primary_provider": "ollama", "fallback_enabled": False}
    )
    counter = ConservativeTokenCounter()
    service = ReviewService(
        gateway=build_gateway(
            settings,
            httpx.AsyncClient(transport=httpx.MockTransport(providers.handler)),
            sleep=_no_sleep,
            rng=random.Random(0),
        ),
        prompt_builder=PromptBuilder.load(_PROMPTS, "v1", counter),
        validator=FindingValidator(),
        token_counter=counter,
        settings=settings,
    )
    providers.script.extend([503, 503])

    result = await service.review_chunk(payload, _budget())

    assert (result.status, result.error_code) == (ChunkStatus.FAILED, "llm_unavailable")
    assert (result.provider, result.model) == ("ollama", "qwen")
    assert providers.hosts == ["ollama", "ollama"]


def _built(payload_data: dict[str, Any]) -> BuiltContextPayload:
    envelope = {"schema_version", "review_id", "chunk_id"}
    return BuiltContextPayload(
        schema_version=1,
        review_id=uuid.uuid4(),
        chunk_id=uuid.uuid4(),
        payload_body={k: v for k, v in payload_data.items() if k not in envelope},
    )


async def test_port_adapter_returns_canonical_review_chunk_result(
    service: ReviewService,
    providers: ScriptedProviders,
    payload_data: dict[str, Any],
    canonical_validator: Callable[[str], Draft202012Validator],
) -> None:
    providers.script.append(_output(_finding(39), _finding(38)))
    port: LlmGatewayPort = LlmGatewayAdapter(service, _budget())

    result = await port.review(_built(payload_data))

    wire = dataclasses.asdict(result)
    canonical_validator("review-result/v1.json").validate(wire)
    assert [f["start_line"] for f in wire["findings"]] == [39]
    assert wire["usage"] == {"input_tokens": 1500, "output_tokens": 120}
    assert wire["summary"] == "Сводка"


async def test_port_adapter_omits_usage_without_both_token_counts(
    service: ReviewService,
    providers: ScriptedProviders,
    payload_data: dict[str, Any],
    canonical_validator: Callable[[str], Draft202012Validator],
) -> None:
    providers.script.extend([503, 503, _output()])  # Ollama: no prompt tokens

    result = await LlmGatewayAdapter(service, _budget()).review(_built(payload_data))

    assert result.usage is None
    canonical_validator("review-result/v1.json").validate(dataclasses.asdict(result))


async def test_port_adapter_raises_for_a_failed_chunk(
    service: ReviewService, providers: ScriptedProviders, payload_data: dict[str, Any]
) -> None:
    providers.script.extend([503, 503, 503])

    with pytest.raises(ChunkReviewFailedError) as error:
        await LlmGatewayAdapter(service, _budget()).review(_built(payload_data))

    assert error.value.outcome.error_code == "llm_unavailable"


async def test_port_adapter_refuses_to_spend_one_run_budget_on_another_review(
    service: ReviewService, providers: ScriptedProviders, payload_data: dict[str, Any]
) -> None:
    providers.script.extend([_output(), _output()])
    adapter = LlmGatewayAdapter(service, _budget())
    first = _built(payload_data)
    await adapter.review(first)
    # A handler can process multiple jobs, but this adapter and budget belong to
    # one review. It must reject accidental reuse before issuing HTTP.
    with pytest.raises(ValueError, match="one review run"):
        await adapter.review(dataclasses.replace(first, review_id=uuid.uuid4()))
    assert len(providers.requests) == 1
