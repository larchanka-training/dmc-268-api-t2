# Архитектура backend — DMC-268 API (Team 2)

Документ фиксирует архитектуру серверной части сервиса асинхронного AI-assisted code review. Основания: актуальный `SYSTEM_DESIGN.md`, `BACKEND_ERD.md` и структура репозитория `dmc-268-api-t2`.

---

## 1. Архитектурная модель

Backend представляет собой единый кодовый проект с несколькими типами процессов: HTTP API, dispatcher и фоновые worker-процессы.

Направление зависимостей:

```text
HTTP-обработчики / worker-обработчики
        ↓
Use cases приложения
        ↓
Доменные контракты / ports
        ↓
Adapters / хранение данных
        ↓
PostgreSQL / GitHub / RabbitMQ / Redis / LLM runtime
```

Распределение ответственности:

| Область | Ответственность |
| --- | --- |
| `api` | маршруты FastAPI, HTTP DTO, аутентификация и авторизация на границе HTTP |
| `application` | координация use cases и транзакционные границы |
| `domain` | значения lifecycle и provider-neutral контракты |
| `db` | ORM-модели SQLAlchemy, сессия БД и работа с PostgreSQL |
| adapters | интеграция с VCS, LLM, Publisher и broker |
| workers | анализ, публикация, dispatcher и recovery |
| `core` | конфигурация, журналирование, middleware и общая инфраструктура приложения |

Ports используются на внешних границах и на границе хранения данных: VCS, Context Builder, LLM Gateway, Publisher, repositories и Unit of Work.

---

## 2. Структура модулей

Существующие модули `api`, `core` и `db` сохраняются. Предметные модули размещаются рядом с ними и используют существующую инициализацию приложения, конфигурацию и сессию БД.

```text
src/app/
├─ api/                           # маршруты FastAPI
├─ core/                          # конфигурация, журналирование, middleware
├─ db/
│  ├─ base.py                     # общий SQLAlchemy Base
│  ├─ session.py                  # асинхронный движок SQLAlchemy и сессия БД
│  ├─ models.py                   # ORM-модели предметной схемы
│  ├─ repositories.py             # SQLAlchemy persistence adapter
│  └─ uow.py                      # граница commit/rollback
├─ domain/
│  └─ enums.py                    # значения lifecycle и domain enum
├─ application/
│  ├─ ports.py                    # контракты внешних границ и хранения данных
│  └─ use_cases/
│     └─ start_review.py          # создание начального состояния ReviewJob
├─ workers/                       # dispatcher/analysis/publication/recovery; в разработке
└─ main.py                        # инициализация FastAPI

migrations/
└─ versions/
   ├─ 0001_baseline.py
   └─ 0002_backend_core.py

docs/
├─ BACKEND_ARCHITECTURE.md
├─ BACKEND_ERD.md
└─ PIPELINE_SPEC.md
```

---

## 3. Основные процессы

### 3.1 Запуск ReviewJob

```text
HTTP-команда
 ↓
аутентификация/авторизация + RepositoryAccess
 ↓
Idempotency-Key
 ↓
резервирование квоты
 ↓
ChangeRequest + текущий RepositorySettings
 ↓
config_digest + проверка active-run unique index
 ↓
ReviewJob + Publication(NOT_READY) + OutboxEvent(analyze)
 ↓
COMMIT
```

При создании `ReviewJob` repository adapter выводит `repository_id` из выбранного
`ChangeRequest`, а составные FK гарантируют принадлежность ChangeRequest и
зафиксированной версии RepositorySettings одному repository. В `ReviewJob`
фиксируются `requested_head_sha`, `repository_settings_id`, `rules_digest`,
идентификаторы prompt и модели. Чистая application-функция заранее вычисляет
`config_digest` как SHA-256 детерминированного UTF-8 JSON этих frozen config полей:
UUID сериализуется строкой, nullable model digest остаётся JSON `null`, ключи
сортируются, separators компактны. Run/snapshot/lifecycle/result поля не входят в
digest.

Незавершённый запуск определяется `finished_at IS NULL`. Частичный уникальный
индекс по ChangeRequest, requested head и `config_digest` атомарно запрещает второй
эквивалентный запуск. DB adapter создаёт ReviewJob, Publication и OutboxEvent в
одной session, но не делает commit; commit/rollback принадлежит application UoW.

### 3.2 Worker анализа

