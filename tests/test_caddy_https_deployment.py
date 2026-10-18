"""Caddy is one host container in front of the existing HTTP ports."""

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_caddy_https_keeps_port_80_and_serves_both_names() -> None:
    caddyfile = (ROOT / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    assert "auto_https disable_redirects" in caddyfile
    main_site, develop_site = caddyfile.split(
        "https://dmc268t2-staging.ddns.net", maxsplit=1
    )
    assert "disable_http_challenge" in main_site
    assert "disable_http_challenge" in develop_site
    assert "reverse_proxy 127.0.0.1:80\n" in main_site
    assert "reverse_proxy 127.0.0.1:8080" not in main_site
    assert "reverse_proxy 127.0.0.1:8080\n" in develop_site
    assert "https://dmc268-t2.ddns.net" in caddyfile

    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "deploy-staging.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    names = [step.get("name") for step in steps]
    caddy = next(step for step in steps if step.get("name") == "Ensure Caddy HTTPS")
    assert names.index("Ensure Caddy HTTPS") == names.index("Deploy application") + 1
    assert caddy["env"]["SSH_USER"] == "${{ secrets.VPS_DMC268_U }}"
    assert caddy["env"]["SSHPASS"] == "${{ secrets.VPS_DMC268_P }}"
    run = caddy["run"]
    assert "mkdir -p /opt/caddy" in run
    assert "caddy/Caddyfile" in run
    assert "/opt/caddy/Caddyfile" in run
    assert "--network host" in run
    assert "--restart unless-stopped" in run
    assert "-v caddy-data:/data" in run
    assert "-v caddy-config:/config" in run
    assert "-v /opt/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" in run
    assert "caddy:2" in run
    assert "-p 443:443" not in run
    assert "docker rm" not in run
    assert (
        "docker exec caddy caddy reload --config /etc/caddy/Caddyfile "
        "--adapter caddyfile"
    ) in run

    compose = yaml.safe_load(
        (ROOT / "docker-compose.staging.yml").read_text(encoding="utf-8")
    )
    assert "caddy" not in compose["services"]

    doc = (ROOT / "docs" / "staging-deployment.md").read_text(encoding="utf-8")
    assert "DNS и TLS/HTTPS пока не настроены" not in doc
    assert "HTTPS на `443`" not in doc
    assert "https://dmc268-t2.ddns.net/" in doc
    assert "https://dmc268t2-staging.ddns.net/" in doc
    assert "http://213.199.63.63/" in doc
