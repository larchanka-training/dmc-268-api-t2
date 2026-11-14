"""Dedicated Redis must remain opt-in through the real Compose contract."""

import json
import os
import subprocess

import pytest
from test_analysis_deployment import ROOT, compose_config


@pytest.mark.parametrize(
    "profiles", ["", "analysis", "publication", "analysis,publication"]
)
def test_simulation_redis_is_available_only_with_its_explicit_profile(
    profiles: str,
) -> None:
    ordinary = compose_config(profiles=profiles)["services"]
    assert "simulation-redis" not in ordinary
    assert "simulation-redis" not in ordinary["api"]["depends_on"]
    opted_in = compose_config(profiles="simulation")["services"]
    assert "simulation-redis" in opted_in
    dedicated = opted_in["simulation-redis"]
    assert dedicated["profiles"] == ["simulation"]
    assert dedicated["image"] == "redis:8-alpine"
    assert set(dedicated["networks"]) == {"backend"}
    assert not dedicated.get("ports")
    assert dedicated["healthcheck"]["test"] == ["CMD", "redis-cli", "ping"]


def test_simulation_redis_requires_its_own_prepared_named_data_volume() -> None:
    config = compose_config(profiles="simulation")
    dedicated = config["services"]["simulation-redis"]
    assert dedicated.get("volumes") == [
        {
            "type": "volume",
            "source": "simulation-redis-data",
            "target": "/data",
            "volume": {},
        }
    ]
    volume = config["volumes"]["simulation-redis-data"]
    assert volume["external"] is True
    assert volume["name"] == "dmc-268-t2-staging_simulation-redis-data"
    assert "simulation-redis-data" not in compose_config()["volumes"]


def test_only_api_receives_dedicated_url_without_any_shared_default() -> None:

    base = {
        "PATH": os.environ["PATH"],
        "API_IMAGE": "api:synthetic",
        "UI_IMAGE": "ui:synthetic",
        "POSTGRES__USER": "test",
        "POSTGRES__PASSWORD": "synthetic",
        "POSTGRES__DB": "test",
        "COMPOSE_PROFILES": "analysis,publication,simulation",
    }
    for value in [None, "redis://simulation-redis:6379/0"]:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(ROOT / "docker-compose.staging.yml"),
                "config",
                "--format",
                "json",
            ],
            env=base | ({"SIMULATION__REDIS_URL": value} if value is not None else {}),
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        services = json.loads(result.stdout)["services"]
        assert services["api"]["environment"].get("SIMULATION__REDIS_URL") == (
            value or ""
        )
        for name, service in services.items():
            if name != "api":
                assert "SIMULATION__REDIS_URL" not in service.get("environment", {})
