"""Prepare and publish frozen, validated review results to GitHub."""

from __future__ import annotations

# ruff: noqa: RUF001 - publication text is intentionally Russian
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from itertools import groupby
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from app.application.github_ports import (
    GithubFailure,
    GithubRemoteComment,
    GithubRemoteReview,
    GithubRequest,
    GithubReviewReference,
    GithubWriteFailure,
)
from app.application.publication_ports import (
    PersistedFinding,
    PublicationAuthority,
    PublicationAuthorityError,
    PublicationClaim,
    PublicationInput,
    PublicationStore,
)
from app.domain.contract_validation import validate_hunks
from app.review.schemas.context import FileDiff
from app.review.schemas.finding import LLMFinding
from app.review.validation import has_changed_anchor, mask_secrets


@dataclass(frozen=True, slots=True)
class PreparedGithubReview:
    request: GithubRequest
    marker: str
    body: str
    comments: tuple[dict[str, object], ...]


def _old_changes_are_covered(
    item: PersistedFinding, files: list[FileDiff], ranges: list[dict[str, int]]
) -> bool:
    """Project OLD replacements onto their contiguous NEW change blocks."""
    for file in files:
        for hunk in file.hunks:
            for changed, lines in groupby(
                hunk.lines, key=lambda line: line.kind != "context"
            ):
                if not changed:
                    continue
                block = tuple(lines)
                if not any(
                    line.kind == "deleted"
                    and line.old_line is not None
                    and item.start_line <= line.old_line <= item.end_line
                    for line in block
                ):
                    continue
                added = {
                    line.new_line
                    for line in block
                    if line.kind == "added" and line.new_line is not None
                }
                if not added or not all(
                    any(
                        interval["start_line"] <= number <= interval["end_line"]
                        for interval in ranges
                    )
                    for number in added
                ):
                    return False
    return True


def _publishable(item: PersistedFinding, review: PublicationInput) -> bool:
    try:
        finding = LLMFinding.model_validate(
            {name: getattr(item, name) for name in LLMFinding.model_fields}
        )
        files = []
        for context in review.contexts:
            for file in context["files"]:
                diff = file["diff"]
                validate_hunks(diff["hunks"])
                files.append(FileDiff.model_validate(diff))
        covered = {file["path"] for file in review.coverage["included"]}
        if _github_path(item, review) not in covered:
            return False
        if not has_changed_anchor(finding, files):
            return False
        matching_files = [
            file
            for file in files
            if (file.new_path if item.side == "NEW" else file.old_path) == item.path
        ]
        coverage_side = (
            "NEW"
            if any(
                line.kind == "added"
                for file in matching_files
                for hunk in file.hunks
                for line in hunk.lines
            )
            else "OLD"
        )
        ranges = [
            interval
            for file in review.coverage["included"]
            if file["path"] == _github_path(item, review)
            for interval in file["ranges"]
        ]
        if item.side != coverage_side:
            return _old_changes_are_covered(item, matching_files, ranges)
        changed = {
            number
            for file in matching_files
            for hunk in file.hunks
            for line in hunk.lines
            if line.kind == ("added" if item.side == "NEW" else "deleted")
            and (number := (line.new_line if item.side == "NEW" else line.old_line))
            is not None
            and item.start_line <= number <= item.end_line
        }
        return all(
            any(
                interval["start_line"] <= line <= interval["end_line"]
                for interval in ranges
            )
            for line in changed
        )
    except ValidationError, ValueError, KeyError, TypeError:
        return False


def _github_path(item: PersistedFinding, review: PublicationInput) -> str:
    for context in review.contexts:
        for file in context["files"]:
            diff = file["diff"]
            path = diff["old_path"] if item.side == "OLD" else diff["new_path"]
            if path == item.path:
                return str(diff["new_path"] or diff["old_path"])
    return item.path


