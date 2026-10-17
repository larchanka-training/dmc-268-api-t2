"""GitHub-only OAuth code exchange; never uses the worker service token."""

import httpx

from app.application.github_ports import GithubFailure
from app.core.config import Auth


async def exchange_code(client: httpx.AsyncClient, auth: Auth, code: str) -> str:
    async with client.stream(
        "POST",
        "https://github.com/login/oauth/access_token",
        data={
            "client_id": auth.github_client_id,
            "client_secret": auth.github_client_secret.get_secret_value(),
            "redirect_uri": auth.github_redirect_uri,
            "code": code,
        },
        headers={"Accept": "application/json"},
        timeout=15,
        follow_redirects=False,
    ) as response:
        if response.status_code != 200:  # noqa: PLR2004 -- GitHub success
            raise GithubFailure("VCS_UNAVAILABLE", retryable=True)
        content = bytearray()
        async for chunk in response.aiter_bytes():
            if len(content) + len(chunk) > 16384:  # noqa: PLR2004 -- small OAuth JSON
                raise GithubFailure("VCS_UNAVAILABLE", retryable=False)
            content.extend(chunk)
    payload = httpx.Response(200, content=bytes(content)).json()
    if not isinstance(payload, dict):
        raise GithubFailure("VCS_ACCESS_DENIED", retryable=False)
    token = payload.get("access_token")
    token_type = payload.get("token_type")
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 8192  # noqa: PLR2004 -- credential bound
        or not token.isascii()
        or any(char.isspace() or ord(char) < 32 for char in token)  # noqa: PLR2004
        or not isinstance(token_type, str)
        or token_type.lower() != "bearer"
    ):
        raise GithubFailure("VCS_ACCESS_DENIED", retryable=False)
    return token
