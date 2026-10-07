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

Рядом поднимаются PostgreSQL, Redis и сервис-заглушка `worker` (логику фоновой очереди добавит задача про воркеры; деплой-юнит уже существует).

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

## Staging

Инфраструктура, Terraform, CI/CD (автодеплой `main` → staging, `develop` → develop-окружение) и процедура деплоя описаны в
[`docs/staging-deployment.md`](docs/staging-deployment.md).
