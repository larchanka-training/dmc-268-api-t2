"""Repository automation/publication policy, separate from model budgets."""

import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_BRANCH_NAME_BYTES = 1024

PUBLIC_POLICY_FIELDS = frozenset(
    {"auto_review_enabled", "base_branches", "minimum_severity"}
)


class ReviewPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    auto_review_enabled: bool = False
    base_branches: list[str] = Field(default_factory=list, max_length=128)
    minimum_severity: Literal["critical", "high", "medium", "low"] = "low"

    @field_validator("base_branches")
    @classmethod
    def exact_branch_names(cls, branches: list[str]) -> list[str]:
        if any(
            len(branch.encode("utf-8")) > MAX_BRANCH_NAME_BYTES for branch in branches
        ):
            raise ValueError("Branch name exceeds the input bound")
        if any(
            any(
                ord(char) <= ord(" ") or ord(char) == ord("\x7f") or char in "~^:?*[\\"
                for char in branch
            )
            for branch in branches
        ):
            raise ValueError("Expected exact Git branch names")
        if any(
            not branch
            or branch == "@"
            or branch.startswith(("/", "-"))
            or branch.endswith(("/", "."))
            or "//" in branch
            or ".." in branch
            or "@{" in branch
            or any(
                part.startswith(".") or part.endswith(".lock")
                for part in branch.split("/")
            )
            for branch in branches
        ):
            raise ValueError("Expected valid Git branch names")
        return branches


class StoredReviewPolicy(ReviewPreferences):
    schema_version: Literal[1] = 1
    authorized_by: UUID | None = None


def read_review_policy(value: dict[str, Any] | None) -> StoredReviewPolicy:
    return (
        StoredReviewPolicy.model_validate_json(json.dumps(value))
        if value is not None
        else StoredReviewPolicy()
    )


def public_preferences(value: dict[str, Any] | None) -> dict[str, Any]:
    return read_review_policy(value).model_dump(
        mode="json", include=set(PUBLIC_POLICY_FIELDS)
    )


class AutomaticReviewIntent(BaseModel):
    """Server-selected NEW-delivery authority; never derived from the sender."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1
    repository_id: UUID
    source_settings_id: UUID
    source_settings_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor_user_id: UUID
    policy: StoredReviewPolicy
