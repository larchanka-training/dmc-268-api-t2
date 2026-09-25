from enum import StrEnum


class ReviewStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ReviewStage(StrEnum):
    SNAPSHOT = "snapshot"
    CONTEXT = "context"
    INFERENCE = "inference"
    VALIDATION = "validation"
    DONE = "done"


class PublicationStatus(StrEnum):
    NOT_READY = "NOT_READY"
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    SKIPPED = "SKIPPED"


class FindingSide(StrEnum):
    OLD = "OLD"
    NEW = "NEW"


class FindingCategory(StrEnum):
    SECURITY = "security"
    CORRECTNESS = "correctness"
    PERFORMANCE = "performance"
    MAINTAINABILITY = "maintainability"


class FindingSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TaskKind(StrEnum):
    ANALYZE = "analyze"
    PUBLISH = "publish"


class QuotaType(StrEnum):
    STARTS = "starts"
    ACTIVE_JOBS = "active_jobs"


class RepositoryRole(StrEnum):
    REVIEWER = "reviewer"
    ADMIN = "admin"
