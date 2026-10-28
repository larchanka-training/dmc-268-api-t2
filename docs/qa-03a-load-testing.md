# QA-03A — bounded GitHub webhook load characterization

Проверка QA #27 выполнена на базе `main` / `e7dc596844d7cb607736f7054378b402faf37ba0`
в ветке `qa/issue-27-webhook-load`. Локальный и удалённый `main` совпадали,
рабочее дерево перед началом было чистым. Изменения ограничены этим документом
и `tests/test_webhook_load.py`; production-код, зависимости и CI не менялись.

## Контракт и граница проверки

Оба теста отправляют HMAC-SHA256 signed HTTP requests на настоящий
`POST /api/v1/webhooks/github` через `httpx.ASGITransport`. Выполняются
production handler, `SqlAlchemyWebhookInbox`, `BackgroundTasks`, `GithubClient`
и diff parser; persistence — настоящий disposable PostgreSQL.
Только внешний GitHub HTTP заменён `httpx.MockTransport`, который проверяет
GET, host, repository path, SHA и Accept и возвращает согласованные metadata
и небольшой diff (один Python-файл, одна замена строки).
Реальные GitHub, LLM, staging и production не используются.

| Сценарий | Запросы / concurrency | Delivery IDs | HTTP | Receipts / READY | Attempts | GitHub reads | ReviewJob / OutboxEvent |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T1 unique | 32 / 4 | 32 уникальных | 32 × 202 | 32 / 32 | 1 у каждого | 128 | 0 / 0 |
| T2 duplicate | 16 / 8 | один, одинаковые body и headers | 16 × 202 | 1 / 1 | 1 | 4 суммарно | 0 / 0 |

В T1 поровну `opened` и `synchronize`. Проверяются не только counts:
множество сохранённых delivery IDs **точно** равно исходному manifest;
каждый receipt имеет ожидаемый raw diff и полный Level 1 snapshot, `READY`,
`attempts=1`, `reason_code=None`. В T2 те же проверки artifact применены
к единственному receipt. Это HTTP characterization дедупликации, без прямых
вызовов `inbox.accept()` в тестах.

У каждого сценария один Repository с новым положительным числовым external ID
из UUID; payload, GitHub metadata и base/head repository identities используют
этот ID, сохранённые owner/name и согласованные SHA. Secret — test-only literal.
Запросы ограничены `asyncio.Semaphore`; sleeps для синхронизации отсутствуют.
`gather(return_exceptions=True)` сохраняет результат каждого запроса.
Перед teardown все HTTP-задачи завершены либо отменены и awaited;
ASGI BackgroundTasks входят в эти задачи.

Первый T1 запуск выявил неверное ожидание теста для abbreviated blob SHA:
`index 1234567..7654321` корректно даёт `old_blob_sha/new_blob_sha=null`,
поскольку parser сохраняет только полные SHA. Исправлено только ожидание.
Затем T1 — `1 passed`; первый T2 — `1 passed`. Исправлений production
и искусственного red не было; first-green characterization разрешён scope.

## Измеренные результаты

Дата: 2026-10-28. Linux x86_64, 8 доступных логических CPU; Python 3.14.7,
uv 0.12.13; disposable Docker PostgreSQL 18.6 (`postgres:18-alpine`),
миграции до `0016`, loopback TCP. FastAPI 0.141.1, Starlette 1.6.0,
httpx 0.28.1, SQLAlchemy 2.0.52, asyncpg 0.31.0,
pytest 9.1.1, pytest-asyncio 1.4.0. Redis 8.10.1 был доступен для остального
suite; эти два теста не обращаются к Redis.

Три последовательных запуска пары тестов в отдельных pytest-процессах,
без параллельной DB-нагрузки при измерении:

