"""Validate and project only the persisted diff, never the prompt context."""

from collections.abc import Sequence
from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from app.api.review_dto import Projection, RepositoryPath
from app.db.models import ContextPayload
from app.domain.contract_validation import validate_hunks

Coordinate = Annotated[int, Field(ge=0)]
LineNumber = Annotated[int, Field(ge=1)]


class DiffLine(Projection):
    kind: Literal["added", "deleted", "context"]
    content: str = Field(validation_alias="text")
    old_line: LineNumber | None
    new_line: LineNumber | None

    @model_validator(mode="after")
    def check_sides(self) -> Self:
        sides = (self.old_line is not None, self.new_line is not None)
        if (
            sides
            != {
                "added": (False, True),
                "deleted": (True, False),
                "context": (True, True),
            }[self.kind]
        ):
            raise ValueError("Inconsistent line sides")
        return self


class DiffHunk(Projection):
    old_start: Coordinate
    old_lines: Coordinate = Field(validation_alias="old_count")
    new_start: Coordinate
    new_lines: Coordinate = Field(validation_alias="new_count")
    lines: list[DiffLine]

    @model_validator(mode="after")
    def check_coordinates(self) -> Self:
        if (self.old_lines and not self.old_start) or (
            self.new_lines and not self.new_start
        ):
            raise ValueError("Invalid hunk start")
        validate_hunks([self.model_dump()], public=True)
        return self


class StoredFile(Projection):
    old_path: RepositoryPath | None
    new_path: RepositoryPath | None
    status: Literal["added", "modified", "deleted", "renamed"]
    hunks: list[DiffHunk]

    @model_validator(mode="after")
    def check_paths(self) -> Self:
        old, new = self.old_path, self.new_path
        valid = {
            "added": old is None and new is not None,
            "deleted": old is not None and new is None,
            "modified": old is not None and old == new,
            "renamed": old is not None and new is not None and old != new,
        }[self.status]
        if not valid:
            raise ValueError("Inconsistent file paths")
        if any(
            (self.status == "added" and hunk.old_lines)
            or (self.status == "deleted" and hunk.new_lines)
            for hunk in self.hunks
        ):
            raise ValueError("Inconsistent file sides")
        return self


class StoredFileContext(Projection):
    diff: StoredFile


class StoredContext(Projection):
    files: list[StoredFileContext]


def project_diff(contexts: Sequence[ContextPayload]) -> dict[str, Any]:
    merged: dict[str, dict[str, Any]] = {}
    for context in contexts:
        if context.schema_version != 1:
            raise ValueError("Unsupported context schema")
        for saved in StoredContext.model_validate(context.payload_body).files:
            diff = saved.diff
            public_file: dict[str, Any] = {
                "path": diff.new_path or diff.old_path,
                "previous_path": diff.old_path if diff.status == "renamed" else None,
                "status": diff.status,
            }
            path = str(public_file["path"])
            if path not in merged:
                merged[path] = {**public_file, "hunks": []}
            elif public_file != {k: v for k, v in merged[path].items() if k != "hunks"}:
                raise ValueError("Conflicting file identity")
            for hunk in diff.hunks:
                public = hunk.model_dump()
                if public not in merged[path]["hunks"]:
                    merged[path]["hunks"].append(public)
    for merged_file in merged.values():
        merged_file["hunks"].sort(key=lambda h: (h["old_start"], h["new_start"]))
        for side in ("old", "new"):
            end = 0
            for merged_hunk in merged_file["hunks"]:
                if merged_hunk[f"{side}_lines"]:
                    if merged_hunk[f"{side}_start"] < end:
                        raise ValueError("Overlapping or inconsistent hunks")
                    end = merged_hunk[f"{side}_start"] + merged_hunk[f"{side}_lines"]
    return {"files": list(merged.values())}
