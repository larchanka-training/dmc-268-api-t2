# Архитектура backend — DMC-268 API (Team 2)

Документ фиксирует архитектуру серверной части сервиса асинхронного AI-assisted
code review. Источники контрактов: `openapi.yaml`, `PIPELINE_SPEC.md`,
`BACKEND_ERD.md` и структура репозитория `dmc-268-api-t2`.
UI `SYSTEM_DESIGN.md` содержит прежний lifecycle
и не заменяет `openapi.yaml` как HTTP-контракт.

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
│  ├─ task_leases.py              # analyze/publish lease и transactional fencing
│  ├─ webhook_inbox.py            # durable GitHub intake
│  └─ uow.py                      # граница commit/rollback
├─ domain/
│  └─ enums.py                    # значения lifecycle и domain enum
├─ application/
│  ├─ ports.py                    # контракты внешних границ и хранения данных
│  ├─ github_ports.py             # контракт SHA-pinned GitHub capture
│  ├─ webhook_ports.py            # контракт durable inbox
│  └─ use_cases/
│     ├─ start_review.py          # создание начального состояния ReviewJob
│     └─ process_webhook.py       # Level 1 artifact без ReviewJob
├─ vcs/
│  ├─ github.py                   # GitHub REST snapshot
│  └─ diff.py                     # bounded unified diff parser
├─ adapters/
│  ├─ redis_queue.py              # Redis queue и safe dead-letter quarantine
│  ├─ outbox_dispatcher.py        # bounded PostgreSQL outbox dispatch
│  ├─ review_worker.py            # bounded analyze/publish orchestration
│  └─ analysis_handler.py         # анализ и атомарный publication handoff
├─ outbox_dispatcher.py            # CLI bounded batch / continuous polling
├─ webhook_worker.py              # отдельный recovery intake #14
└─ main.py                        # инициализация FastAPI

migrations/
└─ versions/
   ├─ 0001_baseline.py
   ├─ 0002_backend_core.py
   ├─ 0003_result_context_ownership.py
   ├─ 0004_contract_foundation.py
   ├─ 0005_github_webhook_inbox.py
   ├─ 0006_task_lease_kind.py
   └─ 0007_outbox_quarantine.py

docs/
├─ BACKEND_ARCHITECTURE.md
├─ BACKEND_ERD.md
├─ PIPELINE_SPEC.md
├─ github-webhook-intake.md
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

`DELETE /api/v1/repositories/{id}/connection` отключает repository персонально:
после проверки session, Origin и CSRF удаляет только `RepositoryAccess` текущего
пользователя и делает commit в уже открытой session-транзакции. GitHub не
вызывается; `Repository`, настройки, jobs, findings, публикации, credentials и
доступ других пользователей сохраняются. Принятые задачи продолжают работу.
Любой корректный UUID возвращает пустой `204`, даже если repository или связь
не существуют; некорректный UUID даёт `404`. Ошибка БД или обязательной
конфигурации даёт canonical `503`; при сбое соединения после commit результат
может быть неизвестен, повторный DELETE безопасен. Повторный Connect проверяет
актуальные GitHub permissions, назначает текущую роль и переиспользует прежний
Repository ID, настройки и историю. Пересекающиеся Connect/Disconnect допускают
любой последовательный порядок без новых механизмов locking.

`POST /api/v1/auth/refresh` требует действующую, ещё не истёкшую JWT cookie,
active PostgreSQL `AuthSession`, allowlisted Origin и CSRF. Одна атомарная
операция продлевает DB expiry до now + 30 минут и выдаёт новую cookie с тем же
`sid` и тем же CSRF token; expired/revoked session не восстанавливается и
возвращает `401`, после чего нужен OAuth. Отдельный browser-readable refresh
token не вводится. Это согласованный HTTP-контракт; handlers и физическая
AuthSession не реализованы.

### 3.2 GitHub webhook intake

