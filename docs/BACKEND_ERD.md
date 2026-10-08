# ERD backend — DMC-268 API (Team 2)

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
    Repository ||--o| RepositorySettings : текущие_настройки

    ChangeRequest ||--o{ ReviewJob : проверяется
    ReviewJob ||--o{ ReviewEvent : события
    ReviewJob ||--o{ ContextPayload : формирует
    ReviewJob ||--o{ ChunkResult : сохраняет_результаты
    ReviewJob ||--|| Publication : публикация
    ReviewJob ||--o{ OutboxEvent : создаёт
    ReviewJob ||--o| TaskLease : lease
    ReviewJob ||--o{ IdempotencyRecord : результат
    ReviewJob ||--o{ ReviewJob : rerun_of

    ContextPayload o|--o| ChunkResult : исходный_контекст
    ChunkResult ||--o{ Finding : находки

    Repository {
        UUID id PK
        TEXT external_id
        TEXT provider_name
        TEXT repo_name
        TEXT owner
        UUID current_settings_id FK "nullable"
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
        UUID repository_id "integrity anchor"
        UUID change_request_id "composite FK"
        UUID repository_settings_id "composite FK"
        TEXT config_digest
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
        UUID review_job_id FK "UNIQUE with id"
        INT schema_version
        JSONB payload_body
        TIMESTAMPTZ created_at
    }

    ChunkResult {
        UUID id PK
        UUID review_job_id FK
        UUID context_payload_id FK "nullable, composite FK"
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

| Объект                       | Ограничение                              |
| ---------------------------- | ---------------------------------------- |
| Repository                   | `UNIQUE(provider_name, external_id)`     |
| Repository → current settings | `0..1`; composite FK `(id, current_settings_id) → RepositorySettings(repository_id, id)` |
| RepositorySettings           | `UNIQUE(repository_id, id)` for composite references |
| Repository → ChangeRequest   | `1:N`                                    |
| ChangeRequest                | `UNIQUE(repository_id, external_number)` and `UNIQUE(repository_id, id)` |
| ChangeRequest → ReviewJob    | `1:N`                                    |
| ReviewJob repository/settings | composite FKs require ChangeRequest and frozen RepositorySettings to belong to `ReviewJob.repository_id` |
| ReviewJob → ContextPayload   | `1:N`                                    |
| ReviewJob → ChunkResult      | `1:N`; durable result ownership          |
| ContextPayload | `UNIQUE(review_job_id, id)` for composite reference |
| ContextPayload → ChunkResult | `1:0..1`; nullable `UNIQUE(context_payload_id)`, composite FK `(review_job_id, context_payload_id) → ContextPayload(review_job_id, id)`, `ON DELETE SET NULL (context_payload_id)` |
| ChunkResult → Finding        | `1:N`                                    |
| ReviewJob → Publication      | `1:1`, `UNIQUE(review_job_id)`           |
| ReviewJob → TaskLease        | `1:0..1`, `UNIQUE(review_job_id)`        |
| ReviewJob → OutboxEvent      | `1:N`                                    |
| RepositoryAccess             | `UNIQUE(repository_id, user_id)`         |
| WebhookReceipt               | `UNIQUE(provider_name, delivery_id)`     |
| IdempotencyRecord            | `UNIQUE(scope, idempotency_key)`         |

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

Именованный CHECK `ck_review_job_status_finished_at` использует
`ReviewJob.finished_at` как DB-маркер активности. Статусы `QUEUED` и
`RUNNING` требуют `finished_at IS NULL`; `COMPLETED`, `PARTIAL`, `FAILED` и
`SKIPPED` требуют `finished_at IS NOT NULL`. Terminal-переход записывает status и
`finished_at` одним SQL `UPDATE` или ORM flush: CHECK проверяется после каждого
statement, одной транзакции с двумя отдельными обновлениями недостаточно.
`stage` в этот invariant не входит.

`Repository.current_settings_id` изначально равен `NULL`. После создания первой
версии настроек составной FK разрешает назначить только настройки того же
repository. Исторический `ReviewJob.repository_settings_id` остаётся неизменным,
когда current settings переходит на новую версию. FK immediate, не deferred.
SQLAlchemy `use_alter` разрывает только DDL-цикл создания таблиц из-за двух
встречных repository/settings ссылок; данные создаются через nullable-then-update.

`ReviewJob.config_digest` — SHA-256 детерминированного JSON ровно из
`repository_settings_id`, `rules_digest`, `model_ref`, nullable `model_digest`,
`model_settings`, `prompt_version` и `prompt_digest`. Идентификаторы run/snapshot
и lifecycle/result поля исключены.

После retention cleanup `ChunkResult.context_payload_id` становится `NULL`;
долговременный путь — `ReviewJob -> ChunkResult -> Finding`. Если context указан,
составной FK `fk_chunk_result_context_payload_same_review` требует совпадения
`review_job_id` у результата и context. При удалении context PostgreSQL обнуляет
только `context_payload_id`, сохраняя владельца результата. Миграция `0003` после
`0002` добавляет это ограничение без изменения старой миграции. При наличии
несогласованных исторических строк создание FK завершается ошибкой и транзакция
миграции откатывается без исправления данных.

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
- partial `UNIQUE(change_request_id, requested_head_sha, config_digest) WHERE finished_at IS NULL`
  — не более одного незавершённого эквивалентного запуска; завершённые запуски не
  блокируют rerun;
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
