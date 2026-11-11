"""Aggregate safe evidence from the four scripted rules-state E2E scenarios."""

import argparse
import json
import sys
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

CASES = ("absent", "valid", "empty", "adversarial")
NOTE = (
    "E2E integration evidence only. LLM and GitHub publication used scripted HTTP "
    "transport; this is not a measurement of real model quality."
)


class ScenarioRecord(BaseModel):
    """Only scalar integration receipts, without provider bodies."""

    model_config = ConfigDict(strict=True, extra="forbid")

    case_id: str
    base_revision_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    file_parser_state: str
    effective_rules_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    file_content_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None
    analysis_status: Literal["COMPLETED"]
    findings_count: Annotated[int, Field(ge=0)]
    reflection_succeeded_calls: Annotated[int, Field(ge=1)]
    publication_status: Literal["PUBLISHED"]
    github_review_posts_before_replay: int
    github_review_posts_after_replay: int
    replay_worker_processed: bool
    replay_without_second_post: bool
    sse_http_status: Annotated[int, Field(ge=200, le=200)]
    sse_snapshot_received: bool
    sse_event_received: bool
    sse_published_received: bool
    sse_credentials_absent: bool


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    paths = list(args.records_dir.glob("*.json"))
    if len(paths) != 4:  # noqa: PLR2004 -- four agreed E2E scenarios
        print("Expected exactly four scenarios", file=sys.stderr)
        return 1
    try:
        records = [
            ScenarioRecord.model_validate_json(
                path.read_text(encoding="utf-8")
            ).model_dump()
            for path in paths
        ]
        if any(
            row["case_id"] != "absent" and row["file_content_digest"] is None
            for row in records
        ):
            raise ValueError("Missing file content digest")
        if any(
            row["case_id"] == "absent" and row["file_content_digest"] is not None
            for row in records
        ):
            raise ValueError("Unexpected file content digest")
        if not all(
            all(
                row[key]
                for key in (
                    "sse_snapshot_received",
                    "sse_event_received",
                    "sse_published_received",
                    "sse_credentials_absent",
                )
            )
            for row in records
        ):
            raise ValueError("Incomplete SSE evidence")
    except ValueError, ValidationError, OSError:
        print("Invalid scenario evidence", file=sys.stderr)
        return 1
    if {row["case_id"] for row in records} != set(CASES):
        print("Expected exactly four scenarios, each unique", file=sys.stderr)
        return 1
    if any(
        row["file_parser_state"]
        != ("valid" if row["case_id"] == "adversarial" else row["case_id"])
        for row in records
    ):
        print("Invalid parser state for scenario", file=sys.stderr)
        return 1
    if any(
        not row["replay_worker_processed"]
        or not row["replay_without_second_post"]
        or row["github_review_posts_before_replay"] != 1
        or row["github_review_posts_after_replay"] != 1
        for row in records
    ):
        print("Missing observed replay evidence", file=sys.stderr)
        return 1
    records.sort(key=lambda row: CASES.index(row["case_id"]))
    report = {"quality_measurement": False, "note": NOTE, "scenarios": records}
    markdown = [
        "# Rules states E2E report",
        "",
        "quality_measurement: false",
        "",
        NOTE,
        "",
        "4/4 scenarios recorded.",
        "",
        "| Case | Parser state | Analysis | Findings | Reflection calls | Publication |"
        " POST before/after replay | Replay without second POST | Authenticated SSE |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in records:
        markdown.append(
            f"| {row['case_id']} | {row['file_parser_state']} | "
            f"{row['analysis_status']} | {row['findings_count']} | "
            f"{row['reflection_succeeded_calls']} | {row['publication_status']} | "
            f"{row['github_review_posts_before_replay']}/"
            f"{row['github_review_posts_after_replay']} | "
            f"{row['replay_without_second_post']} | HTTP {row['sse_http_status']} |"
        )
    markdown.extend(
        [
            "",
            "## Source provenance",
            "",
            "| Case | base_revision_sha | effective_rules_digest |"
            " file_content_digest |",
            "| --- | --- | --- | --- |",
        ]
    )
    for row in records:
        digest = "N/A" if row["case_id"] == "absent" else row["file_content_digest"]
        markdown.append(
            f"| {row['case_id']} | {row['base_revision_sha']} | "
            f"{row['effective_rules_digest']} | {digest} |"
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "rules-e2e-states.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "rules-e2e-states.md").write_text(
        "\n".join(markdown) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