| Run | Сценарий | Elapsed, s | Completed requests/s | p50, ms | p95, ms |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 | T1 | 2.267 | 14.117 | 274.810 | 350.224 |
| 1 | T2 | 0.457 | 34.995 | 189.040 | 353.272 |
| 2 | T1 | 2.296 | 13.937 | 280.130 | 340.683 |
| 2 | T2 | 0.458 | 34.953 | 202.674 | 359.621 |
| 3 | T1 | 2.249 | 14.232 | 272.245 | 343.896 |
| 3 | T2 | 0.481 | 33.296 | 203.635 | 381.173 |

Каждый запуск: **2 passed, 0 failed, 0 skipped**. Во всех трёх повторениях
T1: `{202: 32}`, exceptions=0, receipts=READY=32, GitHub reads=128;
T2: `{202: 16}`, exceptions=0, receipts=READY=1, GitHub reads=4.
ReviewJob/OutboxEvent отсутствуют в scope каждого Repository.

Тесты печатают строки `QA03A {JSON}` с параметрами нагрузки, окружением,
counts и метриками (видны с `pytest -s`). Individual duration измеряется
`perf_counter()` **после допуска semaphore**, до возврата/исключения HTTP-вызова.
p50/p95 — nearest rank (`ceil(n*p)-1` в отсортированной выборке).
Throughput = число завершённых HTTP responses / время всей серии, включая
ожидание очереди semaphore; exceptions считаются отдельно. Setup, проверка БД
и cleanup не входят в elapsed серии. Порогов latency/throughput нет.

**Ограничения интерпретации:** `ASGITransport` включает выполнение
`BackgroundTasks`, поэтому это **не production HTTP ACK latency**.
Duplicate responses могут завершиться раньше capture первого запроса;
проверка БД выполняется только после завершения всей серии.
Fixture использует **NullPool**, создавая соединения заново; production engine
использует pool. MockTransport не воспроизводит сетевые задержки, rate limits
или сбои GitHub. Малые серии с одним Repository и небольшим diff не являются
capacity/SLO benchmark или доказательством устойчивости под длительной нагрузкой.

## Изоляция и cleanup

Используется существующий `PostgresHarness`, без изменений общих fixtures.
Receipt query ограничен `provider=github` и manifest либо уникальным external
repository ID; глобальных DB counts в AC нет. ReviewJob ограничены Repository,
OutboxEvent — join с ReviewJob этого Repository.

`load_scenario` в `finally` повторно находит и регистрирует **все** receipts
серии в `PostgresHarness.track()`, даже если запросы завершились частично или
assertion сработал до чтения receipts. Поиск по external ID также охватывает
неожиданный сохранённый delivery ID. Затем существующий fixture удаляет только
tracked rows. Teardown HTTP-задач предшествует этой регистрации; HTTP/GitHub
clients остаются открытыми до её завершения. Глобального DELETE/TRUNCATE,
Redis flush и изменения общих fixtures нет.

Независимая session-only fault injection использовала этот же fixture и
`PostgresHarness` (дополнительные тесты/fixtures в репозиторий не добавлялись):

- Частичная серия 8/4: 7 × 202 + 1 request exception; собраны все 8 outcomes.
  Assertion намеренно возник до чтения receipts. Fixture `finally`
  зарегистрировал все 7 committed rows; после harness cleanup в scope
  сценария осталось 0 receipts / repositories / ReviewJobs / OutboxEvents.
- Отмена серии 8/4 через Event barriers: один HTTP-запрос вместе с capture
  успел завершиться, четыре следующих были заблокированы. После cancel
  все admitted и queued задачи drained, дополнительных живых asyncio tasks=0.
  Единственный committed receipt зарегистрирован; после cleanup те же counts=0.
- После предыдущих обычных запусков scoped поиск QA-03A artifacts нашёл 0
  оставшихся receipts. Fault probes завершились с exit code 0.

## Повторный запуск

Использовать только отдельную disposable PostgreSQL БД с применёнными миграциями.
Задать `POSTGRES__HOST/PORT/USER/PASSWORD/DB` для Alembic и
`DMC268_TEST_DATABASE_URL=postgresql+asyncpg://...` для harness, отключить
внешнюю телеметрию (`SENTRY__DSN=''`). Обычный запуск:

