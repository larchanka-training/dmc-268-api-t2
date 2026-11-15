"""Read-only staging activation gate. Never constructs the simulation runtime."""

import asyncio
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.adapters.simulation_admission import create_admission_client
from app.core.config import Settings
from app.core.simulation_hmac import decode_simulation_hmac, simulation_hmac_config_id
from app.review.simulation_configuration import prepare_simulation_configuration
from scripts import provision_simulation_admission as provisioning


async def _check_ledger(settings: Settings, config_id: str) -> None:
    engine = create_async_engine(
        settings.postgres.url,
        isolation_level="REPEATABLE READ",
        hide_parameters=True,
        connect_args={"timeout": 5, "command_timeout": 5},
    )
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            versions = await connection.scalars(
                text("SELECT version_num FROM alembic_version")
            )
            if versions.all() != ["0022"]:
                raise ValueError("migration_0022_required")
            control = (
                await connection.execute(
                    text(
                        "SELECT blocked, hmac_config_id "
                        "FROM simulation_dispatch_control WHERE id=1"
                    )
                )
            ).one_or_none()
            if control is None or control.blocked is not False:
                raise ValueError("ledger_blocked_or_missing")
            if control.hmac_config_id is None:
                if await connection.scalar(
                    text("SELECT EXISTS(SELECT 1 FROM simulation_dispatch_claim)")
                ):
                    raise ValueError("unbound_ledger_has_claims")
            elif control.hmac_config_id != config_id:
                raise ValueError("ledger_hmac_mismatch")
    finally:
        await engine.dispose()


async def run(environ: Mapping[str, str]) -> int:
    ready = (
        environ.get("SIMULATION__ENABLED") == "true"
        and environ.get("DEPLOY_ENV") == "staging"
        and environ.get("SIMULATION_ACTIVATION_APPROVED") == "true"
    )
    if ready:
        try:
            secret = decode_simulation_hmac(environ.get("SIMULATION__HMAC_SECRET_B64"))
            if (
                environ.get("SIMULATION__REDIS_URL")
                != "redis://simulation-redis:6379/0"
            ):
                raise ValueError("unexpected_dedicated_endpoint")
            # Read only the container environment, never an incidental dotenv file.
            settings = Settings(_env_file=None)  # type: ignore[call-arg]
            manifest = settings.simulation.manifest_path
            expected_image = environ.get("SIMULATION_EXPECTED_IMAGE_SHA", "")
            if (
                not re.fullmatch(r"[0-9a-f]{40}", expected_image)
                or environ.get("GIT_SHA") != expected_image
                or manifest is None
                or hashlib.sha256(manifest.read_bytes()).hexdigest()
                != environ.get("SIMULATION_MANIFEST_SHA256")
            ):
                raise ValueError("selected_image_manifest_mismatch")
            if (
                prepare_simulation_configuration(settings, manifest_path=manifest)
                is None
            ):
                raise ValueError("configuration_unavailable")
            code, report = await provisioning._operate(["--check"], environ)
            ready = (
                code == 0
                and report.get("state_exists") is True
                and report.get("state_type") == "string"
                and report.get("structure") == "valid"
                and report.get("hmac_identity") == "match"
                and report.get("pttl_ms") == -1
            )
            if ready:
                client = create_admission_client(
                    settings.simulation.dedicated_redis_url(settings.redis.url)
                )
                try:
                    stats = await client.info("stats")
                    if any(
                        stats.get(name) != 0
                        for name in ("expired_keys", "evicted_keys")
                    ):
                        raise ValueError("redis_history_loss_detected")
                finally:
                    await client.aclose()
                await _check_ledger(settings, simulation_hmac_config_id(secret))
        except Exception:
            ready = False
    print(json.dumps({"ready": ready, "history_continuity": "unproven"}))
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run(os.environ)))
