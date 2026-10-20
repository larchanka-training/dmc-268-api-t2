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


def test_caddy_deploy_validates_before_replace_and_waits_until_stable() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "deploy-staging.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    run = next(step for step in steps if step.get("name") == "Ensure Caddy HTTPS")[
        "run"
    ]
    validate = "caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile"
    replace = (
        "cat /opt/caddy/Caddyfile.new > /opt/caddy/Caddyfile"
        " && rm -f /opt/caddy/Caddyfile.new"
    )
    assert ":/opt/caddy/Caddyfile.new" in run
    assert "rm -f /opt/caddy/Caddyfile.new" in run
    assert run.index(validate) < run.index(replace)
    assert run.index(replace) < run.index("docker run -d")
    after_create = run.split("docker run -d", maxsplit=1)[1]
    assert after_create.index("reload_caddy") < after_create.index("exit 0")
    after_exec = run.split(
        "docker exec caddy caddy reload --config /etc/caddy/Caddyfile "
        "--adapter caddyfile",
        maxsplit=1,
    )[1]
    stable = "{{.State.Running}} {{.State.Restarting}} {{.RestartCount}}"
    assert after_exec.index("{{.RestartCount}}") < after_exec.index("sleep 5")
    assert after_exec.index("sleep 5") < after_exec.index(stable)
    assert after_exec.index("true false") < after_exec.index("return 0")
    assert after_exec.index(stable) < after_exec.index("return 0")
    assert "docker logs --tail=50 caddy" in after_exec
    collision = after_create.split("exit 0", maxsplit=1)[1]
    assert collision.index("docker start") < collision.index("reload_caddy")

    ci = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "api-ci.yml").read_text(encoding="utf-8")
    )
    caddy_job = ci["jobs"]["caddy"]
    assert "needs" not in caddy_job
    ci_run = "\n".join(step.get("run", "") or "" for step in caddy_job.get("steps", []))
    assert "caddy/Caddyfile:/etc/caddy/Caddyfile:ro" in ci_run
    assert validate in ci_run
