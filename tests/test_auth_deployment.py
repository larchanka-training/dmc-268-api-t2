"""Browser auth settings reach only the staging API container."""

import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]

_API_AUTH = {
    "AUTH__GITHUB_CLIENT_ID": "${AUTH__GITHUB_CLIENT_ID:-}",
    "AUTH__GITHUB_CLIENT_SECRET": "${AUTH__GITHUB_CLIENT_SECRET:-}",
    "AUTH__GITHUB_REDIRECT_URI": "${AUTH__GITHUB_REDIRECT_URI:-}",
    "AUTH__JWT_SECRET": "${AUTH__JWT_SECRET:-}",
    "AUTH__CREDENTIAL_KEY": "${AUTH__CREDENTIAL_KEY:-}",
    "AUTH__FRONTEND_ORIGINS": "${AUTH__FRONTEND_ORIGINS:-}",
}


def test_deploy_forwards_browser_auth_only_to_api() -> None:
    services = yaml.safe_load((ROOT / "docker-compose.staging.yml").read_text())[
        "services"
    ]
    assert services["api"]["environment"] | _API_AUTH == services["api"]["environment"]
    for name, service in services.items():
        if name != "api":
            for variable in _API_AUTH:
                assert variable not in service.get("environment", {})

    example = (ROOT / ".env.staging.example").read_text(encoding="utf-8")
    for variable in _API_AUTH:
        assert variable in example

    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/deploy-staging.yml").read_text()
    )
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    runtime = next(
        step for step in steps if step.get("name") == "Write runtime environment"
    )
    assert (
        runtime["env"]["AUTH_GITHUB_CLIENT_ID"] == "${{ vars.AUTH__GITHUB_CLIENT_ID }}"
    )
    assert (
        runtime["env"]["AUTH_GITHUB_CLIENT_SECRET"]
        == "${{ secrets.AUTH__GITHUB_CLIENT_SECRET }}"
    )
    assert (
        runtime["env"]["AUTH_GITHUB_REDIRECT_URI"]
        == "${{ vars.AUTH__GITHUB_REDIRECT_URI }}"
    )
    assert runtime["env"]["AUTH_JWT_SECRET"] == "${{ secrets.AUTH__JWT_SECRET }}"
    assert (
        runtime["env"]["AUTH_CREDENTIAL_KEY"] == "${{ secrets.AUTH__CREDENTIAL_KEY }}"
    )
    assert (
        runtime["env"]["AUTH_FRONTEND_ORIGINS"] == "${{ vars.AUTH__FRONTEND_ORIGINS }}"
    )
    writer = runtime["run"].split("} | sshpass -e ssh", 1)[0] + "}\n"
    bash = "bash"
    git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if git_bash.is_file():
        bash = str(git_bash)
    result = subprocess.run(
        [bash, "-euc", writer.replace("\r\n", "\n")],
        env={
            "PATH": os.environ["PATH"],
            "API_IMAGE": "api:test",
            "UI_IMAGE": "ui:test",
            "WEB_PORT": "80",
            "POSTGRES_PASSWORD": "test",
            "LLM_PRIMARY_PROVIDER": "eurorouter",
            "LLM_FALLBACK_ENABLED": "false",
            "EUROROUTER_BASE_URL": "https://llm.example/v1",
            "EUROROUTER_MODEL": "test",
            "EUROROUTER_API_KEYS": "",
            "OLLAMA_BASE_URL": "http://ollama:11434",
            "OLLAMA_MODEL": "",
            "AUTH_GITHUB_CLIENT_ID": "Iv1.synthetic",
            "AUTH_GITHUB_CLIENT_SECRET": "secret-synthetic",
            "AUTH_GITHUB_REDIRECT_URI": (
                "https://dmc268-t2.ddns.net/api/v1/auth/github/callback"
            ),
            "AUTH_JWT_SECRET": "jwt-synthetic-secret-32-bytes!!",
            "AUTH_CREDENTIAL_KEY": "dGVzdC1lbmNyeXB0aW9uLWtleS0zMi1ieXRlcyEhISE=",
            "AUTH_FRONTEND_ORIGINS": '["https://dmc268-t2.ddns.net"]',
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    entries = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert entries["AUTH__GITHUB_CLIENT_ID"] == '"Iv1.synthetic"'
    assert entries["AUTH__GITHUB_CLIENT_SECRET"] == '"secret-synthetic"'
    assert entries["AUTH__GITHUB_REDIRECT_URI"] == (
        '"https://dmc268-t2.ddns.net/api/v1/auth/github/callback"'
    )
    assert entries["AUTH__JWT_SECRET"] == '"jwt-synthetic-secret-32-bytes!!"'
    assert entries["AUTH__CREDENTIAL_KEY"] == (
        '"dGVzdC1lbmNyeXB0aW9uLWtleS0zMi1ieXRlcyEhISE="'
    )
    assert entries["AUTH__FRONTEND_ORIGINS"] == ('"[\\"https://dmc268-t2.ddns.net\\"]"')
