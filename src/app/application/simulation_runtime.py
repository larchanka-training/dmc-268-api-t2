"""Application-owned Playground composition; constructed only inside lifespan."""

import asyncio
import base64

import httpx
from starlette.responses import JSONResponse

from app.adapters.simulation_admission import RedisSimulationAdmission
from app.application.simulation_execution import SimulationExecutionCoordinator
from app.core.config import Settings
from app.llm.openai_compatible import OpenAICompatibleClient
from app.review.simulation import CodeSimulationService
from app.review.simulation_configuration import (
    SimulationConfiguration,
    prepare_simulation_configuration,
)

_MIN_SECRET_BYTES = 32
_MAX_SECRET_BYTES = 1024


class SimulationRuntime:
    """An unavailable runtime owns no provider or admission resources."""

    def __init__(self, settings: Settings) -> None:
        self.closed = False
        self.cleanup_task: asyncio.Task[None] | None = None
        self.accepting = False
        self.http: httpx.AsyncClient | None = None
        self.admission: RedisSimulationAdmission | None = None
        self.configuration: SimulationConfiguration | None = None
        if (
            settings.simulation.enabled
            and settings.simulation.hmac_secret_b64 is not None
            and settings.simulation.redis_url is not None
        ):
            try:
                settings.simulation.dedicated_redis_url(settings.redis.url)
                encoded = settings.simulation.hmac_secret_b64.get_secret_value()
                secret = base64.b64decode(encoded, validate=True)
                if (
                    not _MIN_SECRET_BYTES <= len(secret) <= _MAX_SECRET_BYTES
                    or base64.b64encode(secret).decode() != encoded
                ):
                    raise ValueError("invalid_simulation_secret")
                self._hmac_secret = secret
                self.configuration = prepare_simulation_configuration(
                    settings, manifest_path=settings.simulation.manifest_path
                )
            except ValueError:
                pass
        self.executor: CodeSimulationService | None = None
        self.coordinator: SimulationExecutionCoordinator[JSONResponse] | None = None

    async def start(self, settings: Settings) -> None:
        """Compose one direct service and a dedicated admission pool, without I/O."""
        config = self.configuration
        if config is None:
            return
        try:
            self.http = httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                transport=httpx.AsyncHTTPTransport(retries=0, trust_env=False),
            )
            self.admission = RedisSimulationAdmission(
                settings.simulation.dedicated_redis_url(settings.redis.url),
                hmac_secret=self._hmac_secret,
            )
            client = OpenAICompatibleClient(
                name=config.provider,
                http=self.http,
                base_url=config.base_url,
                model=config.model,
                api_key=config.api_key.get_secret_value(),
            )
            executor = CodeSimulationService(
                client,
                max_input_tokens=config.max_input_tokens,
                max_output_tokens=config.max_output_tokens,
            )
            coordinator: SimulationExecutionCoordinator[JSONResponse] = (
                SimulationExecutionCoordinator(
                    self.admission, timeout_s=config.execution_timeout_s
                )
            )
        except Exception:
            # No provider diagnostics, configuration values or traceback logging.
            self.configuration = None
            await self.aclose()
            return
        self.executor = executor
        self.coordinator = coordinator
        self.accepting = True

    async def aclose(self) -> bool:
        """Bound shutdown; False retains ownership in one observed cleanup task.

        Caller cancellation never cancels that task. Completion still requires a
        live event loop and locally terminating operations; remote inference
        termination cannot be inferred from local cancellation.
        """
        self.accepting = False
        if self.cleanup_task is None:
            self.cleanup_task = asyncio.create_task(self._close_when_idle())
            self.cleanup_task.add_done_callback(self._observe_cleanup)
        await asyncio.wait({self.cleanup_task}, timeout=2.0)
        return self.closed

    @staticmethod
    def _observe_cleanup(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def _close_when_idle(self) -> None:
        while self.coordinator is not None and not await self.coordinator.aclose():
            await asyncio.sleep(0.05)
        try:
            if self.http is not None:
                await self.http.aclose()
        finally:
            if self.admission is not None:
                await self.admission.aclose()
        self.closed = True
