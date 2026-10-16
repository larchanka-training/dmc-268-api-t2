# Pipeline specification — DMC-268 API

Авторитетный контракт pipeline, lifecycle, retry, queue, lease, DLQ и recovery.
HTTP endpoints и DTO определяет `openapi.yaml`. Требования Issue #13 и
контракты PR #30/#31 сохраняются; текущая реализация не означает их полной активации.

## 1. Решение и границы

PostgreSQL — источник истины о ReviewJob, командах, результате, retry, lease и расходе LLM.
Redis Streams — at-least-once transport; несколько deliveries одной команды допустимы.
Transport не доказывает выполнение; enqueue, ACK и платный вызов — разные события.
Безопасность повторного выполнения обеспечивают PostgreSQL authority, fencing,
проверка durable результата и сохранённый бюджет, а не уникальность Redis delivery.

Минимальный pipeline: API → transactional outbox → dispatcher → Redis → analysis worker
→ сохранённый результат и подготовленный publish handoff. REST читает PostgreSQL.
Один production execution path использует существующую frozen Gateway composition.
Новые framework abstractions, scheduler tables и обходные execution paths не нужны.
Publisher, SSE и Reflection Pass не активируются при минимальном завершении #13.
Publish-команды и Publication сохраняются, но dispatcher отправляет только analyze.
Остальные REST-требования Issue #13 сохраняются.

## 2. Lifecycle и durable результат

`ReviewJob.status` — единственное текущее lifecycle-поле:

```text
QUEUED → FETCHING_DIFF → PARSING_CONTEXT → LLM_PROCESSING
       → COMPLETED | PARTIAL | FAILED | SKIPPED
```

COMPLETED означает полноценный валидированный результат; PARTIAL — полезный,
валидированный, но неполный результат. FAILED означает отсутствие пригодного
результата либо обязательного доверенного входа; SKIPPED — намеренный non-run.
Текущего `stage` нет; validation относится к LLM_PROCESSING.
`ReviewEvent.phase` nullable и описывает только аудит фазы.
Terminal `status` и `finished_at` меняются одним SQL UPDATE / ORM flush:
active требует NULL finished_at, terminal — ненулевой. Terminal analysis не открывается заново.
Publication lifecycle независим: сбой публикации не запускает analysis повторно.

Start-review transaction атомарно создаёт ReviewJob, начальную Publication,
quota reservation и initial analyze OutboxEvent. Её rollback не оставляет команду.
Успешный analysis transaction атомарно сохраняет summary, ChunkResult/Findings,
terminal status/finished_at, Publication(PENDING) с началом/дедлайном цикла
и canonical publish OutboxEvent. Анализ не объявляет публикацию успешной.
FAILED/SKIPPED без полезного результата не требуют publish successor.
При ошибке пригодный сохранённый результат остаётся PARTIAL с safe limitation.
Полезный валидированный late output сохраняется как PARTIAL, не разрешая новую попытку после deadline.

## 3. Retry, deadlines и платные запросы

Допустимые публичные reason codes остаются:

```text
QUEUE_DEADLINE_EXCEEDED, VCS_RATE_LIMITED, VCS_UNAVAILABLE, VCS_ACCESS_DENIED,
PR_STALE_OR_CLOSED, DIFF_UNTRUSTWORTHY, SOURCE_BLOB_UNAVAILABLE, AST_PARSE_FAILED,
LLM_TIMEOUT, LLM_UNAVAILABLE, LLM_OUTPUT_INVALID, ANALYSIS_DEADLINE_EXCEEDED
```

Generic retry разрешён только для VCS_RATE_LIMITED, VCS_UNAVAILABLE,
LLM_TIMEOUT и LLM_UNAVAILABLE: максимум три transient attempts на active phase.
Он сохраняет active status и исходный deadline; PostgreSQL `retry_at` задаёт допуск.
Redis delivery count и повторный enqueue не являются domain attempt counter.
LLM_OUTPUT_INVALID допускает ровно одну format repair, не новый generic retry.
Durable execution counter и HTTP request ledger сохраняют свои разные единицы учёта.
Provider retry/fallback, worker retry и repair делят один deadline и request budget.
Независимый framework autoretry запрещён.

