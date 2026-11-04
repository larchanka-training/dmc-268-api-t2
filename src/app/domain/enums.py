from enum import StrEnum


class ReviewStatus(StrEnum):
    QUEUED = "QUEUED"
    FETCHING_DIFF = "FETCHING_DIFF"
    PARSING_CONTEXT = "PARSING_CONTEXT"
    LLM_PROCESSING = "LLM_PROCESSING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ReviewPhase(StrEnum):
    FETCHING_DIFF = "FETCHING_DIFF"
    PARSING_CONTEXT = "PARSING_CONTEXT"
    LLM_PROCESSING = "LLM_PROCESSING"


class ReviewReasonCode(StrEnum):
    QUEUE_DEADLINE_EXCEEDED = "QUEUE_DEADLINE_EXCEEDED"
    VCS_RATE_LIMITED = "VCS_RATE_LIMITED"
    VCS_UNAVAILABLE = "VCS_UNAVAILABLE"
    VCS_ACCESS_DENIED = "VCS_ACCESS_DENIED"
    PR_STALE_OR_CLOSED = "PR_STALE_OR_CLOSED"
    DIFF_UNTRUSTWORTHY = "DIFF_UNTRUSTWORTHY"
    SOURCE_BLOB_UNAVAILABLE = "SOURCE_BLOB_UNAVAILABLE"
    AST_PARSE_FAILED = "AST_PARSE_FAILED"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    LLM_OUTPUT_INVALID = "LLM_OUTPUT_INVALID"
    ANALYSIS_DEADLINE_EXCEEDED = "ANALYSIS_DEADLINE_EXCEEDED"


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