def _suppress_key(item: PersistedFinding) -> str:
    """Line-free identity hash for cross-cycle suppression (issue #53).

    The full fingerprint pins the line range, so a finding moved between
    commits changes it and would be re-published; the suppress key keeps the
    same normalized components minus the range and survives pure line shifts.
    Published inline as a ``dmc268-suppress`` marker next to the fingerprint.
    """
    key = "|".join(
        [
            item.path,
            item.side,
            item.category,
            " ".join(item.title.lower().split()),
        ]
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _already_published(item: PersistedFinding, published: frozenset[str]) -> bool:
    return item.fingerprint in published or _suppress_key(item) in published


def prepare_github_review(
    review: PublicationInput, *, published: frozenset[str] = frozenset()
) -> PreparedGithubReview:
    """Render saved analysis without another model call.

    ``published`` carries identities already published in earlier review
    cycles (full fingerprints and line-free suppress keys): those findings
    are not posted again as inline comments, they are only named as already
    noted in the review body (issue #53).
    """
    expected = {
        "provider": "github",
        "base_repository_id": review.repository_external_id,
        "base_sha": review.base_sha,
        "head_sha": review.head_sha,
        "merge_base_sha": review.merge_base_sha,
    }
    for context in review.contexts:
        snapshot = context.get("snapshot", {})
        if any(snapshot.get(key) != value for key, value in expected.items()):
            raise ValueError("Frozen context snapshot mismatch")
    marker = (
        f"<!-- dmc268-review:v1 review={review.review_id} "
        f"head={review.head_sha} config={review.config_digest} -->"
    )
    files = "\n".join(f"- {item['path']}" for item in review.coverage["included"])
    severity = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    eligible = tuple(
        item
        for item in review.findings
        if severity.get(item.severity, 4) <= severity[review.minimum_severity]
    )
    summary = review.summary
    risks = "\n".join(f"- {item.severity}: {item.title}" for item in eligible)
    if review.minimum_severity != "low":
        summary = (
            "Проверка завершена. Минимальный уровень серьёзности: "
            f"{review.minimum_severity}. "
            f"Замечаний на выбранном уровне: {len(eligible)}."
        )
        if not risks:
            risks = "На выбранном уровне серьёзности замечаний нет."
    elif not risks:
        risks = "Замечаний не обнаружено в проверенной части изменений."
    skipped = "\n".join(
        f"- {item['path']}: {item['reason']}" for item in review.coverage["skipped"]
    )
    unique: dict[str, PersistedFinding] = {}
    for item in eligible:
        if _publishable(item, review):
            unique.setdefault(item.fingerprint, item)
    ranked = sorted(
        unique.values(),
        key=lambda item: (
            severity[item.severity],
            item.path,
            item.side,
            item.start_line,
            item.end_line,
            item.fingerprint,
        ),
    )
    noted = [item for item in ranked if _already_published(item, published)][:20]
    publishable = [item for item in ranked if not _already_published(item, published)][
        :20
    ]
    comments: tuple[dict[str, object], ...] = tuple(
        {
            "path": _github_path(item, review),
            "line": item.end_line,
            "side": "LEFT" if item.side == "OLD" else "RIGHT",
            "body": mask_secrets(
                f"**{item.severity}: {item.title}**\n\n"
                + (f"Правило: {item.rule_ref}\n\n" if item.rule_ref else "")
                + f"{item.explanation}\n\n{item.recommendation}\n\n"
                + f"<!-- dmc268-suppress:{_suppress_key(item)} -->\n"
                f"<!-- dmc268-finding:{item.fingerprint} -->"
            ),
            **(
                {
                    "start_line": item.start_line,
                    "start_side": "LEFT" if item.side == "OLD" else "RIGHT",
                }
                if item.start_line != item.end_line
                else {}
            ),
        }
        for item in publishable
    )
    omitted = len(eligible) - len(comments) - len(noted)
    already_noted = ""
    if noted:
        already_noted = "\n\nУже отмечено в прошлых циклах:\n" + "\n".join(
            f"- {item.path}: {item.severity}: {item.title} "
            f"<!-- dmc268-finding:{item.fingerprint} -->"
            for item in noted
        )
    return PreparedGithubReview(
        request=GithubRequest(
            review.owner,
            review.name,
            review.repository_external_id,
            review.number,
            review.head_sha,
            review.base_sha,
        ),
        marker=marker,
        body=mask_secrets(
            f"{marker}\n## Обзор AI-ревью\n\n{summary}\n\n"
            f"Проверенные файлы:\n{files}\n\nРиски:\n{risks}"
            f"{already_noted}"
            f"\n\nНе опубликованы inline: {omitted}"
            f"\n\nНе проверены:\n{skipped or 'Нет пропущенных файлов.'}"
            f"\n\nКачество контекста: {review.coverage['context_quality']}"
        ),
        comments=comments,
    )


class GithubPublicationGateway(Protocol):
    def with_authority(
        self,
        authorize: Callable[[], Awaitable[None]],
    ) -> GithubPublicationGateway: ...

    async def check_pull(self, request: GithubRequest) -> None: ...

    async def list_reviews(
        self, request: GithubRequest
    ) -> list[GithubRemoteReview]: ...

    async def create_review(
        self,
        request: GithubRequest,
        *,
        body: str,
        comments: Sequence[dict[str, object]],
    ) -> GithubReviewReference: ...

    async def list_review_comments(
        self,
        request: GithubRequest,
        remote_review_id: str,
    ) -> list[GithubRemoteComment]: ...


class GithubPublisher:
    def __init__(
        self,
        store: PublicationStore,
        github: GithubPublicationGateway,
        *,
        authority: PublicationAuthority,
        bot_user_id: str,
    ) -> None:
        self._store = store
        self._github = github
        self._authority = authority
        self._bot_user_id = bot_user_id

    async def _require_authority(self, claim: PublicationClaim) -> None:
        if not await self._store.check_authority(claim):
            raise PublicationAuthorityError("Publication authority is unavailable")

    async def _require_lease(self) -> None:
        if not await self._store.check_publication_authority(self._authority):
            raise PublicationAuthorityError("Publication authority is unavailable")

    @staticmethod
    def _payload_hash(prepared: PreparedGithubReview) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "head_sha": prepared.request.head_sha,
                    "body": prepared.body,
                    "comments": prepared.comments,
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    async def _require_reconciliation_authority(self, review_id: UUID) -> None:
        if not await self._store.check_reconciliation_authority(
            review_id, self._authority
        ):
            raise PublicationAuthorityError("Reconciliation authority is unavailable")

    def _overview_matches(
        self,
        remote: GithubRemoteReview,
        prepared: PreparedGithubReview,
    ) -> bool:
        return (
            remote.body.split("\n", 1)[0] == prepared.marker
            and remote.author_id == self._bot_user_id
            and remote.commit_id == prepared.request.head_sha
            and remote.state == "COMMENTED"
        )

    async def _note_denial(self, claim: PublicationClaim, reason_code: str) -> None:
        # A bot denial flags the integration; it never removes access links.
        if reason_code == "VCS_ACCESS_DENIED":
            await self._store.mark_bot_denied(claim)

    async def _unknown_overview(
        self,
        claim: PublicationClaim,
        remote_review_id: str | None,
        *,
        reason_code: str = "VCS_UNAVAILABLE",
    ) -> dict[str, object]:
        await self._note_denial(claim, reason_code)
        metadata: dict[str, object] = {
            "confirmed_absent": False,
            "reason_code": reason_code,
        }
        if remote_review_id is not None:
            metadata["remote_review_id"] = remote_review_id
        await self._store.save(claim, status="UNKNOWN", provider_metadata=metadata)
        return {"status": "UNKNOWN", **metadata}

    def _comment_references(
        self,
        review: PublicationInput,
        prepared: PreparedGithubReview,
        comments: list[GithubRemoteComment],
    ) -> dict[str, str]:
        references: dict[str, str] = {}
        for expected in prepared.comments:
            for finding in review.findings:
                tag = f"<!-- dmc268-finding:{finding.fingerprint} -->"
                if not str(expected["body"]).endswith(tag):
                    continue
                for comment in comments:
                    if (
                        comment.body.split("\n")[-1] == tag
                        and comment.author_id == self._bot_user_id
                        and comment.commit_id == review.head_sha
                        and comment.path == expected["path"]
                        and comment.line == expected["line"]
                        and comment.side == expected["side"]
                        and comment.start_line == expected.get("start_line")
                        and comment.start_side == expected.get("start_side")
                    ):
                        references[finding.fingerprint] = comment.remote_comment_id
        return references

    @staticmethod
    def _marker_cycle_id(body: str) -> UUID | None:
        """Review id of the cycle that published this overview, if ours."""
        match = re.fullmatch(
            r"<!-- dmc268-review:v1 review=(\S+) head=\S+ config=\S+ -->",
            body.split("\n", 1)[0],
        )
        if match is None:
            return None
        try:
            return UUID(match.group(1))
        except ValueError:
            return None

    async def _already_noted_fingerprints(
        self,
        review: PublicationInput,
        github: GithubPublicationGateway,
    ) -> frozenset[str]:
        """Identities the bot published in earlier cycles of this PR.

        Full fingerprints (``dmc268-finding:``) and line-free suppress keys
        (``dmc268-suppress:``), so a repeat survives a line shift between
        commits (issue #53 review). The reads happen before the claim exists,
        so they carry no operation; the gateway is still lease-fenced to keep
        expired workers off the provider (the same posture as AC-PUB-15).
        """
        request = GithubRequest(
            review.owner,
            review.name,
            review.repository_external_id,
            review.number,
            review.head_sha,
            review.base_sha,
        )
        identities: set[str] = set()
        for item in await github.list_reviews(request):
            if item.author_id != self._bot_user_id:
                continue
            if self._marker_cycle_id(item.body) == review.review_id:
                continue
            for comment in await github.list_review_comments(
                request, item.remote_review_id
            ):
                if comment.author_id != self._bot_user_id:
                    continue
                identities.update(
                    re.findall(r"<!-- dmc268-finding:(.*?) -->", comment.body)
                )
                identities.update(
                    re.findall(r"<!-- dmc268-suppress:(.*?) -->", comment.body)
                )
        return frozenset(identities)

    async def publish(  # noqa: PLR0911, PLR0912, PLR0915 - explicit durable outcome branches
        self,
        *,
        review_job_id: UUID,
    ) -> dict[str, object]:
        if (
            self._authority.review_job_id != review_job_id
            or self._authority.task_kind != "publish"
        ):
            raise PublicationAuthorityError("Publication authority is unavailable")
        state = await self._store.get(review_job_id)
        if state is not None and state.status in {
            "PUBLISHED",
            "PARTIAL",
            "UNKNOWN",
            "FAILED",
            "SKIPPED",
        }:
            return {**(state.provider_metadata or {}), "status": state.status}
        review = await self._store.load(review_job_id)
        if review is None:
            raise LookupError("Frozen publication input is unavailable")
        denial_reason: str | None = None
        noted: frozenset[str]
        if review.integration_status != "healthy":
            # AC6: no new provider attempts for an unhealthy repository —
            # the claim-free suppression read included.
            noted = frozenset()
        else:
            try:
                noted = await self._already_noted_fingerprints(
                    review,
                    self._github.with_authority(self._require_lease),
                )
            except GithubFailure as error:
                # The claim-free read precedes begin(): transient failures stay
                # retryable claim-free, but a bot denial (AC6) must not loop —
                # it parks durably once the claim exists below.
                if error.reason_code != "VCS_ACCESS_DENIED":
                    raise
                denial_reason = error.reason_code
                noted = frozenset()
        prepared = prepare_github_review(review, published=noted)
        payload_hash = self._payload_hash(prepared)
        claim = await self._store.begin(
            review_job_id,
            authority=self._authority,
            marker=prepared.marker,
            payload_hash=payload_hash,
        )
        if claim is None:
            raise PublicationAuthorityError("Publication authority is unavailable")
        github = self._github.with_authority(lambda: self._require_authority(claim))
        await self._require_authority(claim)
        if review.integration_status != "healthy":
            blocked: dict[str, object] = {"reason_code": "INTEGRATION_UNHEALTHY"}
            await self._store.save(claim, status="SKIPPED", provider_metadata=blocked)
            return {"status": "SKIPPED", **blocked}
        if denial_reason is not None:
            return await self._unknown_overview(
                claim,
                None,
                reason_code=denial_reason,
            )
        try:
            await github.check_pull(prepared.request)
        except GithubFailure as error:
            if error.reason_code != "PR_STALE_OR_CLOSED":
                return await self._unknown_overview(
                    claim,
                    None,
                    reason_code=error.reason_code,
                )
            metadata: dict[str, object] = {"reason_code": error.reason_code}
            await self._store.save(claim, status="SKIPPED", provider_metadata=metadata)
            return {"status": "SKIPPED", **metadata}
        remote: GithubReviewReference | None = None
        try:
            await self._require_authority(claim)
            reviews = await github.list_reviews(prepared.request)
            matching = [
                item
                for item in reviews
                if (
                    item.body.split("\n", 1)[0] == prepared.marker
                    and item.author_id == self._bot_user_id
                )
            ]
            if matching:
                remote = GithubReviewReference(matching[0].remote_review_id)
                if len(matching) != 1 or not self._overview_matches(
                    matching[0], prepared
                ):
                    return await self._unknown_overview(claim, remote.remote_review_id)
            else:
                await self._require_authority(claim)
                remote = await github.create_review(
                    prepared.request,
                    body=prepared.body,
                    comments=prepared.comments,
                )
                await self._require_authority(claim)
                receipts = await github.list_reviews(prepared.request)
                created = [
                    item
                    for item in receipts
                    if item.remote_review_id == remote.remote_review_id
                ]
                if len(created) != 1 or not self._overview_matches(
                    created[0], prepared
                ):
                    return await self._unknown_overview(claim, remote.remote_review_id)
        except GithubWriteFailure as error:
            await self._note_denial(claim, error.reason_code)
            status = "FAILED" if error.remote_effect == "absent" else "UNKNOWN"
            metadata = {
                "reason_code": error.reason_code,
                "confirmed_absent": error.remote_effect == "absent",
            }
            await self._store.save(claim, status=status, provider_metadata=metadata)
            return {"status": status, **metadata}
        except GithubFailure as error:
            return await self._unknown_overview(
                claim,
                remote.remote_review_id if remote else None,
                reason_code=error.reason_code,
            )
        await self._require_authority(claim)
        try:
            comments = await github.list_review_comments(
                prepared.request,
                remote.remote_review_id,
            )
        except GithubFailure as error:
            return await self._unknown_overview(
                claim,
                remote.remote_review_id,
                reason_code=error.reason_code,
            )
        comment_ids = self._comment_references(review, prepared, comments)
        status = (
            "PUBLISHED" if len(comment_ids) == len(prepared.comments) else "PARTIAL"
        )
        metadata = {
            "remote_review_id": remote.remote_review_id,
            "comment_ids": comment_ids,
        }
        await self._store.save(claim, status=status, provider_metadata=metadata)
        return {"status": status, **metadata}

    async def reconcile(self, *, review_job_id: UUID) -> dict[str, object]:
        operation = await self._store.get_operation(review_job_id)
        review = await self._store.load(review_job_id)
        if operation is None or review is None:
            raise PublicationAuthorityError("Reconciliation input is unavailable")
        github = self._github.with_authority(
            lambda: self._require_reconciliation_authority(review_job_id)
        )
        await self._require_reconciliation_authority(review_job_id)
        noted = await self._already_noted_fingerprints(review, github)
        prepared = prepare_github_review(review, published=noted)
        if (
            operation.marker != prepared.marker
            or operation.payload_hash != self._payload_hash(prepared)
        ):
            raise PublicationAuthorityError("Reconciliation payload is unavailable")
        reviews = await github.list_reviews(prepared.request)
        matching = [
            item
            for item in reviews
            if (
                item.body.split("\n", 1)[0] == operation.marker
                and item.author_id == self._bot_user_id
            )
        ]
        if not matching:
            # A timed-out POST may still commit after this read. Absence alone
            # cannot release the PR-scoped operation or permit another write.
            return {"status": "UNKNOWN", "confirmed_absent": False}
        if len(matching) != 1 or not self._overview_matches(matching[0], prepared):
            return {"status": "UNKNOWN"}
        remote = matching[0]
        await self._require_reconciliation_authority(review_job_id)
        comments = await github.list_review_comments(
            prepared.request,
            remote.remote_review_id,
        )
        references = self._comment_references(review, prepared, comments)
        status = "PUBLISHED" if len(references) == len(prepared.comments) else "PARTIAL"
        metadata: dict[str, object] = {
            "remote_review_id": remote.remote_review_id,
            "comment_ids": references,
        }
        await self._store.reconcile(
            review_job_id,
            authority=self._authority,
            status=status,
            confirmed_absent=False,
            provider_metadata=metadata,
        )
        return {"status": status, **metadata}
