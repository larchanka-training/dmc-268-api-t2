# Архитектура backend — DMC-268 API (Team 2)

Документ фиксирует архитектуру серверной части сервиса асинхронного AI-assisted
code review. Основания: задачи второго спринта, `BACKEND_ERD.md` и структура
репозитория `dmc-268-api-t2`. UI `SYSTEM_DESIGN.md` пока ожидает синхронизации
нового lifecycle и не заменяет `openapi.yaml` как HTTP-контракт.

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
PostgreSQL / GitHub / Redis queue / LLM runtime
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
   ├─ 0002_backend_core.py
   ├─ 0003_result_context_ownership.py
   └─ 0004_contract_foundation.py

docs/
├─ BACKEND_ARCHITECTURE.md
├─ BACKEND_ERD.md
├─ PIPELINE_SPEC.md
└─ sprint2-contract-handoff.md
```

---

## 3. Основные процессы

### 3.1 GitHub session и подключение repository

`GET /api/v1/repositories` возвращает page только подключённых repository,
доступных текущему пользователю. `GET /api/v1/repositories/available` получает
у GitHub page ещё не подключённых для него repository с текущим read access.
`POST /api/v1/repositories/connect` принимает только provider `github` и
`external_id`; после проверки актуальных provider permissions backend атомарно
создаёт или переиспользует `Repository` и `RepositoryAccess`. Роль выводится
из GitHub permissions, не из тела запроса. Уникальность provider/external ID и
repository/user access защищает от параллельных дублей. Начальные настройки
создаются только для нового repository; повторное подключение не сбрасывает
существующие настройки. Новое подключение возвращает `201`, replay — `200`.

`POST /api/v1/auth/refresh` требует действующую, ещё не истёкшую JWT cookie,
active PostgreSQL `AuthSession`, allowlisted Origin и CSRF. Одна атомарная
операция продлевает DB expiry до now + 30 минут и выдаёт новую cookie с тем же
`sid` и тем же CSRF token; expired/revoked session не восстанавливается и
возвращает `401`, после чего нужен OAuth. Отдельный browser-readable refresh
token не вводится. Это согласованный HTTP-контракт, а не утверждение, что
handlers и физическая AuthSession уже реализованы.

### 3.2 Запуск ReviewJob

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

### 3.3 Worker анализа

```text
команда очереди Redis
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

Полный wire `ContextPayload` для одного вызова LLM содержит `schema_version`,
`review_id` и `chunk_id`. В БД эти значения хранятся в отдельных колонках; перед
вызовом LLM полный payload собирается из authoritative идентификаторов ReviewJob
и ContextPayload и `payload_body`. `BuiltContextPayload` несёт `schema_version`,
`review_id`, `chunk_id` и body; его `to_wire()` отклоняет envelope-поля в body
и собирает wire contract из этих полей до валидации и inference. JSON Schema проверяет
форму payload; публичные helpers `validate_ranges(payload)` и
`validate_hunks(hunks, public=False)` в `domain/contract_validation.py`
проверяют порядок диапазонов, hunk counts и точные contiguous old/new
координаты. Для HTTP diff используется `public=True` с полями
`old_lines`/`new_lines`; пути нормализуются отдельно до этих проверок.
`ChunkResult` хранит результат соответствующего вызова
и имеет долговременную ссылку на ReviewJob. Ссылка на ContextPayload nullable и использует
составной FK по `(review_job_id, context_payload_id)`: БД разрешает только context
того же ReviewJob. При удалении context `ON DELETE SET NULL (context_payload_id)`
сохраняет `review_job_id`, результат и Finding. Модель генерирует только
summary, findings и limitations по `schemas/llm-output/v1.json`; gateway должен
развернуть локальный Finding `$ref` перед передачей structured-output schema
провайдеру. Backend добавляет `schema_version`, timing и полные token counts
(если доступны) в `ReviewChunkResult`. Review/chunk IDs остаются в authoritative
контексте и сохранённых агрегатах, status — в ReviewJob; provider/model provenance
и служебные метрики не входят в model output.
Результат LLM и данные Change Request проходят validation перед сохранением и
публикацией.

### 3.4 Worker публикации

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

### 3.5 Dispatcher и recovery

Dispatcher выбирает `OutboxEvent` с `broker_published_at IS NULL`, передаёт
небольшую команду в очередь Redis и устанавливает `broker_published_at` только
после подтверждения постановки queue adapter. PostgreSQL остаётся источником
истины; recovery восстанавливает потерянную доставку по состоянию job/outbox,
lease и deadlines. Конкретный Redis worker/DLQ adapter выбирается в задаче #13.

Recovery обрабатывает expired/no lease с учётом `retry_at` и deadline-полей.

Переход ReviewJob в terminal status записывает terminal `status` и non-null
`finished_at` одним SQL `UPDATE` или ORM flush: CHECK проверяется после каждого
statement, одной транзакции с двумя отдельными обновлениями недостаточно.
DB CHECK запрещает active status с заполненным
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

PostgreSQL хранит долговременное состояние системы. Redis используется для
очереди небольших команд, dead-letter и временного кэша; он не является
хранилищем результатов или единственным источником состояния задачи.

---

## 5. Ports и внешние контракты