| Граница | Значение / гарантия |
|---|---|
| Queue | 15 минут от создания |
| Analysis | 10 минут, включая retry и repair; часы не сбрасываются при recovery |
| Publication | 5 минут на начало новых попыток в принятом цикле |
| VCS / Ollama timeout | 15 / 120 секунд, дополнительно ограничены остатком deadline |
| Heartbeat / lease | 15 / 90 секунд |
| Recovery interval | 30 секунд |
| Concurrency | analyze 1; publish 4 — контракт будущей активации |

Context/model token budgets фиксированы в сохранённом ContextPayload и не сбрасываются при recovery.
Истечение queue/analysis deadline обрабатывает существующий fenced analysis handler:
без нового GitHub/LLM HTTP, с reason event и terminal outcome по сохранённому результату.
Transport recovery может доставить просроченную active команду именно для этого решения.
Deadline ограничивает начало работы; при недоступных зависимостях момент записи
terminal outcome может быть позже. Dispatcher не становится вторым analysis executor.
Publication expiry запрещает новые provider attempts и progress writes;
результат ранее начатой попытки можно записать только под исходным live publish lease.

Перед каждым платным HTTP, включая fallback/repair, PostgreSQL фиксирует fenced reservation.
Settlement независим от result transaction; rollback результата не возвращает потраченный бюджет.
Неизвестный usage остаётся зарезервированным, известный overrun блокирует следующие вызовы.
Crash/restart, новый fence или повторная доставка не обнуляют расход и repair allowance.
Неизвестный исторический бюджет закрывает выполнение; текущие defaults его не подменяют.
Это ограничение допуска запросов; точно однократный remote HTTP и абсолютный billing cap
при неизвестном usage/поведении провайдера не обещаются.

## 4. Queue, outbox и singleton dispatcher

Envelope v1 соответствует `schemas/queue/task-envelope.v1.json`: только
schema/event/review/task/attempt/trace identity, максимум 16 KiB UTF-8.
Source, diff, prompt и model result через очередь не передаются.
Serializer выдаёт canonical поля; consumer игнорирует неизвестные optional поля v1.
Breaking change требует новую major schema. Analyze, publish и DLQ разделены.
Обычная delivery истекает через 20 минут; её истечение не доказывает completion.
Возраст определяется Redis server-generated XADD ID относительно Redis TIME; future ID не истекает.
Expired delivery без completion proof остаётся pending и не запускает handler/HTTP;
active job получает свежую delivery через тот же PG resend. После durable outcome старый duplicate ACK допустим.

`broker_published_at` означает время последнего подтверждённого Redis enqueue.
Он записывается только после enqueue acknowledgement и не служит completion receipt.
Обрыв после XADD до PostgreSQL commit допускает дубликат той же canonical команды.
Неподтверждённая отправка не получает publication marker; прежний подтверждённый marker
при resend не стирается. Worker принимает команду до появления marker.

В первом rollout работает **ровно один активный dispatcher**, операции отправки последовательны.
Deploy/restart: остановить прежний процесс, дождаться его выхода, затем запустить новый;
при потере host сначала подтвердить его остановку. Rolling overlap и active replicas запрещены.
Singleton — операционная предпосылка, не новый distributed-leader framework.
Её нарушение может увеличить число deliveries; worker authority и cost limits остаются обязательными.
Crash/restart одного dispatcher поддерживается без process-local recovery state.

