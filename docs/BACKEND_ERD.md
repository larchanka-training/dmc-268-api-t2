# ERD backend — DMC-268 API (Team 2)

**Статус:** схема хранения данных для предметного ядра backend.  
**Основание:** актуальный `SYSTEM_DESIGN.md` и модель данных backend.

---

## 1. Назначение

Схема описывает долговременное состояние процесса AI-review: настройки repository, Change Request, lifecycle ReviewJob, контекст и результаты LLM, Finding, состояние публикации, transactional outbox, lease, idempotency и квоты.

---

## 2. ERD

```mermaid
erDiagram
    User ||--o{ RepositoryAccess : доступ
    User ||--o{ QuotaUsage : квоты
    User ||--o{ ReviewJob : инициирует

    Repository ||--o{ RepositoryAccess : предоставляет
    Repository ||--o{ RepositorySettings : версии
    Repository ||--o{ QuotaUsage : учитывает
    Repository ||--o{ ChangeRequest : содержит
    RepositorySettings ||--o{ ReviewJob : фиксируются_для
    Repository ||--|| RepositorySettings : текущие_настройки

    ChangeRequest ||--o{ ReviewJob : проверяется
    ReviewJob ||--o{ ReviewEvent : события
    ReviewJob ||--o{ ContextPayload : формирует
    ReviewJob ||--|| Publication : публикация
    ReviewJob ||--o{ OutboxEvent : создаёт
    ReviewJob ||--o| TaskLease : lease
    ReviewJob ||--o{ IdempotencyRecord : результат
    ReviewJob ||--o{ ReviewJob : rerun_of

    ContextPayload ||--o| ChunkResult : результат
    ChunkResult ||--o{ Finding : находки

    Repository {
        UUID id PK
        TEXT external_id
        TEXT provider_name
        TEXT repo_name
        TEXT owner
        UUID current_settings_id FK
    }

    ChangeRequest {
        UUID id PK
        UUID repository_id FK
        TEXT external_number
        TEXT source_repository_external_id
        TEXT source_ref
        TEXT base_ref
        TEXT head_sha
        TEXT base_sha
        TEXT title
        TEXT description
        TEXT current_status
    }

    ReviewJob {
        UUID id PK
        UUID change_request_id FK
        UUID repository_settings_id FK
        TEXT rules_digest
        TEXT trigger_type
        UUID initiator_user_id FK
        TIMESTAMPTZ captured_at
        TEXT snapshot_provider
        TEXT requested_head_sha
        TEXT base_sha
        TEXT merge_base_sha
        JSONB provider_diff_version
        TEXT source_repository_external_id
        TEXT base_repository_external_id
        UUID rerun_of FK
        TIMESTAMPTZ retry_at
        TEXT model_ref
        TEXT model_digest
        JSONB model_settings
        TEXT prompt_version
        TEXT prompt_digest
        TIMESTAMPTZ created_at
        TIMESTAMPTZ started_at
        TIMESTAMPTZ finished_at
        TEXT status
        TEXT stage
        TIMESTAMPTZ queue_deadline_at
        TIMESTAMPTZ analysis_deadline_at
        JSONB coverage
        TEXT final_summary
    }

    ReviewEvent {
        UUID id PK
        UUID review_job_id FK
        TIMESTAMPTZ occurred_at
        TEXT event_type
        TEXT status
        TEXT stage
        TEXT reason_code
    }

    ContextPayload {
        UUID id PK
        UUID review_job_id FK
        INT schema_version
        JSONB payload_body
        TIMESTAMPTZ created_at
    }

    ChunkResult {
        UUID id PK
        UUID context_payload_id FK
        INT schema_version
        TEXT summary
        JSONB limitations
        JSONB usage
        INT latency_ms
        TIMESTAMPTZ created_at
    }

    Finding {
        UUID id PK
        UUID chunk_result_id FK
        TEXT fingerprint
        TEXT path
        TEXT side
        INT start_line
        INT end_line
        TEXT category
        TEXT severity
        TEXT title
        TEXT explanation
        TEXT evidence
        TEXT recommendation
        TIMESTAMPTZ created_at
    }

    Publication {
        UUID id PK
        UUID review_job_id FK
        TEXT status
        JSONB provider_metadata
        TIMESTAMPTZ started_at
        TIMESTAMPTZ deadline_at
        TIMESTAMPTZ finished_at
        TIMESTAMPTZ created_at
    }

    OutboxEvent {
        UUID id PK
        UUID review_job_id FK
        INT schema_version
        TEXT task_kind
        INT attempt
        TEXT trace_id
        TIMESTAMPTZ created_at
        TIMESTAMPTZ broker_published_at
    }

    TaskLease {
        UUID id PK
        UUID review_job_id FK
        TEXT owner
        TIMESTAMPTZ expires_at
        BIGINT fencing_token
    }

    RepositoryAccess {
        UUID id PK
        UUID repository_id FK
        UUID user_id FK
        TEXT role
    }

    RepositorySettings {
        UUID id PK
        UUID repository_id FK
        JSONB rules
        JSONB ignores
        INT max_starts_per_hour
        INT max_active_jobs
        TEXT output_language
        INT surrounding_lines
        TIMESTAMPTZ created_at
        TEXT rules_digest
    }

    QuotaUsage {
        UUID id PK
        UUID repository_id FK
        UUID user_id FK
        TEXT type
        INT value
        TIMESTAMPTZ window_start
    }

    User {
        UUID id PK
    }

    WebhookReceipt {
        UUID id PK
        TEXT provider_name
        TEXT delivery_id
        TIMESTAMPTZ received_at
    }

    IdempotencyRecord {
        UUID id PK
        TEXT scope
        TEXT idempotency_key
        TEXT request_hash
        UUID review_job_id FK
        TIMESTAMPTZ created_at
        TIMESTAMPTZ expires_at
    }
```

