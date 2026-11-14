"""Owned subprocess harness; synthetic credentials and MockTransport only."""

import asyncio
import json
import os
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.adapters.simulation_admission import RedisSimulationAdmission
from app.adapters.simulation_dispatch_ledger import PostgresSimulationDispatchLedger
from app.application.simulation_execution import SimulationExecutionCoordinator

SECRET = b"test-only-secret-material-32bytes!"


async def main() -> None:
    options = json.loads(os.environ["B2C2_PROCESS_OPTIONS"])
    engine = create_async_engine(
        options["url"],
        connect_args={"server_settings": {"application_name": options["name"]}},
    )
    ledger = PostgresSimulationDispatchLedger(
        async_sessionmaker(engine), hmac_secret=SECRET
    )
    scope = (
        UUID(options["user"]),
        UUID(options["repository"]),
        options["key"].encode(),
    )
    try:
        if options["mode"] == "claim":
            print(await ledger.claim(*scope), flush=True)
            return
        admission = RedisSimulationAdmission(
            options["redis"], state_key=options["state_key"], hmac_secret=SECRET
        )
        coordinator = SimulationExecutionCoordinator[object](admission, ledger=ledger)

        async def send(request: httpx.Request) -> httpx.Response:
            print("MOCK_HTTP_ACCEPTED", flush=True)
            os._exit(17)

        async def execute(_: float) -> object:
            if options["mode"] == "crash_after_claim":
                print("CLAIM_ACK_BEFORE_HTTP", flush=True)
                os._exit(17)
            async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
                return await http.post(
                    "https://api.eurouter.ai/api/v1/chat/completions",
                    json={"synthetic": True},
                )

        await coordinator.execute(*scope, execute)
        raise AssertionError("crash barrier not reached")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