```text
команда RabbitMQ
 ↓
TaskLease + fencing_token
 ↓
ReviewJob: FETCHING_DIFF
 ↓
проверка snapshot через VCS
 ↓
получение diff и кода
 ↓
Context Builder
 ↓
ContextPayload[]
 ↓
LLM Gateway
 ↓
ChunkResult[]
 ↓
validation внутри LLM_PROCESSING + дедупликация
 ↓
Finding[] + final_summary + coverage
 ↓
конечный status ReviewJob
 ↓
OutboxEvent(publish), если результат допускает публикацию
```

`ContextPayload` содержит canonical payload одного вызова LLM с указанием
`schema_version`. `ChunkResult` хранит результат соответствующего вызова и имеет
долговременную ссылку на ReviewJob. Ссылка на ContextPayload nullable и использует
`ON DELETE SET NULL`, поэтому context можно удалить раньше результата и Finding.
Будущий persistence path обязан выводить `ChunkResult.review_job_id` из owning
ContextPayload/review. Результат LLM и данные Change Request проходят validation
перед сохранением и публикацией.

### 3.3 Worker публикации

```text
Publication: PENDING
 ↓
проверка состояния PR / SHA / доступа
 ↓
Publisher
 ↓
remote review + inline comments
 ↓
provider_metadata
 ↓
PUBLISHED | PARTIAL | FAILED | UNKNOWN | SKIPPED
```

Lifecycle анализа и lifecycle публикации независимы. Повторная попытка публикации не запускает inference LLM повторно.

### 3.4 Dispatcher и recovery

Dispatcher выбирает `OutboxEvent` с `broker_published_at IS NULL`, передаёт queue command в broker и после publisher confirm устанавливает `broker_published_at`.

Recovery обрабатывает expired/no lease с учётом `retry_at` и deadline-полей.

Переход ReviewJob в terminal status атомарно записывает тот же terminal `status`
и non-null `finished_at`. DB CHECK запрещает active status с заполненным
`finished_at` и terminal status с `finished_at IS NULL`. Единственное текущее
состояние ReviewJob хранится в `status`: `QUEUED`, `FETCHING_DIFF`,
`PARSING_CONTEXT`, `LLM_PROCESSING`, `COMPLETED`, `PARTIAL`, `FAILED` или
`SKIPPED`. Отдельного current/public `stage` нет.

Временные ограничения:

- очередь: до 15 минут;
- анализ: до 10 минут, включая retry;
- цикл публикации: до 5 минут.

---

## 4. Модель хранения данных

Физическая модель приведена в [`BACKEND_ERD.md`](./BACKEND_ERD.md).

Основные сущности:

| Сущность | Назначение |
| --- | --- |
| `Repository` | подключённый внешний repository; внешний identity уникален по provider+external ID |
| `RepositorySettings` | неизменяемая версия настроек repository |
| `ChangeRequest` | текущее provider-neutral состояние PR/MR |
| `ReviewJob` | отдельный запуск анализа для зафиксированного состояния |
| `ReviewEvent` | append-only история переходов, retry, degradation и failure с безопасными деталями |
| `ContextPayload` | canonical payload одного вызова LLM |
| `ChunkResult` | результат одного вызова LLM, сохраняемый по ReviewJob независимо от retention контекста |
| `Finding` | проверенная находка с координатами и рекомендацией |
| `Publication` | состояние публикации результата во внешний VCS |
| `OutboxEvent` | команда очереди для transactional outbox |
| `TaskLease` | текущий lease worker с fencing token |
| `RepositoryAccess` | локальная роль пользователя в repository |
| `QuotaUsage` | состояние технических квот |
| `WebhookReceipt` | дедупликация webhook delivery |
| `IdempotencyRecord` | идемпотентность ручных API-команд |

PostgreSQL хранит долговременное состояние системы. RabbitMQ передаёт небольшие команды и идентификаторы. Redis используется для временного состояния и кэширования.

---

## 5. Ports и внешние контракты

| Port | Контракт |
| --- | --- |
| `VcsPort` | provider-backed cursor list текущих OPEN PR, получение snapshot, diff и кода |
| `ContextBuilderPort` | snapshot + исходные данные → `ContextPayload` |
| `LlmGatewayPort` | `ContextPayload` → структурированный `ReviewChunkResult` |
| `PublisherPort` | сохранённый результат ReviewJob → remote publication IDs |
| repository/UoW ports | операции хранения и управление транзакцией |

