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

## Staging

Инфраструктура, Terraform, CI/CD и процедура staging deployment описаны в
[`docs/staging-deployment.md`](docs/staging-deployment.md).
