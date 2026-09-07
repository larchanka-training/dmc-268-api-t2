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
cp .env.example .env   # укажите POSTGRES_HOST=localhost и REDIS_URL=redis://localhost:6379/0
uv run uvicorn app.main:app --reload
```

## Проверки качества

```bash
uv run ruff check
uv run ruff format --check
uv run mypy .
```