`StartReviewCommand` является application value. Use case вычисляет
`config_digest`, вызывает `ReviewJobRepositoryPort.add_start_review` и делает один
UoW commit. SQLAlchemy repository выводит repository anchor из ChangeRequest и
строит ORM-записи ReviewJob/Publication/OutboxEvent. Ни Session, ни ORM entities не
пересекают application boundary, а repository не скрывает собственный commit.

Сообщение очереди содержит `schema_version`, `event_id`, `review_id`, `task_kind`, `attempt`, `trace_id`. Diff, prompt и результат LLM в сообщении broker не передаются.

Machine-readable queue и dead-letter контракты находятся в `schemas/queue/`.
Dead-letter содержит только безопасные метаданные. Replay выполняется оператором
через исходный PostgreSQL `OutboxEvent`, а не через raw dead message; полная
семантика определена в `PIPELINE_SPEC.md`.

---

## 6. Граница API

HTTP-маршруты и DTO должны соответствовать canonical API contract из `SYSTEM_DESIGN.md`.

Требования:

- ручной запуск ReviewJob использует scoped `Idempotency-Key`;
- API возвращает единый `status` и результат без raw response LLM provider;
- события ReviewJob доступны через отдельный read use case;
- `GET /healthcheck` остаётся dependency-free liveness endpoint;
- readiness проверяет зависимости через отдельный endpoint;
- корреляция HTTP-запросов использует `Request ID` и `X-Request-Id`.

ORM-модели не используются как публичные HTTP DTO.

---

## 7. Транзакционные границы

1. Создание `ReviewJob`, `Publication(NOT_READY)`, `OutboxEvent(analyze)` и резервирование квоты выполняются в одной UoW-транзакции; нарушение active-run uniqueness откатывает агрегат целиком.
2. Snapshot, изменение `status` и соответствующий `ReviewEvent` сохраняются согласованно.
3. `ContextPayload`, `ChunkResult` и проверенные `Finding` сохраняются только при актуальном lease и fencing token; ChunkResult получает `review_job_id` от owning context/review.
4. Конечное состояние анализа и `OutboxEvent(publish)` фиксируются в одной транзакции.
5. `broker_published_at` устанавливается после publisher confirm.
6. Вызовы внешних API выполняются вне открытой транзакции PostgreSQL.

---

## 8. Состояние реализации

Реализовано:

- ORM-модели SQLAlchemy 2 для сущностей ERD;
- PostgreSQL UUID, JSONB, FK, UNIQUE и CHECK constraints;
- индексы для recovery, outbox dispatcher, истечения lease и очистки `IdempotencyRecord`;
- поля transactional outbox;
- хранение `TaskLease` и fencing token;
- ограничения для `QuotaUsage`;
- перечисления domain-слоя;
- предметная миграция Alembic после baseline;
- контракты ports для VCS, Context Builder, LLM Gateway, Publisher и Unit of Work;
- создание начального состояния `ReviewJob`;
- SQLAlchemy repository/UoW для атомарного начального агрегата без зависимости application слоя от ORM;
- same-repository composite FK, внешний repository identity, active-run index и status/finished-at CHECK;
- независимый retention ChunkResult/Finding через прямую связь с ReviewJob;
- тесты метаданных для ключевых ограничений схемы.

В разработке:

- остальные реализации repositories и Unit of Work;
- HTTP-обработчики и DTO;
- GitHub/VCS adapter;
- Context Builder;
- LLM Gateway и validation canonical schema;
- RabbitMQ dispatcher;
- workers анализа, публикации и recovery;
- Publisher reconciliation;
- интеграционные тесты с PostgreSQL и RabbitMQ;
- readiness endpoint;
- модель хранения OAuth/access;
- обязательная accepted auth-модель включает 30-минутную JWT cookie с `sid` и
  authoritative PostgreSQL `AuthSession`; полная реализация auth отложена;
- provider-specific данные установки;
- HTTP handlers, DTO и session/CSRF/Origin middleware.

`RepositorySettings.rules` имеет форму `{"instructions": [...]}`, где элементы —
уникальные непустые строки, а порядок значим. `ignores` имеет форму
`{"globs": [...]}` с уникальными repository-relative normalized POSIX patterns.
Patterns являются данными и никогда не передаются shell. `rules_digest`
вычисляется backend из canonical serialization; клиент его не задаёт.