Существующие dispatcher CLI и analysis CLI сохраняются. `--once` — ограниченный проход;
dispatcher `--once` выполняет recovery sweep перед initial batch без ожидания.
Continuous dispatcher делает recovery на старте, затем через 30 секунд после окончания
предыдущего sweep по monotonic clock, также при initial backlog и пустой очереди.
Ticks последовательны; пропущенные ticks не накапливаются. `--batch-size` по умолчанию
100, положителен и capped at 100; `--poll-interval` по умолчанию 5 секунд, finite и
минимум 0.01 секунды. Interruptible idle wait ограничен ближайшим recovery tick.
Redis PING предшествует каждой итерации; connect/command timeouts — 5 секунд.
SIGTERM/SIGINT прекращают новые batch/ticks; stop во время recovery запрещает
следующий initial batch. Текущая ограниченная операция завершается, polling wait
прерывается, Redis закрывается и engine освобождается. Оператор оставляет время
на окончание batch; forced termination после XADD допускает duplicate.
Ошибки Redis/PostgreSQL дают sanitized nonzero exit, а не успешный пустой проход.
External I/O не удерживает PostgreSQL transaction. SQL/Redis operations имеют конечные timeouts.
Эта спецификация не включает автоматическую активацию сервисов в Compose/CD.

## 5. Минимальное восстановление потерянного Redis transport

OutboxEvent сохраняет `broker_published_at`, `broker_message_id` и nullable
`dispatch_not_before`; generation/CAS, recovery windows/counters удаляются.
До каждой initial/resend XADD attempt dispatcher коммитит
`dispatch_not_before = DB now + 90 секунд`. Одна reservation допускает не более одной
XADD command attempt; скрытые command retries запрещены.
Последний подтверждённый ID и marker при reservation сохраняются; заменяются
только после подтверждённой отправки. Потерянный ответ допускает ограниченные
дубликаты после persisted cooldown, без hidden retries и lifetime limit.

Initial batch выбирает максимум 100 никогда не резервированных canonical analyze
команд. Recovery рассматривает максимум 100 due active событий и делает максимум
10 resend reservations за tick. Quarantine, live lease и future retry исключаются
до LIMIT; истёкший queue/analysis deadline отменяет ожидание retry, позволяя
существующему worker terminalize без нового HTTP. PostgreSQL eligibility повторно
проверяется перед committed reservation. External I/O вне транзакции.

При подтверждённом broker ID выполняется exact Redis ID probe:
PRESENT с canonical identity откладывает проверку без resend; ABSENT/EXPIRED
разрешают повторную eligibility check и committed throttle перед одной XADD.
UNKNOWN является infrastructure failure, не успешным empty tick и не ABSENT.
UNTRUSTED даёт безопасную диагностику без XADD/ACK/изменения canonical identity;
следующая проверка откладывается, исключая горячий цикл.
Persisted deferral обеспечивает fairness due backlog: healthy сообщения не должны
постоянно занимать все candidate slots. Process-local dispatcher cursor и отдельный
terminal diagnostics scan удаляются.

Новая forward migration `0012_lean_outbox_transport.py` следует за `0011`;
0011 и история PR #30 не переписываются. Конфликт PR #30 revisions 0008/0009 —
integration blocker before merge, не blocker реализации LEAN-01 внутри PR #29.

## 6. Pending recovery, leases и безопасный ACK

Утрата Redis stream/payload восстанавливается из PostgreSQL (§5).
Сохранённый PEL после смерти consumer восстанавливается существующей consumer group:
continuous analysis worker каждые 30 секунд делает один bounded XAUTOCLAIM,
min idle 90 секунд, COUNT 1; сохраняет returned cursor, включая пустой результат.
Первый tick — через 30 секунд после trusted preflight; следующий — через 30 секунд
после завершения bounded recovery. Новые и reclaimed deliveries исполняются
последовательно; catch-up и накопления claimed batch нет. Analysis `--once` принимает
только одну новую delivery, без recovery; каждый boot использует новый consumer owner.
Identity/lease failures оставляют pending и позволяют continuous loop продолжиться;
неожиданные infrastructure failures завершают CLI nonzero с sanitized error type.
Missing group создаётся с id=0; deleted PEL IDs не доказывают completion и не создают работу.
New delivery и reclaimed delivery используют один processing path и один execution slot.

