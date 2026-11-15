"""Operator CLI contract; Redis doubles live only at the network boundary."""

import asyncio
import base64
import json
from typing import cast

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from scripts import provision_simulation_admission as cli
from scripts.provision_simulation_admission import run

from app.adapters.simulation_admission import _STATE
from app.core.config import Simulation


def environment() -> dict[str, str]:
    return {
        "SIMULATION__ENABLED": "false",
        "SIMULATION__REDIS_URL": "redis://simulation-redis:6379/0",
        "REDIS__URL": "redis://redis:6379/0",
        "SIMULATION__HMAC_SECRET_B64": base64.b64encode(b"s" * 32).decode(),
    }


async def test_default_is_inspection_without_writes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert await run([], environment()) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "check"


async def test_missing_hmac_refuses(capsys: pytest.CaptureFixture[str]) -> None:
    env = environment()
    del env["SIMULATION__HMAC_SECRET_B64"]
    assert await run(["--check"], env) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_simulation_secret"


@pytest.mark.parametrize(
    "value", ["bad!", "é", " " + base64.b64encode(b"s" * 32).decode()]
)
async def test_invalid_base64_refuses(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    env = environment()
    env["SIMULATION__HMAC_SECRET_B64"] = value
    assert await run(["--check"], env) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_simulation_secret"


@pytest.mark.parametrize("size", [31, 1025])
async def test_secret_size_refuses(
    size: int, capsys: pytest.CaptureFixture[str]
) -> None:
    env = environment()
    env["SIMULATION__HMAC_SECRET_B64"] = base64.b64encode(b"s" * size).decode()
    assert await run(["--check"], env) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_simulation_secret"


async def test_noncanonical_base64_refuses(capsys: pytest.CaptureFixture[str]) -> None:
    env = environment()
    env["SIMULATION__HMAC_SECRET_B64"] = env["SIMULATION__HMAC_SECRET_B64"][:-2] + "N="
    assert await run(["--check"], env) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_simulation_secret"


@pytest.mark.parametrize(
    "url", [None, "redis://redis:6379/0", "rediss://user:pass@REDIS.:6379/9"]
)
async def test_dedicated_endpoint_required(
    url: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    env = environment()
    if url is None:
        del env["SIMULATION__REDIS_URL"]
    else:
        env["SIMULATION__REDIS_URL"] = url
    assert await run(["--check"], env) == 1
    assert (
        json.loads(capsys.readouterr().out)["error"] == "simulation_redis_unavailable"
    )


async def test_create_requires_operator_provenance(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert await run(["--create"], environment()) == 1
    assert (
        json.loads(capsys.readouterr().out)["error"] == "operator_confirmation_required"
    )


CREATE = [
    "--create",
    "--confirm-new-instance-no-history",
    "--confirm-exclusive-dedicated-instance",
]


@pytest.mark.parametrize("enabled", ["true", "", "0", "unknown", None])
async def test_creation_requires_explicitly_disabled(
    enabled: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    env = environment()
    if enabled is None:
        del env["SIMULATION__ENABLED"]
    else:
        env["SIMULATION__ENABLED"] = enabled
    assert await run(CREATE, env) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "simulation_must_be_disabled"


class RedisDouble:
    def __init__(self) -> None:
        self.available = True
        self.structural_result = ["unknown", "invalid"]
        self.other_db = False
        self.command_stats: dict[str, object] = {}
        self.loss_stats: dict[str, object] = {"expired_keys": 0, "evicted_keys": 0}
        self.ttl = -1
        self.set_calls: list[dict[str, object]] = []
        self.closed = False
        self.policy = {
            "appendonly": "yes",
            "appendfsync": "always",
            "no-appendfsync-on-rewrite": "no",
            "maxmemory-policy": "noeviction",
        }
        self.persistence: dict[str, object] = {
            "aof_enabled": 1,
            "aof_last_write_status": "ok",
        }
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, **options: object) -> bool | None:
        self.set_calls.append(options)
        if options.get("nx") and key in self.values:
            return None
        self.values[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def pttl(self, key: str) -> int:
        return self.ttl if key in self.values else -2

    async def dbsize(self) -> int:
        return len(self.values)

    async def eval_ro(self, script: str, numkeys: int, *args: object) -> list[str]:
        assert script.startswith(_STATE)
        assert numkeys == 1
        return self.structural_result

    async def type(self, key: str) -> str:
        return "string" if key in self.values else "none"

    async def exists(self, key: str) -> int:
        return int(key in self.values)

    async def ping(self) -> bool:
        if not self.available:
            raise ConnectionError("synthetic connection credentials")
        return True

    async def config_get(self, *names: str) -> dict[str, str]:
        return {name: self.policy[name] for name in names}

    async def info(self, section: str) -> dict[str, object]:
        if section == "commandstats":
            return self.command_stats
        if section == "stats":
            return self.loss_stats
        return (
            self.persistence
            if section == "persistence"
            else ({"db1": {"keys": 1}} if self.other_db else {})
        )

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def redis_boundary(monkeypatch: pytest.MonkeyPatch) -> RedisDouble:

    client = RedisDouble()
    monkeypatch.setattr(
        cli, "create_admission_client", lambda url: cast(Redis, client), raising=False
    )
    return client


async def test_unavailable_redis_refuses(
    redis_boundary: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    redis_boundary.available = False
    assert await run(["--check"], environment()) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "redis_operation_failed"
    assert redis_boundary.closed
    assert redis_boundary.values == {}


async def test_reports_effective_persistence(
    redis_boundary: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await run(["--check"], environment()) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["persistence"] == {
        "appendonly": "yes",
        "appendfsync": "always",
        "no-appendfsync-on-rewrite": "no",
        "aof_enabled": 1,
        "aof_last_write_status": "ok",
    }


@pytest.mark.parametrize(
    "name,value",
    [
        ("appendonly", "no"),
        ("appendfsync", "everysec"),
        ("no-appendfsync-on-rewrite", "yes"),
        ("aof_enabled", 0),
        ("aof_last_write_status", "err"),
    ],
)
async def test_persistence_misconfiguration_refuses(
    name: str,
    value: str | int,
    redis_boundary: RedisDouble,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if name in redis_boundary.policy:
        redis_boundary.policy[name] = str(value)
    else:
        redis_boundary.persistence[name] = value
    assert await run(CREATE, environment()) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "unsafe_persistence"
    assert redis_boundary.values == {}


async def test_eviction_refuses(
    redis_boundary: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    redis_boundary.policy["maxmemory-policy"] = "allkeys-lru"
    assert await run(CREATE, environment()) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error"] == "unsafe_eviction_policy"
    assert report["eviction_policy"] == "other"
    assert redis_boundary.values == {}


async def test_existing_state_refuses_without_set(
    redis_boundary: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    redis_boundary.values["dmc268:rule-playground:admission"] = "existing-history"
    before = redis_boundary.values.copy()
    assert await run(CREATE, environment()) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "state_already_exists"
    assert redis_boundary.values == before


@pytest.mark.parametrize("other_db", [False, True])
async def test_nonempty_instance_is_not_new(
    other_db: bool, redis_boundary: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    if other_db:
        redis_boundary.other_db = True
    else:
        redis_boundary.values["unrelated"] = "synthetic"
    before = redis_boundary.values.copy()
    assert await run(CREATE, environment()) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "instance_not_empty"
    assert redis_boundary.values == before


async def test_primary_bootstrap_writes_v3_without_ttl(
    redis_boundary: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await run(CREATE, environment()) == 0
    assert json.loads(redis_boundary.values["dmc268:rule-playground:admission"]) == {
        "schema_version": 3,
        "lease": None,
        "admissions": [],
        "consumed_keys": [],
        "hmac_config_id": (
            "ebb7cfe0b2ac5246f56853b4a795ab89733c59a2647026b0f2dabee75b8b6413"
        ),
    }
    assert json.loads(capsys.readouterr().out)["status"] == "created"
    assert redis_boundary.ttl == -1


async def test_concurrent_bootstrap_has_one_winner(
    redis_boundary: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    original = redis_boundary.set
    gate = asyncio.Event()
    arrivals = 0

    async def simultaneous(key: str, value: str, **options: object) -> bool | None:
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:  # noqa: PLR2004 -- two contenders
            gate.set()
        await gate.wait()
        return await original(key, value, **options)

    redis_boundary.set = simultaneous  # type: ignore[method-assign]
    results = await asyncio.gather(
        run(CREATE, environment()), run(CREATE, environment())
    )
    assert sorted(results) == [0, 1]
    assert all(options == {"nx": True} for options in redis_boundary.set_calls)
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert sum(report.get("status") == "created" for report in reports) == 1


async def test_verification_failure_does_not_acknowledge_creation(
    redis_boundary: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    redis_boundary.ttl = 60000
    assert await run(CREATE, environment()) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "creation_unconfirmed"
    assert "dmc268:rule-playground:admission" in redis_boundary.values


async def test_inspection_reports_type_and_ttl_without_mutation(
    redis_boundary: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    redis_boundary.values["dmc268:rule-playground:admission"] = "synthetic"
    redis_boundary.ttl = 60000
    before = redis_boundary.values.copy()
    assert await run(["--check"], environment()) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["state_exists"] is True
    assert report["state_type"] == "string"
    assert report["pttl_ms"] == 60000  # noqa: PLR2004 -- synthetic physical TTL
    assert redis_boundary.values == before
    assert not redis_boundary.set_calls


async def test_inspection_uses_adapter_structure_contract(
    redis_boundary: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    redis_boundary.values["dmc268:rule-playground:admission"] = "{broken"
    redis_boundary.structural_result = ["unknown", "invalid"]
    before = redis_boundary.values.copy()
    assert await run(["--check"], environment()) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["structure"] == "invalid"
    assert report["hmac_identity"] == "unknown"
    assert redis_boundary.values == before
    assert not redis_boundary.set_calls


async def test_valid_stale_snapshot_is_not_activation_or_new_bootstrap(
    redis_boundary: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    redis_boundary.values["dmc268:rule-playground:admission"] = (
        "synthetic-valid-old-snapshot"
    )
    redis_boundary.structural_result = ["match", "valid"]
    before = redis_boundary.values.copy()
    assert await run(["--check"], environment()) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["structure"] == "valid"
    assert report["activation_authorized"] is False
    assert report["history_continuity"] == "unproven"
    assert await run(CREATE, environment()) == 1
    assert redis_boundary.values == before
    assert not redis_boundary.set_calls


@pytest.mark.parametrize("committed", [False, True])
async def test_write_error_is_unconfirmed_without_retry_or_leak(
    committed: bool, redis_boundary: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    async def failure(key: str, value: str, **options: object) -> bool | None:
        redis_boundary.set_calls.append(options)
        if committed:
            redis_boundary.values[key] = value
        raise ConnectionError(environment()["SIMULATION__HMAC_SECRET_B64"])

    redis_boundary.set = failure  # type: ignore[method-assign]
    assert await run(CREATE, environment()) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["error"] == "creation_unconfirmed"
    assert (
        environment()["SIMULATION__HMAC_SECRET_B64"] not in captured.out + captured.err
    )
    assert len(redis_boundary.set_calls) == 1
    assert bool(redis_boundary.values) == committed


async def test_cleanup_failure_is_safe_and_never_reports_success(
    redis_boundary: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    async def failure() -> None:
        raise RuntimeError(environment()["SIMULATION__HMAC_SECRET_B64"])

    redis_boundary.aclose = failure  # type: ignore[method-assign]
    assert await run(CREATE, environment()) == 1
    captured = capsys.readouterr()
    assert (
        environment()["SIMULATION__HMAC_SECRET_B64"] not in captured.out + captured.err
    )
    reports = [json.loads(line) for line in captured.out.splitlines()]
    assert all(report.get("status") != "created" for report in reports)


async def test_success_reports_verified_state(
    redis_boundary: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await run(CREATE, environment()) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["state_exists"] is True
    assert report["state_type"] == "string"
    assert report["pttl_ms"] == -1
    assert report["structure"] == "valid"
    assert report["hmac_identity"] == "match"


async def test_known_shared_endpoint_refuses_even_with_other_review_setting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = environment()
    env["REDIS__URL"] = "redis://other-review-host:6379/0"
    env["SIMULATION__REDIS_URL"] = "redis://redis:6379/0"
    assert await run(CREATE, env) == 1
    assert (
        json.loads(capsys.readouterr().out)["error"] == "simulation_redis_unavailable"
    )


async def test_check_still_inspects_state_when_persistence_is_unsafe(
    redis_boundary: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    redis_boundary.policy["appendfsync"] = "everysec"
    redis_boundary.values["dmc268:rule-playground:admission"] = "synthetic"
    assert await run(["--check"], environment()) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error"] == "unsafe_persistence"
    assert report["state_exists"] is True
    assert report["structure"] == "invalid"
    assert not redis_boundary.set_calls


@pytest.mark.parametrize("counter", ["expired_keys", "evicted_keys"])
async def test_prior_key_loss_refuses_even_when_database_is_empty(
    counter: str, redis_boundary: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    redis_boundary.loss_stats[counter] = 1
    assert await run(CREATE, environment()) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "prior_key_loss_detected"
    assert not redis_boundary.set_calls


@pytest.mark.parametrize("size", [32, 1024])
async def test_secret_boundary_sizes_are_accepted_characterization(
    size: int, capsys: pytest.CaptureFixture[str]
) -> None:
    env = environment()
    env["SIMULATION__HMAC_SECRET_B64"] = base64.b64encode(b"s" * size).decode()
    assert await run(["--check"], env) == 0
    assert "error" not in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize(
    "args",
    [
        ["--hmac-secret", "SENSITIVE"],
        ["--bogus=SENSITIVE"],
        ["--create=SENSITIVE"],
        ["--cre"],
        ["--check", "--create"],
    ],
)
async def test_arguments_are_redacted_characterization(
    args: list[str],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert await run(args, environment()) == 1
    captured = capsys.readouterr()
    assert "SENSITIVE" not in captured.out + captured.err + caplog.text
    assert json.loads(captured.out)["error"] == "invalid_arguments"


async def test_repeat_bootstrap_never_sets_again_characterization(
    redis_boundary: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await run(CREATE, environment()) == 0
    before = redis_boundary.values.copy()
    assert await run(CREATE, environment()) == 1
    assert redis_boundary.values == before
    assert len(redis_boundary.set_calls) == 1
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert reports[-1]["error"] == "state_already_exists"


@pytest.mark.parametrize(
    "command",
    ["set", "del", "flushdb", "restore", "eval", "fcall", "unknown-module-write"],
)
async def test_prior_mutation_or_unknown_command_refuses_empty_instance(
    command: str, redis_boundary: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    redis_boundary.command_stats[f"cmdstat_{command}"] = {"calls": 1}
    assert await run(CREATE, environment()) == 1
    assert (
        json.loads(capsys.readouterr().out)["error"] == "prior_use_or_unknown_command"
    )
    assert not redis_boundary.set_calls


@pytest.mark.parametrize(
    "flags", [[], ["--check"], CREATE[1:], ["--check", *CREATE[1:]]]
)
async def test_no_create_means_no_set_characterization(
    flags: list[str], redis_boundary: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await run(flags, environment()) == 0
    assert redis_boundary.values == {}
    assert not redis_boundary.set_calls
    assert json.loads(capsys.readouterr().out)["mode"] == "check"


@pytest.mark.parametrize("omitted", CREATE[1:])
async def test_each_attestation_is_required_characterization(
    omitted: str, redis_boundary: RedisDouble, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await run([flag for flag in CREATE if flag != omitted], environment()) == 1
    assert (
        json.loads(capsys.readouterr().out)["error"] == "operator_confirmation_required"
    )
    assert not redis_boundary.set_calls


async def test_initialization_and_settings_repr_are_redacted_characterization(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = environment()
    env["SIMULATION__REDIS_URL"] = (
        "redis://synthetic-user:private-credential@simulation-redis:6379/0"
    )
    encoded = env["SIMULATION__HMAC_SECRET_B64"]

    def failure(url: str) -> Redis:
        raise ValueError(url + encoded)

    monkeypatch.setattr(cli, "create_admission_client", failure)
    assert await run(CREATE, env) == 1
    captured = capsys.readouterr()
    rendered = repr(
        Simulation(
            hmac_secret_b64=SecretStr(encoded),
            redis_url=SecretStr(env["SIMULATION__REDIS_URL"]),
        )
    )
    output = captured.out + captured.err + caplog.text + rendered
    assert encoded not in output
    assert "private-credential" not in output
    assert json.loads(captured.out)["error"] == "redis_operation_failed"