Подписанный `POST /api/v1/webhooks/github` принимает `opened`/`synchronize`
только для подключённого GitHub repository и дедуплицирует delivery в
PostgreSQL `WebhookReceipt`. Отдельный worker снимает SHA-pinned GitHub
snapshot и сохраняет raw diff и Level 1 artifact; этот путь **не создаёт
ReviewJob** и не собирает полный LLM `ContextPayload`. Пустой/полностью
отфильтрованный diff допустим как Level 1 artifact. Retry, lease и локальная
проверка описаны в [`github-webhook-intake.md`](./github-webhook-intake.md).
Redis review worker не использует WebhookReceipt как неявный источник входных
данных. LLM Gateway реализован в `app.llm` / `app.review` и подключён к bounded
review worker через production-capable composition; CLI `app.analysis_worker`
запускает её с обязательным trusted preflight (Stage 8C-04-01).

### 3.3 Запуск ReviewJob

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

`POST /api/v1/repositories/{id}/pull-requests/{number}/reviews` реализован: scoped
idempotency, проверка доступа и текущего GitHub snapshot, frozen configuration
и атомарные quota/job/publication/outbox writes. Реальная session/Origin/CSRF
проверка и production dependencies для provider authorization и configuration
ещё не подключены; по умолчанию обработчик закрыт для реальных пользователей.

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

### 3.4 Worker анализа

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
summary, findings и limitations: historical prompt v1/v2 используют
`schemas/llm-output/v1.json`, rules-aware v3 — `schemas/llm-output/v2.json`.
Gateway выбирает проверенную schema identity и должен
развернуть локальный Finding `$ref` перед передачей structured-output schema
провайдеру. Backend добавляет `schema_version`, timing и полные token counts
(если доступны) в `ReviewChunkResult`. Review/chunk IDs остаются в authoritative
контексте и сохранённых агрегатах, status — в ReviewJob; provider/model provenance
и служебные метрики не входят в model output.
Результат LLM и данные Change Request проходят validation перед сохранением и
публикацией.

### 3.5 Worker публикации