Один persistent TaskLease на ReviewJob: task kind, owner, expiry и возрастающий fence.
Release истекает строку, не удаляет её и не обнуляет token; live lease не отбирается.
Analyze claim допустим только для active analysis; publish claim — для полезного
terminal analysis и Publication(PENDING) с будущим принятым deadline.
Analyze claim не разрешает publish writes; публикация не меняет terminal ReviewJob.
Heartbeat продлевает текущий claim; lease loss останавливает локальный handler.
Cancellation не является гарантией отмены уже принятого remote запроса.
Snapshot/context/result/findings/status/publication writes проверяют live fence
в том же UoW, в том числе в SQL после lock wait; внешние вызовы идут вне transaction.
LLM call admission дополнительно проверяет canonical command, deadline и durable budget.

Consumer сверяет все canonical identity-поля с PostgreSQL до domain work.
Missing/mismatched command или quarantined source остаётся pending без domain execution;
это не invalid transport schema и не повод для DLQ.
Handler return, retry_at и промежуточный commit сами по себе не разрешают ACK.
ACK следует только после проверки committed durable decision:

- FAILED/SKIPPED analysis с finished_at;
- COMPLETED/PARTIAL analysis с подготовленной Publication и canonical,
  nonquarantined publish successor, включая envelope size bound;
- для будущего publisher: PUBLISHED/PARTIAL/SKIPPED с cycle timestamps и finished_at.

Дубликат с таким proof ACK-ается без handler/LLM. Новый execution дополнительно требует
успешного fenced release; commit до ACK failure безопасен при следующей delivery.
Retry остаётся pending до due time; ACK по retry потребовал бы atomic replacement proof,
который минимальный pipeline не вводит. Contention, lease loss, ошибки и incomplete
handoff не ACK-аются. FAILED/UNKNOWN publication требуют отдельного решения/reconciliation.

## 7. Invalid transport, DLQ и operator replay

Permanently invalid unpublished outbox event сохраняется в PostgreSQL quarantine
с paired quarantined_at/reason_code; published marker и quarantine взаимоисключены.
Допустимы UNSUPPORTED_SCHEMA_VERSION, UNKNOWN_TASK_KIND, INVALID_ENVELOPE.
Validation предшествует отправке; инфраструктурная ошибка не становится quarantine.
Historically published invalid command сохраняется, получает safe alert и backoff.
PostgreSQL quarantine не создаёт Redis DLQ record и не ACK-ает delivery.

Invalid Redis envelope попадает в safe DLQ **до source XACK**; ошибка DLQ write оставляет pending.
DLQ schema — `schemas/queue/dead/v1.json`, retention семь дней, те же три reason codes.
Только allowlisted identity/time/source_routing_key; raw payload, unknown поля,
source/prompt/provider output, secrets, exception text и stacks запрещены.
Без valid source_event_id запись diagnostic-only. Unsupported dead schema не quarantine-ится рекурсивно.
Idle retention/cleanup остаётся обязательной queue maintenance в существующем runtime,
не новым recovery scheduler; её ещё не завершённую реализацию нельзя считать удаляемой.

Replay operator-only: PostgreSQL source_event_id → проверка schema/lifecycle/lease/deadlines
и отсутствия equivalent pending command → новый OutboxEvent/new ID с source attempt+1,
тем же review/task/trace/schema. Raw DLQ не replay-ится; source не переписывается.
Без этого validated operator seam replay остаётся недоступным, не ручным Redis XADD.

## 8. Trust boundaries и командные интерфейсы

Frozen settings/model/routing/prompt identities разрешаются по сохранённому job,
verified deployment artifacts и allowlisted credentials; environment не заменяет policy.
Исторические prompt artifacts/digests не пересчитываются новым renderer.
Context wire собирается из authoritative schema/review/chunk columns и payload_body;
payload_body содержит snapshot/metadata/files/related symbols/coverage/budget.
До inference и persistence обязательны canonical JSON Schema и semantic validation:
нормализованные paths, ordered 1-based ranges, hunk counts и contiguous coordinates.
Zero-length side может начинаться с 0; nonempty — с 1. Новые contexts используют `ru`;
frozen historical language сохраняется. Public diff использует свои count names.
Raw LLM output содержит только schema-defined summary/findings/limitations;
локальный Finding $ref bundled для provider schema. Backend проверяет output и
обогащённый result, добавляет schema/latency/fingerprints; usage — только при наличии обоих token counts.
Review/chunk identity, lifecycle, provenance, request counts и safe errors принадлежат backend.
Source/rules — недоверенные данные, не system instructions; anchors проверяются по captured diff.
ReviewEvent append-only: status, nullable phase/reason, retryable, attempt≥1, safe details;
historical unknown reason остаётся в БД, публично проецируется как null.
Safe details исключают source, prompts, model reasoning, secrets, raw provider payloads и stacks.

