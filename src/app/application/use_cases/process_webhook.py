"""Capture Context Level 1 without starting an authorized review."""

from uuid import UUID

from app.application.github_ports import GithubFailure, GithubPort
from app.application.webhook_ports import WebhookInbox
from app.vcs.diff import InvalidDiff, parse_diff


async def process_webhook(
    receipt_id: UUID, inbox: WebhookInbox, github: GithubPort
) -> None:
    claim = await inbox.claim(receipt_id)
    if claim is None:
        return
    try:
        captured = await github.fetch(claim.request)
    except GithubFailure as failure:
        await inbox.fail(claim, failure)
        return
    try:
        parsed = parse_diff(captured.raw_diff, ignore_globs=claim.ignore_globs)
    except InvalidDiff:
        await inbox.fail(claim, GithubFailure("DIFF_UNTRUSTWORTHY", retryable=False))
        return
    snapshot = {
        "schema_version": "1.0",
        "context_level": 1,
        **captured.metadata,
        "files": parsed.files,
        "coverage": parsed.coverage,
    }
    await inbox.complete(claim, snapshot, captured.raw_diff)
