"""ReviewService.review_chunk end to end over mocked providers (spec §8)."""

import json
import random
from collections import deque
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import LLM, Eurorouter, Ollama
from app.llm.budget import BudgetLimits, RunBudget
from app.llm.providers import build_gateway
from app.llm.tokens import ConservativeTokenCounter
from app.review.prompt import PromptBuilder
from app.review.schemas.context import ContextPayload
from app.review.schemas.result import ChunkStatus, ReviewChunkResult
from app.review.service import ReviewService
from app.review.validation import FindingValidator

_PROMPTS = Path(__file__).resolve().parents[2] / ".agents" / "reviewer"


def _finding(line: int) -> dict[str, Any]:
    return {
        "location": {
            "path": "src/review.py",
            "side": "NEW",
            "line": line,
            "end_line": None,
        },
        "category": "correctness",
        "severity": "high",
        "title": f"Проблема в строке {line}",
        "explanation": "ValidationError роняет обработчик.",
        "evidence": f"Строка {line} вызывает model_validate без обработки ошибок.",
        "recommendation": "Перехватить ValidationError.",
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


async def test_valid_answer_gives_completed_result(
    service: ReviewService, providers: ScriptedProviders, payload: ContextPayload
) -> None:
    providers.script.append(_output(_finding(39), _finding(38)))

    result = await service.review_chunk(payload, _budget())

    assert isinstance(result, ReviewChunkResult)
    assert result.status == ChunkStatus.COMPLETED
    assert [f.location.line for f in result.findings] == [39]
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
    assert [f.location.line for f in result.findings] == [39]
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