---

## 3. Кардинальности и ограничения

| Объект | Ограничение |
| --- | --- |
| Repository → ChangeRequest | `1:N` |
| ChangeRequest → ReviewJob | `1:N` |
| ReviewJob → ContextPayload | `1:N` |
| ContextPayload → ChunkResult | `1:0..1`, `UNIQUE(context_payload_id)` |
| ChunkResult → Finding | `1:N` |
| ReviewJob → Publication | `1:1`, `UNIQUE(review_job_id)` |
| ReviewJob → TaskLease | `1:0..1`, `UNIQUE(review_job_id)` |
| ReviewJob → OutboxEvent | `1:N` |
| RepositoryAccess | `UNIQUE(repository_id, user_id)` |
| ChangeRequest | `UNIQUE(repository_id, external_number)` |
| WebhookReceipt | `UNIQUE(provider_name, delivery_id)` |
| IdempotencyRecord | `UNIQUE(scope, idempotency_key)` |

Допустимые значения:

- `ReviewJob.status`: `QUEUED | RUNNING | COMPLETED | PARTIAL | FAILED | SKIPPED`;
- `ReviewJob.stage`: `snapshot | context | inference | validation | done | NULL`;
- `Publication.status`: `NOT_READY | PENDING | PUBLISHED | PARTIAL | FAILED | UNKNOWN | SKIPPED`;
- `Finding.side`: `OLD | NEW`;
- `Finding.category`: `security | correctness | performance | maintainability`;
- `Finding.severity`: `critical | high | medium | low`;
- `OutboxEvent.task_kind`: `analyze | publish`;
- `QuotaUsage.type`: `starts | active_jobs`;
- `RepositoryAccess.role`: `reviewer | admin`.

---

## 4. JSONB

Поля JSONB:

- `ReviewJob.model_settings`;
- `ReviewJob.provider_diff_version`;
- `ReviewJob.coverage`;
- `ContextPayload.payload_body`;
- `ChunkResult.limitations`;
- `ChunkResult.usage`;
- `Publication.provider_metadata`;
- `RepositorySettings.rules`;
- `RepositorySettings.ignores`.

`ContextPayload.payload_body` содержит `snapshot`, `metadata`, `files`, `related_symbols`, `coverage`, `budget`. Значения `review_id`, `chunk_id` и `schema_version` представлены отдельными колонками `review_job_id`, `id`, `schema_version`.

---

## 5. Индексы

- `review_job(status, retry_at)` — выбор ReviewJob для recovery;
- `outbox_event(broker_published_at, created_at)` — выбор событий dispatcher;
- `task_lease(expires_at)` — выбор истёкших lease;
- `idempotency_record(expires_at)` — удаление истёкших записей;
- частичный уникальный индекс для `QuotaUsage(type='starts')` по `repository_id, user_id, type, window_start`;
- частичный уникальный индекс для `QuotaUsage(type='active_jobs')` по `repository_id, type`.

---

## 6. Открытые вопросы

- структура `ReviewJob.error/reason`;
- семантика `Repository.enabled`;
- место хранения GitHub `installation_id`;
- JSON schema для `RepositorySettings.rules/ignores`;
- модель хранения OAuth/access для `User`;
- необходимость `ChunkResult.schema_version` после окончательной сверки с canonical review-result schema.

---

## 7. Именование SQL

Идентификаторы SQL используют `snake_case`: `ReviewJob` → `review_job`, `review_job_id`; `RepositorySettings` → `repository_settings`.
