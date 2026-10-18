# DMC-268 API (Team 2)

FastAPI backend service for DMC-268 Team 2.

## Стек

- Python 3.14, FastAPI, SQLAlchemy 2 (async, asyncpg), Alembic, pydantic-settings
- PostgreSQL 18, Redis 8
- Менеджер зависимостей: [uv](https://docs.astral.sh/uv/)
- Качество: ruff (check + format), mypy (strict)

## Быстрый старт (docker compose)

```bash
cp .env.example .env
docker compose up --build
```

API будет доступен на http://localhost:8000, healthcheck — http://localhost:8000/healthcheck.

Рядом поднимаются PostgreSQL, Redis и сервис-заглушка `worker` с `sleep infinity`.
Компоненты очереди реализованы, но compose не запускает dispatcher и analysis
worker автоматически.

Stage 8C-04-01 добавляет `uv run python -m app.analysis_worker --once` и
последовательный continuous mode. Требования к trusted runtime, параметры CLI и
границы реализации описаны в [`analysis-runtime.md`](docs/examples/analysis-runtime.md).
Deployment и activation этим изменением не включаются.

Миграции (после старта compose):

```bash
docker compose exec api alembic upgrade head
```

## Локальная разработка без Docker

```bash
uv sync
cp .env.example .env   # укажите POSTGRES__HOST=localhost и REDIS__URL=redis://localhost:6379/0
uv run uvicorn app.main:app --reload
```

## Ключ шифрования GitHub credentials

Для первоначальной настройки `AUTH__CREDENTIAL_KEY` сгенерируйте Fernet key
через штатный API:

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Сохраните результат полностью, включая завершающий `=`, и передайте его через
окружение времени выполнения. `AUTH__CREDENTIAL_KEY` шифрует GitHub credentials,
а отдельный `AUTH__JWT_SECRET` подписывает сессионные JWT; это разные секреты.
Ключ с удалённым завершающим `=` отклоняется при OAuth start: endpoint возвращает
`503 SERVICE_UNAVAILABLE`. Проверка конфигурации auth остаётся отложенной до
использования auth endpoints и не препятствует запуску приложения.

При обычном рестарте сохраняйте существующий ключ; не генерируйте его заново.
Смена ключа без миграции сделает ранее зашифрованные GitHub credentials
нечитаемыми. Реальные ключи нельзя записывать в Git, тестовые отчёты и логи.

## Логирование

Логи пишутся одновременно в stdout и в файл `logs/app.log` (ротация: 10 МБ × 5 копий). Настройки — в `.env`:

- `LOGGING__LEVEL` — уровень журнала (по умолчанию `INFO`);
- `LOGGING__FORMAT` — `console` (человекочитаемый, по умолчанию) или `json` (в docker-compose включён автоматически);
- `LOGGING__FILE_ENABLED`, `LOGGING__FILE_PATH` — файловый вывод.

Каждый запрос получает Request ID: он попадает во все записи журнала этого запроса и в заголовок ответа `X-Request-Id`.

## Проверки качества

```bash
uv run ruff check
uv run ruff format --check
uv run mypy .
```

Примечание: каталог `migrations/` исключён из ruff и mypy — это генерируемый boilerplate Alembic.

### Интеграционные тесты PostgreSQL

Используйте отдельную одноразовую базу PostgreSQL 18. Следующий контейнер слушает
только `localhost:55433` и не использует данные основного compose:

```bash
docker run --rm -d --name dmc268-api-test-db \
  -e POSTGRES_USER=dmc268_test \
  -e POSTGRES_PASSWORD=dmc268_test \
  -e POSTGRES_DB=dmc268_test \
  -p 127.0.0.1:55433:5432 postgres:18-alpine
until docker exec dmc268-api-test-db pg_isready -U dmc268_test -d dmc268_test; do sleep 1; done

export POSTGRES__HOST=localhost POSTGRES__PORT=55433
export POSTGRES__USER=dmc268_test POSTGRES__PASSWORD=dmc268_test POSTGRES__DB=dmc268_test
export DMC268_TEST_DATABASE_URL=postgresql+asyncpg://dmc268_test:dmc268_test@localhost:55433/dmc268_test

uv run alembic upgrade head
uv run alembic check
uv run pytest
uv run alembic downgrade base
uv run alembic upgrade head
uv run alembic check
uv run pytest tests/test_postgres_integration.py
docker stop dmc268-api-test-db
```

`alembic downgrade base` удаляет предметные таблицы. Выполняйте этот цикл только
на выделенной тестовой базе; переменные `POSTGRES__*` и
`DMC268_TEST_DATABASE_URL` должны указывать на один и тот же контейнер.

### Интеграционные тесты Redis queue adapter

Используйте отдельный Redis без рабочих данных. Тесты создают уникальные stream
keys и удаляют только их; `FLUSHDB` не вызывается. CI запускает их с Redis 8.

```bash
docker run --rm -d --name dmc268-api-test-redis \
  -p 127.0.0.1:56379:6379 redis:8-alpine
DMC268_TEST_REDIS_URL=redis://localhost:56379/0 uv run pytest tests/test_redis_queue.py
docker stop dmc268-api-test-redis
```

Без `DMC268_TEST_REDIS_URL` эти тесты пропускаются, как PostgreSQL integration
tests без отдельной тестовой БД.

`RedisQueue` принимает `redis.asyncio.Redis.from_url(settings.redis.url)`;
создание и закрытие клиента остаётся обязанностью вызывающего кода. Application
`QueuePort` предоставляет `enqueue`, `receive` и явный `ack`. Canonical envelope
отделён от transport message ID в `QueueDelivery`. Analyze и publish используют
разные streams, а `receive` читает одну новую задачу без ожидания. Полученная
задача остаётся pending до `ack`, который вызывающий worker должен делать только
после durable processing decision. Повторный ACK возвращает `False`.

Реализованы queue/dispatcher, worker orchestration с leases/fencing и heartbeat,
analysis handler и API запуска/чтения ревью. Полный production pipeline ещё не
готов: актуальная граница реализации и отложенных интеграций приведена в
[`BACKEND_ARCHITECTURE.md`, §8](docs/BACKEND_ARCHITECTURE.md#8-состояние-реализации).
Гарантии доставки и lifecycle определены в
[`PIPELINE_SPEC.md`](docs/PIPELINE_SPEC.md).

## Staging

Инфраструктура, Terraform, CI/CD (автодеплой `main` → staging, `develop` → develop-окружение) и процедура деплоя описаны в
[`docs/staging-deployment.md`](docs/staging-deployment.md).

## Публикация ревью и SSE

GitHub Publisher использует сохранённый результат анализа и запускается отдельно:

```bash
uv run python -m app.publication_worker --once
uv run python -m app.publication_worker --poll-interval 1
```

Нужны миграции, общие с analysis worker PostgreSQL/Redis и отдельные
`GITHUB__WRITE_TOKEN` / `GITHUB__BOT_USER_ID`. API и Compose-заглушка `worker`
не запускают публикацию автоматически. Контракты, SSE, recovery и интеграция
с объединёнными зависимостями:
[`docs/github-review-publication.md`](docs/github-review-publication.md).
