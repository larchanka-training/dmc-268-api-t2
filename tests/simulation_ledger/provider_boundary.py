"""Only the external HTTP boundary is replaced; actual simulation service runs."""

import asyncio
from dataclasses import dataclass, field

import httpx

from app.api.rule_simulations import SimulationCodeInput, SimulationRules
from app.llm.openai_compatible import OpenAICompatibleClient
from app.review.simulation import CodeSimulationService


@dataclass
class PaidBoundary:
    calls: list[httpx.Request] = field(default_factory=list)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    finish: asyncio.Event | None = None
    failure: Exception | None = None
    ignore_cancellation: bool = False

    async def send(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        self.entered.set()
        if self.finish is not None:
            while not self.finish.is_set():
                try:
                    await self.finish.wait()
                except asyncio.CancelledError:
                    if not self.ignore_cancellation:
                        raise
        if self.failure:
            raise self.failure
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"findings":[]}'}}]}
        )

    async def execute(self, deadline: float) -> object:
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.send)) as http:
            client = OpenAICompatibleClient(
                name="eurorouter",
                http=http,
                base_url="https://api.eurouter.ai/api/v1",
                model="mistral-small-4",
                api_key="synthetic-key",
            )
            service = CodeSimulationService(
                client, max_input_tokens=8192, max_output_tokens=2000
            )
            return await service.simulate(
                SimulationCodeInput(kind="code", path="example.py", source="print(1)"),
                SimulationRules(
                    format="rules-md/1",
                    markdown="# Security\n## SEC-1: No eval\nAvoid eval.\n",
                ),
                deadline=deadline,
            )
