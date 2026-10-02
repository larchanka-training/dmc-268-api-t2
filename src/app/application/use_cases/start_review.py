from __future__ import annotations

import hashlib
import json
import uuid

from app.application.ports import StartReviewCommand, UnitOfWorkPort


def compute_config_digest(command: StartReviewCommand) -> str:
    """Return the deterministic identity of the frozen analysis configuration."""

    payload = {
        "model_digest": command.model_digest,
        "model_ref": command.model_ref,
        "model_settings": command.model_settings,
        "prompt_digest": command.prompt_digest,
        "prompt_version": command.prompt_version,
        "repository_settings_id": str(command.repository_settings_id),
        "rules_digest": command.rules_digest,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def start_review(
    command: StartReviewCommand, *, unit_of_work: UnitOfWorkPort
) -> uuid.UUID:
    """Persist the initial review aggregate through one transaction boundary."""

    config_digest = compute_config_digest(command)
    async with unit_of_work:
        review_id = await unit_of_work.review_jobs.add_start_review(
            command,
            config_digest=config_digest,
        )
        await unit_of_work.commit()
    return review_id


__all__ = ["StartReviewCommand", "compute_config_digest", "start_review"]