| Port | Контракт |
| --- | --- |
| `VcsPort` | сейчас: provider-backed cursor list текущих OPEN PR и capture snapshot; получение raw diff/кода добавляется адаптером задачи #14 |
| `ContextBuilderPort` | snapshot + исходные данные → `ContextPayload` |
| `LlmGatewayPort` | `ContextPayload` → структурированный `ReviewChunkResult` |
| `PublisherPort` | сохранённый результат ReviewJob → remote publication IDs |
| repository/UoW ports | операции хранения и управление транзакцией |

`StartReviewCommand` является application value. Use case вычисляет
`config_digest`, вызывает `ReviewJobRepositoryPort.add_start_review` и делает один
UoW commit. SQLAlchemy repository выводит repository anchor из ChangeRequest и
строит ORM-записи ReviewJob/Publication/OutboxEvent. Ни Session, ни ORM entities не
пересекают application boundary, а repository не скрывает собственный commit.
`ReviewJobRepositoryPort.get` возвращает immutable `ReviewJobSnapshot` с
идентификаторами, зафиксированными параметрами запуска и состоянием либо `None`,
если ReviewJob не найден.

Сообщение очереди содержит `schema_version`, `event_id`, `review_id`, `task_kind`, `attempt`, `trace_id`. Diff, prompt и результат LLM в сообщении broker не передаются.

Machine-readable queue и dead-letter контракты находятся в `schemas/queue/`.
Dead-letter содержит только безопасные метаданные. Replay выполняется оператором
через исходный PostgreSQL `OutboxEvent`, а не через raw dead message; полная
семантика определена в `PIPELINE_SPEC.md`.

---

## 6. Граница API

HTTP-маршруты и DTO определяются `openapi.yaml`; lifecycle, retry и доставка
задаются [`PIPELINE_SPEC.md`](./PIPELINE_SPEC.md). UI `SYSTEM_DESIGN.md` пока
содержит прежние `RUNNING + stage` и требует синхронизации с status-only
контрактом, прежде чем его можно использовать как источник текущих API DTO.
GitHub-only scope текущего спринта подтверждён пользователем 7 октября;
GitLab остаётся последующей итерацией. Backend-mediated session refresh и
явное подключение repository являются частью текущего OpenAPI, но их HTTP
handlers ещё не реализованы.

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
5. `broker_published_at` устанавливается после подтверждения постановки в Redis
   queue adapter; recovery не считает этот маркер доказательством завершения работы.
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
- миграции Alembic `0002_backend_core` и `0003_result_context_ownership` после baseline;
- контракты ports для VCS, Context Builder, LLM Gateway, Publisher и Unit of Work;
- создание начального состояния `ReviewJob`;
- SQLAlchemy repository/UoW для атомарного начального агрегата без зависимости application слоя от ORM;
- same-repository composite FK, внешний repository identity, active-run index и status/finished-at CHECK;
- независимый retention ChunkResult/Finding через прямую связь с ReviewJob и
  составной FK, запрещающий context другого ReviewJob;
- тесты метаданных и интеграционные тесты PostgreSQL для ограничений схемы;
- CI с PostgreSQL 18, `alembic check` и циклом downgrade/upgrade на отдельной
  тестовой базе.

Миграция `0003` проверяет существующие пары ChunkResult/ContextPayload при
создании составного FK. Если данные уже имеют разных владельцев, PostgreSQL
отклоняет миграцию целиком; автоматического исправления таких строк нет.

В разработке:

- остальные реализации repositories и Unit of Work;
- HTTP-обработчики и DTO;
- GitHub/VCS adapter;
- Context Builder;
- LLM Gateway и validation canonical schema;
- Redis queue dispatcher и DLQ adapter по согласованному контракту #13;
- workers анализа, публикации и recovery;
- Publisher reconciliation;
- интеграционные тесты Redis queue adapter и recovery;
- readiness endpoint;
- модель хранения OAuth/access и HTTP handlers для connection flow;
- обязательная accepted auth-модель включает 30-минутную JWT cookie с `sid`,
  authoritative PostgreSQL `AuthSession` и refresh только действующей сессии
  с Origin/CSRF; полная реализация auth отложена;
- provider-specific данные установки;
- HTTP handlers, DTO и session/CSRF/Origin middleware.

Новые версии `RepositorySettings.rules` имеют форму `{"instructions": [...]}`,
где элементы — уникальные непустые строки, а порядок значим. `ignores` имеет
форму `{"globs": [...]}` с уникальными repository-relative normalized POSIX
patterns. Patterns являются данными и никогда не передаются shell.
`rules_digest` вычисляется backend при создании новой версии; клиент его не
задаёт. Миграция `0004` не переписывает существующие immutable settings,
digests, язык или frozen job. Legacy пустые `{}` отображаются на чтении как
пустые canonical rules/ignores, но сохранённый язык версии возвращается как
есть; digest не пересчитывается. Иные noncanonical legacy settings дают
`GET /settings` ответ `409 LEGACY_SETTINGS_UNSUPPORTED` с opaque ETag, без raw
JSON. Новый start с такой текущей версией даёт `409` до quota/outbox writes;
ранее принятый ReviewJob и idempotency replay сохраняют frozen версию и digest.
Admin может отправить полный canonical PUT с `If-Match`: атомарное сравнение
текущей версии и переключение указателя создаёт новую `ru`-версию без наследования
неподдерживаемых полей. До развёртывания нового worker неподдерживаемые pending
runs нужно завершить прежним совместимым обработчиком или обеспечить
совместимость worker; миграция frozen конфигурацию не переписывает.