```bash
uv sync --frozen --dev
uv run alembic upgrade head
uv run pytest -q -s tests/test_webhook_load.py
for run in 1 2 3; do
  uv run pytest -q -s tests/test_webhook_load.py || break
done
```

Без `DMC268_TEST_DATABASE_URL` PostgreSQL module пропускается;
такой запуск **не проверяет QA-03A**. Существующий CI pytest step уже собирает
новый файл и предоставляет PostgreSQL, поэтому workflow не изменён.

## Quality и полный suite

- `uv sync --frozen --dev` — PASS, зависимости и lockfile не изменились.
- `uv run --no-sync ruff check` — PASS.
- `uv run --no-sync ruff format --check` — PASS, 252 files.
- `uv run --no-sync mypy .` — PASS, 225 source files.
- `DMC268_LLM_SMOKE=0 uv run --no-sync pytest -q -rs` на disposable
  PostgreSQL/Redis — **2051 passed, 0 failed, 16 skipped**, 857.89 s.
  JUnit: 2067 cases, failures=errors=0; оба новых теста passed.
  Skips: 2 live LLM provider smoke (намеренно отключены) и 14 opt-in
  Docker-проверок (`DMC268_RUN_DOCKER_TESTS` не включён).
  **PostgreSQL и QA-03A tests не пропущены.** Три существующих deprecation
  warnings относятся к Starlette/httpx, AnyIO BlockingPortal и Alembic config.
- Независимый reviewer отдельно выполнил `uv run --no-sync pytest -q -rs`
  для `test_webhook_load`, `test_github_webhook`, `test_webhook_processing`,
  `test_webhook_inbox`, `test_github_client`, `test_diff_parser`:
  **131 passed, 0 failed, 0 skipped**, 18.44 s.
- После full suite и независимого прогона scoped проверка QA-03A artifacts
  вновь дала 0 оставшихся receipts. Созданные disposable PostgreSQL/Redis
  контейнеры остановлены и удалены (`--rm`) после всех проверок.

Session evidence сохранён в gitignored `.scratch/qa03a-*`:
отдельные T1/T2, три повтора, full pytest log/JUnit, cleanup probe/report,
uv sync и независимый code review. Постоянные результаты — в этом документе;
внешние записи в tracker/PR не выполнялись.

## Close

Независимый review по `.agents/skills/project-code-review/SKILL.md`:
range закреплён на `e7dc596` → полные untracked additions двух указанных файлов
(HEAD остался на baseline); nonempty diff проверен через
`git diff --no-index /dev/null <path>`. AC source — одобренный checklist QA #27
из задания. Reviewer самостоятельно выполнил Ruff, format, mypy и 131 relevant
тест, проверил full-suite JUnit/logs и cleanup fault probes.

| Ось | Проверенная область | Findings / severity | Verdict |
| --- | --- | --- | --- |
| Acceptance criteria | T1/T2, точные IDs, artifacts, HTTP read counts, no review work, метрики | Нет; blocker/should-fix/nit = 0/0/0 | PASS |
| Tests | Реальный HTTP/PostgreSQL seam; GitHub только MockTransport; отдельные и повторные прогоны | Нет; blocker/should-fix/nit = 0/0/0 | PASS |
| Bugs / correctness | Semaphore, сбор всех outcomes, draining, регистрация partial receipts и scoped cleanup | Нет; blocker/should-fix/nit = 0/0/0 | PASS |
| Security | Test-only secret/data, отсутствие реального GitHub/LLM, parameterized SQL, безопасные метрики | Нет; blocker/should-fix/nit = 0/0/0 | PASS |

Merge blocked: **нет**. QA-03A проверен и закрыт в согласованном bounded scope;
ограничения производительности приведены выше. Commit, push, PR, deploy
и внешние записи не выполнялись. Изменены только два запрошенных файла.
