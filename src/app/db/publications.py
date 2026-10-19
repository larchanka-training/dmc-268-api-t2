"""Short PostgreSQL transactions for retained review publication state."""

from uuid import UUID, uuid4

from sqlalchemy import Text, and_, func, literal_column, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.application.publication_ports import (
    PersistedFinding,
    PublicationAuthority,
    PublicationAuthorityError,
    PublicationClaim,
    PublicationInput,
    PublicationOperationSnapshot,
    PublicationState,
)
from app.db.models import (
    ChangeRequest,
    ChunkResult,
    ContextPayload,
    Finding,
    Publication,
    Repository,
    ReviewJob,
    TaskLease,
)
from app.db.publication_models import PublicationOperation
from app.db.review_events import append_review_event


class PostgresPublicationStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def begin(
        self,
        review_id: UUID,
        *,
        authority: PublicationAuthority,
        marker: str,
        payload_hash: str,
    ) -> PublicationClaim | None:
        if authority.task_kind != "publish" or authority.review_job_id != review_id:
            return None
        async with self._sessions.begin() as session:
            review = await session.get(ReviewJob, review_id, with_for_update=True)
            if review is None:
                return None
            await session.get(
                ChangeRequest, review.change_request_id, with_for_update=True
            )
            existing = await session.scalar(
                select(PublicationOperation)
                .where(
                    PublicationOperation.change_request_id == review.change_request_id
                )
                .with_for_update()
            )
            if existing is not None and existing.state != "RESOLVED":
                return None
            publication = await session.scalar(
                select(Publication)
                .where(
                    Publication.review_job_id == review_id,
                )
                .with_for_update()
            )
            lease_row = (
                await session.execute(
                    select(
                        TaskLease,
                        literal_column(
                            "to_jsonb(task_lease)->>'task_kind'", type_=Text()
                        ),
                    )
                    .where(
                        TaskLease.review_job_id == review_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            lease, persisted_kind = lease_row if lease_row is not None else (None, None)
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            if (
                lease is None
                or lease.owner != authority.owner
                or lease.fencing_token != authority.fencing_token
                or lease.expires_at <= now
                or persisted_kind not in (None, "publish")
                or review.status not in {"COMPLETED", "PARTIAL"}
                or review.finished_at is None
                or publication is None
                or publication.status != "PENDING"
                or publication.started_at is None
                or publication.deadline_at is None
                or publication.deadline_at <= now
            ):
                return None
            if existing is not None:
                await session.delete(existing)
                await session.flush()
            operation = PublicationOperation(
                id=uuid4(),
                change_request_id=review.change_request_id,
                review_job_id=review_id,
                owner=authority.owner,
                fencing_token=authority.fencing_token,
                marker=marker,
                payload_hash=payload_hash,
                state="IN_FLIGHT",
            )
            session.add(operation)
            await session.flush()
            claim = PublicationClaim(
                review_id,
                review.change_request_id,
                operation.id,
                authority,
                marker,
                payload_hash,
            )
            await append_review_event(
                session,
                review_id,
                event_type="PUBLICATION_STARTED",
                publication_status="PENDING",
            )
            if not await session.scalar(select(self._live_authority(claim))):
                raise PublicationAuthorityError("Publication authority expired")
            return claim

    async def check_authority(self, claim: PublicationClaim) -> bool:
        if (
            claim.authority.task_kind != "publish"
            or claim.authority.review_job_id != claim.review_id
        ):
            return False
        async with self._sessions() as session:
            current = await session.scalar(
                select(TaskLease.id)
                .join(
                    PublicationOperation,
                    PublicationOperation.review_job_id == TaskLease.review_job_id,
                )
                .join(ReviewJob, ReviewJob.id == TaskLease.review_job_id)
                .join(Publication, Publication.review_job_id == ReviewJob.id)
                .where(
                    TaskLease.review_job_id == claim.review_id,
                    TaskLease.owner == claim.authority.owner,
                    TaskLease.fencing_token == claim.authority.fencing_token,
                    TaskLease.expires_at > func.clock_timestamp(),
                    or_(
                        literal_column(
                            "to_jsonb(task_lease)->>'task_kind'", type_=Text()
                        ).is_(None),
                        literal_column(
                            "to_jsonb(task_lease)->>'task_kind'", type_=Text()
                        )
                        == "publish",
                    ),
                    PublicationOperation.id == claim.operation_id,
                    PublicationOperation.state == "IN_FLIGHT",
                    PublicationOperation.owner == claim.authority.owner,
                    PublicationOperation.fencing_token == claim.authority.fencing_token,
                    ReviewJob.status.in_(("COMPLETED", "PARTIAL")),
                    ReviewJob.finished_at.is_not(None),
                    Publication.status == "PENDING",
                    Publication.deadline_at > func.clock_timestamp(),
                )
            )
            return current is not None

    async def get(self, review_id: UUID) -> PublicationState | None:
        async with self._sessions() as session:
            publication = await session.scalar(
                select(Publication).where(
                    Publication.review_job_id == review_id,
                )
            )
            if publication is None:
                return None
            return PublicationState(
                review_id,
                publication.status,
                publication.provider_metadata,
                publication.started_at,
                publication.deadline_at,
                publication.finished_at,
            )

    async def check_reconciliation_authority(
        self,
        review_id: UUID,
        authority: PublicationAuthority,
    ) -> bool:
        """A fresh live claim permits provider reads, never another POST."""
        if authority.review_job_id != review_id or authority.task_kind != "publish":
            return False
        async with self._sessions() as session:
            current = await session.scalar(
                select(TaskLease.id)
                .join(
                    PublicationOperation,
                    PublicationOperation.review_job_id == TaskLease.review_job_id,
                )
                .join(ReviewJob, ReviewJob.id == TaskLease.review_job_id)
                .join(Publication, Publication.review_job_id == ReviewJob.id)
                .where(
                    TaskLease.review_job_id == review_id,
                    TaskLease.owner == authority.owner,
                    TaskLease.fencing_token == authority.fencing_token,
                    TaskLease.expires_at > func.clock_timestamp(),
                    or_(
                        literal_column(
                            "to_jsonb(task_lease)->>'task_kind'", type_=Text()
                        ).is_(None),
                        literal_column(
                            "to_jsonb(task_lease)->>'task_kind'", type_=Text()
                        )
                        == "publish",
                    ),
                    PublicationOperation.fencing_token < authority.fencing_token,
                    or_(
                        and_(
                            PublicationOperation.state == "UNKNOWN",
                            Publication.status == "UNKNOWN",
                        ),
                        and_(
                            PublicationOperation.state == "IN_FLIGHT",
                            Publication.status == "PENDING",
                        ),
                    ),
                    ReviewJob.status.in_(("COMPLETED", "PARTIAL")),
                    ReviewJob.finished_at.is_not(None),
                )
            )
            return current is not None

    async def get_operation(
        self,
        review_id: UUID,
    ) -> PublicationOperationSnapshot | None:
        async with self._sessions() as session:
            operation = await session.scalar(
                select(PublicationOperation).where(
                    PublicationOperation.review_job_id == review_id,
                )
            )
            if operation is None:
                return None
            return PublicationOperationSnapshot(
                operation.review_job_id,
                operation.change_request_id,
                operation.id,
                operation.owner,
                operation.fencing_token,
                operation.marker,
                operation.payload_hash,
                operation.state,
            )

    async def save(
        self,
        claim: PublicationClaim,
        *,
        status: str,
        provider_metadata: dict[str, object] | None,
    ) -> None:
        if status not in {"PUBLISHED", "PARTIAL", "FAILED", "UNKNOWN", "SKIPPED"}:
            raise ValueError("Invalid publication outcome")
        if (
            claim.authority.review_job_id != claim.review_id
            or claim.authority.task_kind != "publish"
        ):
            raise PublicationAuthorityError("Publication authority is not current")
        async with self._sessions.begin() as session:
            await session.get(ReviewJob, claim.review_id, with_for_update=True)
            await session.get(
                ChangeRequest, claim.change_request_id, with_for_update=True
            )
            await session.get(
                PublicationOperation, claim.operation_id, with_for_update=True
            )
            await session.scalar(
                select(TaskLease)
                .where(
                    TaskLease.review_job_id == claim.review_id,
                )
                .with_for_update()
            )
            saved = await session.scalar(
                update(Publication)
                .where(
                    Publication.review_job_id == claim.review_id,
                    Publication.status == "PENDING",
                    Publication.deadline_at.is_not(None),
                    self._live_authority(claim),
                    select(ReviewJob.id)
                    .where(
                        ReviewJob.id == claim.review_id,
                        ReviewJob.status.in_(("COMPLETED", "PARTIAL")),
                        ReviewJob.finished_at.is_not(None),
                    )
                    .exists(),
                    select(PublicationOperation.id)
                    .where(
                        PublicationOperation.id == claim.operation_id,
                        PublicationOperation.review_job_id == claim.review_id,
                        PublicationOperation.owner == claim.authority.owner,
                        PublicationOperation.fencing_token
                        == claim.authority.fencing_token,
                        PublicationOperation.state == "IN_FLIGHT",
                    )
                    .exists(),
                )
                .values(
                    status=status,
                    provider_metadata=provider_metadata,
                    finished_at=func.clock_timestamp(),
                )
                .returning(Publication.id)
            )
            if saved is None:
                raise PublicationAuthorityError("Publication authority is not current")
            operation = await session.scalar(
                update(PublicationOperation)
                .where(
                    PublicationOperation.id == claim.operation_id,
                    self._live_authority(claim),
                )
                .values(state="UNKNOWN" if status == "UNKNOWN" else "RESOLVED")
                .returning(PublicationOperation.id)
            )
            if operation is None:
                raise PublicationAuthorityError("Publication authority expired")
            await append_review_event(
                session,
                claim.review_id,
                event_type="PUBLICATION_FINISHED",
                publication_status=status,
            )
            if not await session.scalar(select(self._live_authority(claim))):
                raise PublicationAuthorityError("Publication authority expired")

    async def reconcile(
        self,
        review_id: UUID,
        *,
        authority: PublicationAuthority,
        status: str,
        confirmed_absent: bool,
        provider_metadata: dict[str, object] | None,
    ) -> None:
        """Record provider reconciliation under a separately granted live claim.

        The caller proves remote presence/absence before invoking this boundary.
        This operation never grants a lease, resets PENDING or schedules retries.
        """
        if status == "FAILED" and not confirmed_absent:
            raise ValueError("Reconciliation requires confirmed remote absence")
        if status not in {"FAILED", "PUBLISHED", "PARTIAL"}:
            raise ValueError("Invalid reconciliation outcome")
        if status != "FAILED" and (confirmed_absent or not provider_metadata):
            raise ValueError("Remote publication requires provider references")
        if authority.review_job_id != review_id or authority.task_kind != "publish":
            raise PublicationAuthorityError("Publication authority is not current")
        async with self._sessions.begin() as session:
            review = await session.get(ReviewJob, review_id, with_for_update=True)
            if review is None:
                raise PublicationAuthorityError("Publication is unavailable")
            await session.get(
                ChangeRequest, review.change_request_id, with_for_update=True
            )
            operation = await session.scalar(
                select(PublicationOperation)
                .where(
                    PublicationOperation.review_job_id == review_id,
                )
                .with_for_update()
            )
            if (
                operation is None
                or operation.state not in {"UNKNOWN", "IN_FLIGHT"}
                or operation.fencing_token >= authority.fencing_token
            ):
                raise PublicationAuthorityError("Reconciliation requires a fresh claim")
            if operation.state == "IN_FLIGHT" and confirmed_absent:
                raise PublicationAuthorityError("Remote in-flight effect is unresolved")
            source_status = "UNKNOWN" if operation.state == "UNKNOWN" else "PENDING"
            claim = PublicationClaim(
                review_id,
                review.change_request_id,
                operation.id,
                authority,
                operation.marker,
                operation.payload_hash,
            )
            await session.scalar(
                select(TaskLease)
                .where(
                    TaskLease.review_job_id == review_id,
                )
                .with_for_update()
            )
            saved = await session.scalar(
                update(Publication)
                .where(
                    Publication.review_job_id == review_id,
                    Publication.status == source_status,
                    self._live_authority(claim),
                )
                .values(
                    status=status,
                    finished_at=func.clock_timestamp(),
                    provider_metadata=(
                        {**(provider_metadata or {}), "confirmed_absent": True}
                        if confirmed_absent
                        else provider_metadata
                    ),
                )
                .returning(Publication.id)
            )
            if saved is None:
                raise PublicationAuthorityError("Reconciliation authority expired")
            operation.state = "RESOLVED"
            operation.owner = authority.owner
            operation.fencing_token = authority.fencing_token
            await append_review_event(
                session,
                review_id,
                event_type="PUBLICATION_RECONCILED",
                publication_status=status,
            )
            if not await session.scalar(select(self._live_authority(claim))):
                raise PublicationAuthorityError("Reconciliation authority expired")

    @staticmethod
    def _live_authority(claim: PublicationClaim) -> ColumnElement[bool]:
        return (
            select(TaskLease.id)
            .where(
                TaskLease.review_job_id == claim.review_id,
                TaskLease.owner == claim.authority.owner,
                TaskLease.fencing_token == claim.authority.fencing_token,
                TaskLease.expires_at > func.clock_timestamp(),
                or_(
                    literal_column(
                        "to_jsonb(task_lease)->>'task_kind'", type_=Text()
                    ).is_(None),
                    literal_column("to_jsonb(task_lease)->>'task_kind'", type_=Text())
                    == "publish",
                ),
            )
            .exists()
        )

    async def load(self, review_id: UUID) -> PublicationInput | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(ReviewJob, Repository, ChangeRequest)
                    .join(Repository, Repository.id == ReviewJob.repository_id)
                    .join(
                        ChangeRequest, ChangeRequest.id == ReviewJob.change_request_id
                    )
                    .where(ReviewJob.id == review_id)
                )
            ).one_or_none()
            if row is None:
                return None
            review, repository, change = row
            if not (
                review.base_sha
                and review.merge_base_sha
                and review.base_repository_external_id
                and review.source_repository_external_id
                and review.snapshot_provider == "github"
                and review.captured_at is not None
            ):
                return None
            contexts = tuple(
                await session.scalars(
                    select(ContextPayload.payload_body)
                    .where(ContextPayload.review_job_id == review_id)
                    .order_by(ContextPayload.created_at, ContextPayload.id)
                )
            )
            expected_snapshot = {
                "provider": "github",
                "base_repository_id": review.base_repository_external_id,
                "head_repository_id": review.source_repository_external_id,
                "head_sha": review.requested_head_sha,
                "base_sha": review.base_sha,
                "merge_base_sha": review.merge_base_sha,
            }
            if not contexts or any(
                not isinstance(context.get("snapshot"), dict)
                or any(
                    context["snapshot"].get(key) != value
                    for key, value in expected_snapshot.items()
                )
                for context in contexts
            ):
                return None
            findings = await session.scalars(
                select(Finding)
                .join(ChunkResult)
                .where(ChunkResult.review_job_id == review_id)
                .order_by(Finding.created_at, Finding.id)
            )
            return PublicationInput(
                review_id=review.id,
                change_request_id=review.change_request_id,
                owner=repository.owner,
                name=repository.repo_name,
                repository_external_id=review.base_repository_external_id,
                number=int(change.external_number),
                head_sha=review.requested_head_sha,
                base_sha=review.base_sha,
                merge_base_sha=review.merge_base_sha,
                config_digest=review.config_digest,
                summary=review.final_summary or "",
                coverage=review.coverage,
                contexts=contexts,
                findings=tuple(
                    PersistedFinding(
                        **{
                            field: getattr(finding, field)
                            for field in PersistedFinding.__dataclass_fields__
                        }
                    )
                    for finding in findings
                ),
            )