```text
Publication: PENDING
 ↓
TaskLease(publish) + новый fencing_token
 ↓
сериализация публикаций по ChangeRequest
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

Одна таблица TaskLease обслуживает analyze и publish, сохраняя единственный lease
на ReviewJob и обязательный task_kind с CHECK. Миграция 0006 после 0005 заполняет
старые lease как analyze, сохраняя owner, expiry и fencing_token. Analyze
захватывает только активный ReviewJob. Publish захватывает
COMPLETED/PARTIAL при Publication(PENDING) и неистёкшем publication deadline;
terminal status и finished_at ReviewJob остаются неизменными. Назначение lease
должно проверяться по authoritative данным вместе с owner/token/expiry: смена
lifecycle не превращает существующий analysis lease в publish lease.

Записи Publication, включая подготовку PENDING анализатором и результат работы
publisher, проверяют соответствующий fencing в той же UoW-транзакции, что и запись.
Renew разрешён только пока задача своего типа остаётся допустимой; release после
завершения остаётся доступен текущему владельцу живого lease. UNKNOWN требует
отдельной reconciliation и исключает автоматическую повторную публикацию.
Подробный контракт задан в PIPELINE_SPEC.md §6 и §8.

Разные ReviewJob одного ChangeRequest имеют разные lease. Поэтому сериализация
provider-вызовов для одного PR нужна отдельно от TaskLease; транзакционные locks
на ReviewJob не заменяют её и не удерживаются во время внешнего I/O.

Реализован bounded `PostgresReviewWorker.run_once()` с
обязательным `TaskHandlerPort`: одна Redis delivery, authoritative identity,
task-specific acquire, повторная проверка перед callback, fenced release и ACK
после проверенного durable outcome. CLI `app.analysis_worker` выполняет один
new-delivery pass (`--once`) либо последовательный continuous loop.
Обычный return, промежуточный commit и захват lease не разрешают
ACK. Консервативные completion proofs и pending-классификация определены в
PIPELINE_SPEC.md §6; PostgreSQL outbox marker может оставаться NULL при delivery.
Тестовый harness фиксирует реальные fenced terminal решения и атомарный publish
handoff без внешних provider calls. Во время callback worker автоматически
продлевает TaskLease через `TaskLeasePort`: heartbeat каждые 15 секунд,
lease на 90 секунд. Потеря lease или ошибка renewal отменяет handler и оставляет
delivery pending. Continuous analysis CLI выполняет bounded pending-delivery
recovery через XAUTOCLAIM (Stage 8C-04-02); это отдельный путь от PostgreSQL
transport recovery в dispatcher (§3.6).

`PostgresAnalysisHandler` подключается через тот же `TaskHandlerPort`:
authoritative ReviewJob/configuration → `GithubPort.fetch` → `parse_diff` →
canonical Level 1 ContextPayload → injected `LlmGatewayPort.review` → fenced
PostgreSQL result и atomic publication handoff. ACK/lease orchestration не
дублируется. Call-start guards проверяют authority/deadlines; внешние вызовы
проходят вне DB transactions.

Stage 8B-05 предоставляет internal builder `build_postgres_analysis_worker`:
`PostgresReviewWorker → PostgresAnalysisHandler → FrozenAnalysisExecutionFactory
→ LlmGatewayAdapter / ReviewService → LLMGateway → PostgresReviewRunBudget`.
Factory получает authoritative ReviewJob под existing locks, TaskLease,
persisted analysis attempt и execution-local authority. Frozen reader проверяет
saved config/rules/settings/prompt digests и deployment artifacts; context limits
берутся из сохранённой policy. Runtime предоставляет HTTP transport, credentials
и разрешённые provider profiles, без замены frozen модели/routing/settings.
Unsupported history/profile/model pin или непроверенный digest дают безопасный
terminal отказ до HTTP. Созданный per-execution budget всегда PostgreSQL-backed;
RunBudget не является параметром или default этой composition.

Reservation каждого HTTP и actual usage settlement имеют собственные committed
транзакции `review_llm_attempt`. Result rollback/crash не возвращает списанные
затраты, новая factory/service/Gateway не обнуляют общий ledger. Retry/repair,
accepted deadline и analysis attempts durable; heartbeat/watchdog отменяют
execution при потере rolling lease. Проверки дают DB-linearized admission и
local cancellation, без обещания exactly-once billing или absolute wire fencing.
Unknown usage остаётся charged; estimates не являются доказанным billing cap.

Точный safe error_code budget/auth/config сохраняется с canonical public reason.
Нет пригодного проверенного результата — FAILED; сохранённый canonical результат
при terminal failure — PARTIAL с limitation и existing atomic publication handoff.
Raw/invalid LLM output не сохраняется как результат. Technical fallback provenance
не делает полноценный результат PARTIAL. Terminal status и finished_at меняются
одним UPDATE; результат, findings, Publication(PENDING) и OutboxEvent(publish)
фиксируются в одной транзакции. ACK требует authoritative completion proof.

Real PostgreSQL 18/Redis 8 integration tests проверяют production composition
in-process с GitHub/LLM HTTP MockTransport, terminal/retryable failure, duplicate
delivery, lease loss, settlement → result rollback/crash и terminal commit →
failed ACK. Migration tests проходят 0001…0010 и head/check/downgrade0009/upgrade/check
с сохранением legacy identities/settings и CHECK constraints. NULL frozen marker
не получает новую policy или нулевой budget: historical_budget_unknown, zero LLM
HTTP, fenced FAILED либо PARTIAL при пригодном сохранённом результате. Historical
terminal rows остаются читаемыми через REST без переписывания digests.

Builder лишь возвращает bounded worker; фонового сервиса он не запускает.
CLI Stage 8C-04-01 описан в [analysis-runtime.md](examples/analysis-runtime.md).
Activation, transport retention/cleanup, полный in-flight shutdown,
полный chunk planner, real publisher/reconciliation и OAuth/session integration
остаются до production activation. Automatic Redis pending recovery и PostgreSQL transport recovery реализованы;
их наличие не заменяет остальные activation gates. Issue #13 и deployment readiness не объявлены
завершёнными. Детали — PIPELINE_SPEC.md §2–3, §6 и §8.

### 3.6 Dispatcher и recovery

Dispatcher отправляет только canonical analyze `OutboxEvent`; publish commands и
Publication(PENDING) остаются durable в PostgreSQL без publish XADD. Initial batch
выбирает максимум 100 ещё не резервированных команд. Recovery рассматривает до
100 due active кандидатов и делает до 10 resend reservations за tick. Quarantine,
live TaskLease и future retry исключаются до LIMIT; истёкший queue/analysis deadline
позволяет доставку для terminalization существующим worker без нового HTTP.

Сохраняются `broker_message_id`, `broker_published_at` и `dispatch_not_before`.
Перед одной XADD command attempt коммитится DB now + 90 секунд; hidden retries нет.
Последний подтверждённый ID/marker сохраняется до подтверждения новой отправки.
Crash до XADD и потерянный ответ/marker commit соблюдают persisted cooldown после
restart; ambiguous send допускает bounded duplicates одной canonical PG команды.
Generation/CAS, recovery counters/windows и отдельный terminal diagnostics scan удалены.
Eligibility повторно проверяется под locks `ReviewJob → TaskLease → OutboxEvent`
по DB clock после locks. Redis probe/XADD не удерживают PostgreSQL transaction.

Exact Redis ID probe различает PRESENT, ABSENT, EXPIRED, UNKNOWN и UNTRUSTED.
Canonical PRESENT откладывает следующую проверку без resend; ABSENT/EXPIRED допускают
повторную eligibility check и committed throttle перед одной XADD. UNKNOWN —
infrastructure failure с sanitized nonzero exit до recovery success log, включая
transient Redis failure. UNTRUSTED даёт safe diagnostics и persisted deferral,
без XADD, ACK и изменения canonical identity. Persisted order/deferral обеспечивают
fairness due backlog без process-local dispatcher cursor и горячего повторного scan.
Incomplete handoff остаётся pending без ACK. Автоматического structural-gap alert
больше нет; проверка сохранённых Publication/outbox данных — ответственность оператора.

Transport expiry 20 минут определяется Redis Stream ID и Redis TIME. Оба пути
receive/reclaim проходят один worker processing seam: expired delivery без durable
completion proof не запускает handler/LLM HTTP и не ACK-ается. Завершённый duplicate
можно ACK-нуть по прежнему proof; PostgreSQL recovery создаёт свежую delivery для
active job, в том числе для deadline terminalization. Job clocks, fencing и durable
paid accounting от transport recovery не сбрасываются.

Операционная предпосылка — **ровно один активный dispatcher**. Deploy/restart:
остановить прежний процесс, дождаться выхода, затем запустить новый; после потери
host подтвердить остановку прежнего процесса. Rolling overlap и active replicas
не поддерживаются. Distributed leader election не добавляется. Нарушение singleton
может увеличить duplicates; обязательные worker fencing/accounting сохраняются.

CLI `python -m app.outbox_dispatcher --once` выполняет recovery sweep и initial batch
без ожидания. Continuous mode делает recovery при старте, затем через 30 секунд
после завершения предыдущего sweep по monotonic clock. Recovery и initial dispatch
последовательны без catch-up, также при initial backlog и пустой очереди. Idle wait
ограничен ближайшим tick; `--poll-interval` default 5s, finite и минимум 0.01s.
`--batch-size` default 100, positive и capped at 100. SIGTERM/SIGINT запрещают новый
tick; stop во время recovery запрещает следующий initial batch. Начатая bounded
операция завершается, ожидание прерывается, Redis/engine освобождаются.
Redis PING перед каждой итерацией, connect/command timeouts 5s, retries отключены;
ошибки Redis/PostgreSQL дают nonzero exit с error type без raw payload/exception text.
Оператор оставляет время завершить bounded batch; forced termination после XADD
может дать duplicate. Pending recovery analysis worker claims существующие deliveries;
PostgreSQL dispatcher восстанавливает потерянный transport через XADD. Автоматического
старта в Compose/CD нет; retention/cleanup и полный analysis shutdown остаются отдельно.

Forward migration `0012_lean_outbox_transport.py` следует за неизменённой `0011`:
удаляет только три obsolete fields и связанные CHECK, сохраняя receipts, throttle,
quarantine и business data. До upgrade необходимо drain/stop старых worker/API/dispatcher
процессов, использующих ORM OutboxEvent: старый binary обращается к удаляемым columns.
После migration запускается новый binary; rolling mixed versions не поддерживаются.
Rollback тоже требует остановки процессов; downgrade восстанавливает counters нулями,
но их историю восстановить нельзя.
Конфликт PR #30 revision IDs 0008/0009 требует согласованной integration history
до merge; migrations другого PR не перенумеровываются в LEAN-01.

До merge техлид согласует изменение обещаний: singleton вместо competing generations,
90s throttle вместо rolling window 3/event/300s и удаление terminal diagnostics scan.
Exact probe, Transactional Outbox, at-least-once, safe DLQ/ACK, fencing, durable LLM
accounting, canonical identity и atomic publication handoff сохраняются.
Подробнее — PIPELINE_SPEC.md §4–6 и §10.

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

### 3.7 Чтение результатов ревью

`GET /api/v1/reviews/{id}` и `GET /api/v1/reviews/{id}/findings` читают
сохранённые результаты из PostgreSQL согласно `openapi.yaml`. Detail выводит
status `ReviewJob`, отдельно status `Publication`, сохранённый snapshot,
coverage и подтверждённые token counts. Detail использует единый PostgreSQL
snapshot `REPEATABLE READ` при конкурентной записи результата. Progress считает
сохранённые contexts
и results; завершённый result с удалённым context сохраняет известный planned
chunk. Неизвестный исторический reason code и недоступные nullable данные
проецируются в null. Raw context, prompts, provider metadata и lease не выдаются.

Findings используют bounded SQL keyset pagination по `(created_at, id)`, default
20, максимум 100, opaque cursor с проверкой review и anchor. Пока отсутствует
согласованная сохранённая связь Finding с provider reference,
`publication_reference` всегда null.

Типизированная dependency `get_authenticated_principal` по умолчанию возвращает
canonical 401. Реальная проверка cookie и PostgreSQL session ещё не подключена:
эти endpoints пока недоступны реальным пользователям. Test principal задаётся
только через FastAPI dependency override. После principal обязательно проверяется
`RepositoryAccess`; отсутствующий review и отсутствие доступа возвращают одинаковый
canonical 404. Production bypass и чтение user ID из запроса отсутствуют.

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
| `WebhookReceipt` | durable GitHub delivery intake, дедупликация, lease/retry и Level 1 artifact |
| `IdempotencyRecord` | идемпотентность ручных API-команд |

PostgreSQL хранит долговременное состояние системы. Redis используется для
очереди небольших команд, dead-letter и временного кэша; он не является
хранилищем результатов или единственным источником состояния задачи.

---

## 5. Ports и внешние контракты

| Port | Контракт |
| --- | --- |
| `VcsPort` | provider-neutral список OPEN PR и capture snapshot для авторизованного ReviewJob; интеграция с worker остаётся отдельной |
| `GithubPort` / `WebhookInbox` | SHA-pinned GitHub capture и durable intake #14, отдельно от авторизованного ReviewJob |
| `ContextBuilderPort` | snapshot + исходные данные → `ContextPayload` |
| `LlmGatewayPort` | `ContextPayload` → структурированный `ReviewChunkResult` |
| `PublisherPort` | сохранённый результат ReviewJob → remote publication IDs |
| `TaskLeasePort` | явный analyze/publish acquire, heartbeat и release; authoritative тип хранится в PostgreSQL |
| `PublicationRepositoryPort` | get snapshot, analysis-fenced prepare и publish-fenced update в существующей UoW |
| repository/UoW ports | операции хранения и управление транзакцией |

`StartReviewCommand` является application value. Use case вычисляет
`config_digest`, вызывает `ReviewJobRepositoryPort.add_start_review` и делает один
UoW commit. SQLAlchemy repository выводит repository anchor из ChangeRequest и
строит ORM-записи ReviewJob/Publication/OutboxEvent. Ни Session, ни ORM entities не
пересекают application boundary, а repository не скрывает собственный commit.
`ReviewJobRepositoryPort.get` возвращает immutable `ReviewJobSnapshot` с
идентификаторами, зафиксированными параметрами запуска и состоянием либо `None`,
если ReviewJob не найден.

`UnitOfWorkPort.publications` использует ту же session и commit/rollback, что и
review_jobs. `prepare(analyze_claim)` переводит только NOT_READY в PENDING после
COMPLETED/PARTIAL и задаёт фиксированный deadline. `update(publish_claim, status,
provider_metadata)` записывает progress/outcome только из допустимого PENDING,
не меняя terminal ReviewJob и deadline. Deadline ограничивает новые provider
attempts и PENDING/progress. Terminal outcome ранее начатой попытки можно
зафиксировать после deadline под тем же ещё живым publish lease; SQL UPDATE
по-прежнему проверяет сохранённый kind, owner, token и expiry. started_at — начало
цикла. Проверка начала каждой внешней попытки до deadline — требование к
publisher worker; этот обработчик не реализован.
Acquire/renew после deadline запрещены. Отказ возвращается как LeaseLostError;
обычные методы publisher не выполняют reconciliation UNKNOWN. Completion/outbox
orchestration остаётся задачей worker: terminal write, prepare и publish event
должны войти в одну UoW-транзакцию.

Сообщение очереди содержит `schema_version`, `event_id`, `review_id`, `task_kind`, `attempt`, `trace_id`. Diff, prompt и результат LLM в сообщении broker не передаются.

Machine-readable queue и dead-letter контракты находятся в `schemas/queue/`.
Dead-letter содержит только безопасные метаданные. Replay выполняется оператором
через исходный PostgreSQL `OutboxEvent`, а не через raw dead message; полная
семантика определена в `PIPELINE_SPEC.md`.

---

## 6. Граница API

HTTP-маршруты и DTO определяются `openapi.yaml`; lifecycle, retry и доставка
задаются [`PIPELINE_SPEC.md`](./PIPELINE_SPEC.md). UI `SYSTEM_DESIGN.md`
содержит прежние `RUNNING + stage` и требует синхронизации с status-only
контрактом, прежде чем его можно использовать как источник текущих API DTO.
Поддерживаемый VCS provider — GitHub; GitLab не реализован.
Backend-mediated session refresh и
явное подключение repository являются частью текущего OpenAPI, но их HTTP
handlers не реализованы.

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
- GitHub-only webhook intake #14: HMAC route, durable `WebhookReceipt`,
  SHA-pinned GitHub capture, bounded diff parser и отдельный recovery worker;
  delivery не создаёт ReviewJob.
- `RedisQueue`: enqueue, receive, ACK, safe dead-letter quarantine и очистка DLQ;
- bounded `PostgresOutboxDispatcher.dispatch_batch()` с PostgreSQL quarantine и
  CLI для single batch / continuous polling; deployment автоматически его
  не запускает;
- analyze/publish lease acquire, renew, release и transactional fencing;
- bounded `PostgresReviewWorker.run_once()` с проверкой durable completion и
  автоматическим TaskLease heartbeat;
- `PostgresAnalysisHandler`: GitHub snapshot, Level 1 context, canonical schema
  и semantic validation, вызов внедряемого LLM-порта, сохранение результата и
  атомарная подготовка publication/outbox;
- verified v1/v2 prompt bundles, frozen configuration reader/admission и
  production-capable internal analysis composition;
- analysis CLI Stage 8C-04-01: trusted preflight, bounded `--once`, последовательный
  continuous loop новых analyze deliveries, idle polling и остановка idle loop;
- migrations 0008…0010: nullable frozen marker, durable LLM ledger/attempts и repair claim;
- forward transport migration 0012 после неизменённой 0011: confirmed Redis ID и
  persisted 90s throttle без generation/window counters;
- интеграционные тесты Redis queue, dispatcher, worker, analysis composition,
  durable crash accounting, legacy compatibility и atomic terminal handoff;
- HTTP start ReviewJob со scoped idempotency и атомарными quota/outbox writes;
- HTTP detail/findings с RepositoryAccess и keyset pagination; до подключения
  backend session integration endpoints закрыты для реальных пользователей.

Миграция `0003` проверяет существующие пары ChunkResult/ContextPayload при
создании составного FK. Если данные уже имеют разных владельцев, PostgreSQL
отклоняет миграцию целиком; автоматического исправления таких строк нет.

Не реализовано:

- остальные реализации repositories и Unit of Work;
- остальные HTTP-обработчики и DTO;
- интеграция `VcsPort` с авторизованным ReviewJob worker;
- Context Builder с source windows, whole files и related symbols сверх Level 1;
- transport retention/cleanup и полный in-flight shutdown analysis CLI;
- publication executor (publisher worker) с call-start guards и сериализацией
  по ChangeRequest;
- Publisher reconciliation;
- readiness endpoint;
- модель хранения OAuth/access и HTTP handlers для connection flow;
- обязательная accepted auth-модель включает 30-минутную JWT cookie с `sid`,
  authoritative PostgreSQL `AuthSession` и refresh только действующей сессии
  с Origin/CSRF; полная реализация auth отсутствует;
- provider-specific данные установки;
- остальные HTTP handlers, DTO и session/CSRF/Origin middleware.

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