`PublicationRepositoryPort` сохраняет get, analysis-fenced prepare и publish-fenced
update в той же UoW; acquire/heartbeat после publication deadline запрещены.
PR #30 сохраняет `handle(task, lease)`, task-kind/fence и DurableTaskOutcome контракт,
atomic publication preparation, отдельную PR/ChangeRequest serialization и reconciliation.
UNKNOWN не retry-ится автоматически; retry FAILED допустим только при confirmed remote absence;
publication PARTIAL, stale snapshot и active publication не republish-ятся автоматически.
Provider refs публично только safe opaque; read token не заменяет write credentials.
SSE требует durable monotonic per-review sequence/replay, безопасных DTO и
повторной access/session проверки; allocation и event write атомарны с переходом.
Общий event append и миграции интегрируются при PR #30, без активации здесь.
PR #31 сохраняет rules isolation, canonical rule_ref/schema mapping и исторические identities.
Reflection выключен; его будущий HTTP обязан использовать тот же durable ledger и deadline.
Подмена durable budget process-local reflection allowance недопустима.

## 9. Наблюдаемость, проверка и согласование

Incomplete handoff остаётся pending без ACK и повторного domain execution.
Отдельного terminal scan/cursor/tick и автоматического предупреждения о structural
handoff gap нет. Проверка сохранённых Publication/outbox данных и оповещение —
операционная ответственность; наличие successor само по себе не доказывает его canonical validity.
Диагностика использует только identity/counts/safe codes, без payloads и exception repr.
Требование к readiness: PostgreSQL failure делает сервис not ready; Redis/model/publisher failures блокируют
затронутую capability, сохраняя stored reads. VCS degradation блокирует starts,
access revalidation и связанные repository/PR operations; session failure — authenticated operations.
Readiness должен выдавать capability booleans из OpenAPI; endpoint пока не реализован.
Healthcheck `/healthcheck` остаётся dependency-free liveness.

## 10. Изменения архитектурных обещаний перед merge

MASTER-02 / техлид согласует до merge следующие изменения прежнего контракта PR #29:

1. Один активный dispatcher со stop-old-before-start-new заменяет поддержку
   competing dispatcher generations; rolling overlap и active replicas не поддерживаются.
2. Persisted throttle 90 секунд и максимум 10 resend reservations/tick заменяют
   generation/CAS и rolling window 3/event/300s. Lifetime limit отсутствует;
   ambiguous XADD допускает ограниченные Redis duplicates одной canonical PG команды.
3. Exact confirmed-ID probe сохраняется. PRESENT deferral и UNTRUSTED backoff
   сохраняются в PostgreSQL; отдельного terminal diagnostic scan/cursor больше нет.
   Worker отказывает в ACK при incomplete handoff; автоматическое обнаружение/alert
   удаляется, оператор отвечает за проверку сохранённых Publication/outbox данных.
4. Publisher/SSE/Reflection остаются отдельной активацией с parked durable handoff;
   численные deadlines/retry/lease/DLQ и публичные schemas не ослабляются.

Перед merge требуется согласовать с владельцем PR #30 конфликт revision IDs
0008/0009 и единую Alembic history. Опубликованная `0011` остаётся неизменной,
LEAN-01 использует forward `0012`. До upgrade необходимо drain/stop всех старых
worker/API/dispatcher процессов, использующих ORM OutboxEvent: старый binary
обращается к удаляемым columns. После migration запускается новый binary;
rolling mixed versions не поддерживаются. Rollback также требует остановки процессов:
он восстанавливает удалённые counters нулями и не восстанавливает историю
reservations. Retained broker ID, marker, throttle, quarantine и business data сохраняются.
Никакая migration другого PR здесь не перенумеровывается и не применяется.
