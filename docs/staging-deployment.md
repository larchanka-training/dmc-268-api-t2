# Staging infrastructure and deployment

Документ описывает окружения DMC-268 Team 2 (staging и develop), Terraform bootstrap, CI/CD и процедуру deployment frontend, backend и воркеров.

## Общая схема

Staging работает на существующем VPS в Hetzner.

Terraform не создаёт VPS. Он подключается к существующему серверу по SSH и подготавливает Docker runtime.

```text
GitHub
  |
  +-- UI CI
  |     |
  |     +-- UI image -> GHCR
  |
  +-- API CI (push в main или develop)
  |     |
  |     +-- API image -> GHCR
  |
  +-- Deploy (workflow_run после успешного API CI, либо вручную)
        |
        +----> HCP Terraform
        |      remote state
        |
        +---- SSH ----> VPS Hetzner
                         |
                         +-- staging: /opt/dmc-268-t2          :80
                         |     +-- web / Nginx :80
                         |     |     +-- React static
                         |     |     +-- /api/* -> FastAPI
                         |     |     +-- /healthcheck -> FastAPI
                         |     +-- FastAPI :8000
                         |     +-- worker (заглушка review-очереди, тот же API-образ)
                         |     +-- webhook-worker (GitHub intake, тот же API-образ)
                         |     +-- PostgreSQL, Redis
                         |
                         +-- develop: /opt/dmc-268-t2-develop  :8080
                               +-- та же конфигурация,
                                   свои контейнеры db/redis
```

Из application-сервисов публично доступен только web-контейнер каждого окружения: staging — порт `80`, develop — порт `8080`.

FastAPI, оба воркера, PostgreSQL и Redis доступны только внутри Docker network каждого окружения.

DNS и TLS/HTTPS пока не настроены (HTTP по IP).

## Окружения

Один VPS обслуживает два изолированных окружения — по одному на деплой-ветку:

|  | staging | develop |
|---|---|---|
| Ветка-триггер | `main` | `develop` |
| Каталог на VPS | `/opt/dmc-268-t2` | `/opt/dmc-268-t2-develop` |
| Env-файл | `.env.staging` | `.env.staging.develop` |
| Compose-проект | `dmc-268-t2-staging` | `dmc-268-t2-develop` |
| Порт web | `80` | `8080` |
| URL | `http://213.199.63.63/` | `http://213.199.63.63:8080/` |

Окружения изолированы: у каждого свои контейнеры PostgreSQL и Redis, свои volumes (префикс — имя compose-проекта) и свой env-файл на VPS. Пароль PostgreSQL общий (`STAGING_POSTGRES_PASSWORD`).

## Staging VPS

IP сервера хранится в GitHub Actions Variable:

```text
VPS_DMC268_IP_T2
```

SSH credentials хранятся в GitHub Actions Secrets:

```text
VPS_DMC268_U
VPS_DMC268_P
```

Секреты не хранятся в Git.

Terraform и deployment workflow используют pinned SSH host keys и strict host key checking.

## Terraform

Terraform configuration находится в:

```text
infra/terraform/
```

Bootstrap выполняет:

* проверку Debian;
* установку Docker Engine из официального Docker repository;
* установку Docker Compose plugin;
* запуск и enable Docker service;
* создание staging-каталога:

```text
/opt/dmc-268-t2
```

Terraform управляет bootstrap существующего VPS, но не создаёт и не удаляет сам VPS.

### Terraform state

Terraform state хранится удалённо в HCP Terraform.

Organization:

```text
BTTF-Team-2
```

Workspace:

```text
btte-team-2
```

Workspace использует Local execution mode: `plan` и `apply` выполняются на GitHub Actions runner, а HCP Terraform хранит и синхронизирует state.

GitHub Actions получает доступ к HCP Terraform через repository secret:

```text
HCP_TERRAFORM_TOKEN
```

Локальные state-файлы исключены из Git:

```text
*.tfstate
*.tfstate.*
*.tfplan
*.tfvars
*.tfvars.json
```

### Terraform workflow

Workflow:

```text
.github/workflows/terraform-staging.yml
```

Для Pull Request в `main` выполняются:

```text
terraform fmt -check
terraform init -backend=false
terraform validate
```

VPS credentials при PR-проверке не используются.

После push в `main` с изменениями в `infra/terraform/**` или самом Terraform workflow выполняется Terraform plan с remote state из HCP Terraform.

`terraform apply` выполняется вручную:

```text
Actions
→ Terraform staging
→ Run workflow
→ Branch: main
→ Apply the Terraform changes to staging: true
```

Параллельные Terraform runs ограничены через GitHub Actions concurrency.

## Staging Docker Compose

Staging Compose:

```text
docker-compose.staging.yml
```

Текущие сервисы:

```text
web
api
worker
webhook-worker
db
redis
```

### Web / frontend

Frontend запускается из immutable GHCR image:

```text
ghcr.io/larchanka-training/dmc-268-ui-t2:sha-<ui-commit-sha>
```

UI image собирается multi-stage Docker build:

```text
Node 24 + pnpm
        ↓
    Vite build
        ↓
       dist
        ↓
Nginx runtime image
```

Nginx:

* раздаёт React static;
* поддерживает SPA fallback на `index.html`;
* проксирует `/api/*` в FastAPI;
* проксирует `/healthcheck` в FastAPI;
* публикует порт `80` (хостовый порт задаёт `WEB_PORT` в env-файле окружения: staging — `80`, develop — `8080`).

Текущий публичный staging:

```text
http://213.199.63.63/
```

### API

FastAPI запускается из immutable GHCR image:

```text
ghcr.io/larchanka-training/dmc-268-api-t2:sha-<api-commit-sha>
```

Порт `8000` не публикуется на host.

API доступен только внутри Docker network:

```text
web -> api:8000
```

### Worker

Оба воркера запускаются из того же immutable API-образа, что и FastAPI, и обновляются тем же деплоем: API-образ — одна деплой-единица репозитория (один образ, три сервиса: `api`, `worker` и `webhook-worker`).

Команда `worker` сейчас — заглушка (`sleep infinity`): логику review-очереди на Redis добавит отдельная задача. `webhook-worker` уже выполняет восстановление GitHub intake и не заменяет review-worker.

Воркеры не публикуют порты наружу и доступны только внутри Docker network.

### PostgreSQL

Используется:

```text
postgres:18-alpine
```

PostgreSQL не публикует порт наружу.

Данные сохраняются в named volume:

```text
dmc-268-t2-staging_db-data
```

### Redis

Используется:

```text
redis:8-alpine
```

Redis не публикует порт наружу и доступен только внутри Docker network.

## Runtime configuration and secrets

Пароль PostgreSQL хранится в GitHub repository secret:

```text
STAGING_POSTGRES_PASSWORD
```

Во время deployment workflow создаёт на VPS:

```text
/opt/dmc-268-t2/.env.staging
```

Файл создаётся с ограниченными правами и не хранится в Git.

Пример структуры без реальных секретов:

```dotenv
API_IMAGE=ghcr.io/larchanka-training/dmc-268-api-t2:sha-<api-commit-sha>
UI_IMAGE=ghcr.io/larchanka-training/dmc-268-ui-t2:sha-<ui-commit-sha>
WEB_PORT=80
POSTGRES__USER=dmc
POSTGRES__PASSWORD=<secret>
POSTGRES__DB=dmc
```

### Passthrough секретов времени выполнения

Конвенция: секрет приложения живёт в GitHub Encrypted Secrets под тем же именем, под которым его читает контейнер. Деплой-workflow переносит заданные секреты в env-файл окружения; незаданные секреты в файл не попадают вовсе.

| Секрет GitHub | Переменная контейнера | Назначение |
|---|---|---|
| `STAGING_POSTGRES_PASSWORD` | `POSTGRES__PASSWORD` | Пароль PostgreSQL |
| `LLM__API_KEY` | `LLM__API_KEY` | Ключ LLM-провайдера (LLM Gateway) |
| `LLM__API_BASE` | `LLM__API_BASE` | Базовый URL LLM API |
| `WEBHOOK__SECRET` | `WEBHOOK__SECRET` | HMAC-секрет входящих VCS webhooks |
| `GITHUB__TOKEN` | `GITHUB__TOKEN` | Read token GitHub API для PR/compare; не Actions `GITHUB_TOKEN` |

`webhook-worker` запускает recovery GitHub intake отдельно от ReviewJob worker.
Нужны PostgreSQL и доступ к GitHub API; подробности: [GitHub intake](github-webhook-intake.md).

Чтобы добавить секрет из таблицы, достаточно создать его в GitHub — деплой менять не нужно. Новая переменная вне таблицы — это одна строка в шаге «Write runtime environment» деплой-workflow и одна строка в `environment:` сервисов compose.

`.env.staging` исключён из Git и существует только на staging VPS.

UI container package является private.

GitHub Actions API-репозитория имеет `Read` access к package `dmc-268-ui-t2` через GitHub Packages `Manage Actions access`.

## API CI

Workflow:

```text
.github/workflows/api-ci.yml
```

Для Pull Request в `main` выполняются:

```text
Ruff lint
Ruff format check
mypy
pytest
Docker build
Compose config (dev- и staging-файлы)
```

После push в `main` или `develop`, если проверки успешны, публикуется immutable Docker image:

```text
ghcr.io/larchanka-training/dmc-268-api-t2:sha-<full-commit-sha>
```

Образ из `main` деплоится в staging, из `develop` — в develop-окружение.

Для feature branches и Pull Requests images в GHCR не публикуются.

## UI CI

Workflow находится в UI repository:

```text
.github/workflows/ui-ci.yml
```

Для Pull Request в `main` выполняются:

```text
ESLint
Stylelint
Prettier check
TypeScript check
Vite build
Docker build
```

После push в `main` публикуется immutable UI image:

```text
ghcr.io/larchanka-training/dmc-268-ui-t2:sha-<full-commit-sha>
```

Автоматический деплой UI инициируется из UI-репозитория — инструкция для UI-команды: `docs/ui-cd-handoff.md`.

Frontend использует:

```text
Node 24
pnpm 12.4.1
```

## Deployment (CD)

Workflow:

```text
.github/workflows/deploy-staging.yml
```

Деплой выполняется в окружение, соответствующее ветке-источнику: пуш в `main` обновляет staging, пуш в `develop` — develop-окружение.

### Автоматический деплой backend и воркеров

```text
merge API -> main (или develop)
      ↓
API CI (quality, build, publish)
      ↓
Deploy (workflow_run после успешного API CI)
```

Workflow стартует через событие `workflow_run` после успешного завершения API CI на `main` или `develop`. API image берётся из коммита, который собрал API CI (`workflow_run.head_sha`). Обновляются API и оба воркера (один образ), версия UI сохраняется (см. ниже).

Версия UI при этом сохраняется: если `ui_sha` не указан, workflow читает текущий `UI_IMAGE` из env-файла окружения на VPS и обновляет только `API_IMAGE` (см. ADR `docs/adr/0001-autonomous-cd.md`).

### Деплой с новой версией UI

`ui_sha` опционален. Чтобы задеплоить новую версию frontend, workflow запускается с полным 40-character SHA уже опубликованного UI image:

```text
Actions
→ Deploy
→ Run workflow
→ Branch: main
→ environment: staging (или develop)
→ ui_sha: <full-ui-commit-sha>
```

UI-репозиторий может запускать этот же workflow автоматически после публикации своего образа:

```bash
gh workflow run deploy-staging.yml \
  --repo larchanka-training/dmc-268-api-t2 \
  --ref main \
  -f environment=staging \
  -f "ui_sha=${UI_SHA}"
```

Здесь `UI_SHA` — полный SHA опубликованного UI-образа из `main`. Для `develop` тот же вызов использует `-f environment=develop` и SHA UI-образа из `develop`. Для этого в секретах UI-репозитория хранится PAT с правом запуска workflow в API-репозитории; настройка триггера находится в UI-репозитории.

Версия API при таком деплое сохраняется: `API_IMAGE` читается из env-файла выбранного окружения, обновляется только `UI_IMAGE`.

### Правила и крайние случаи

- Если `ui_sha` не указан, а `UI_IMAGE` в env-файле окружения отсутствует (первый деплой после bootstrap), workflow падает до любых изменений на сервере. Первый деплой в каждое окружение выполняется вручную с `ui_sha`; дальше автоматические деплои сохраняют зафиксированную версию UI.
- Ручной запуск (dispatch) берёт API-образ последнего успешного push-прогона API CI целевой ветки, поэтому образ гарантированно существует в GHCR.
- Параллельные деплои в одно окружение (от пуша API и от триггера UI) выстраиваются в очередь через `concurrency` (`deploy-staging` / `deploy-develop`) без отмены; очереди разных окружений независимы.
- Деплой обновляет только образ инициировавшей стороны: UI-деплой (с `ui_sha`) сохраняет текущую версию API, API-деплой — текущую версию UI. Исключение — свежее окружение без env-файла: ручной первый деплой с `ui_sha` берёт последний опубликованный API-образ целевой ветки.
- Эндпоинт `/readiness` в API отсутствует; через Nginx проксируется только `/healthcheck`.

### Шаги workflow

1. определяет целевое окружение (staging/develop) и API-коммит;
2. проверяет GitHub Variables, Secrets и формат SHA;
3. настраивает SSH с strict host key checking;
4. проверяет Docker и Docker Compose на VPS;
5. определяет `API_IMAGE` и `UI_IMAGE`: обновляется только инициировавшая сторона, версия другой стороны читается из текущего env-файла окружения;
6. копирует `docker-compose.staging.yml`;
7. создаёт env-файл окружения (включая passthrough секретов);
8. временно авторизуется в GHCR;
9. выполняет `docker compose pull`;
10. запускает PostgreSQL и Redis;
11. ждёт их healthchecks;
12. выполняет:

```text
alembic upgrade head
```

13. запускает API;
14. ждёт Docker healthcheck API;
15. запускает `worker` и `webhook-worker` и проверяет, что оба в состоянии running;
16. запускает web/Nginx;
17. проверяет web endpoint окружения (`http://127.0.0.1/` для staging, `http://127.0.0.1:8080/` для develop);
18. выполняет logout из GHCR.

Deployment считается успешным только после успешной проверки web endpoint.

## Обычный процесс обновления

Frontend:

```text
merge UI -> main
      ↓
UI CI
      ↓
quality / build
      ↓
Docker build
      ↓
GHCR UI image sha-<ui-commit>
```

Backend:

```text
merge API -> main (или develop)
       ↓
API CI
       ↓
quality / tests
       ↓
Docker build
       ↓
GHCR API image sha-<api-commit>
```

Backend и оба воркера деплоятся автоматически: после успешного API CI workflow `Deploy` обновляет `API_IMAGE` в окружении, соответствующем ветке, сохраняя текущую версию UI.

Frontend после публикации образа инициирует деплой своей версии: UI-репозиторий запускает `Deploy` с `ui_sha` через PAT (см. `docs/ui-cd-handoff.md`), либо деплой запускается вручную.

Terraform запускать для обычного обновления application code не требуется.

Terraform `apply` нужен только при изменении infrastructure/bootstrap configuration.

## Проверка окружений

Staging:

```bash
curl -i --max-time 10 http://213.199.63.63/
```

Ожидается:

```text
HTTP/1.1 200 OK
```

API healthcheck через reverse proxy:

```bash
curl -i --max-time 10 http://213.199.63.63/healthcheck
```

Ожидается:

```text
HTTP/1.1 200 OK
```

```json
{"status":"ok"}
```

Develop-окружение:

```bash
curl -i --max-time 10 http://213.199.63.63:8080/
curl -i --max-time 10 http://213.199.63.63:8080/healthcheck
```

Ожидание то же: `200 OK` и `{"status":"ok"}`.

Прямой доступ к FastAPI не должен работать (оба окружения):

```bash
curl -i --max-time 5 http://213.199.63.63:8000/healthcheck
```

Ожидается ошибка подключения или timeout.

## Security notes

* реальные secrets не хранятся в Git;
* Terraform state хранится в HCP Terraform;
* PostgreSQL и Redis не публикуются наружу;
* FastAPI port `8000` не публикуется наружу;
* публичный HTTP-трафик проходит через Nginx;
* SSH host key verification не отключается;
* Docker images имеют immutable commit SHA tags;
* деплой выполняется только из `main` и `develop` в соответствующие изолированные окружения;
* Terraform apply выполняется вручную;
* deployment workflow использует `GITHUB_TOKEN` для временной авторизации в GHCR и выполняет logout после deployment;
* UI package остаётся private и доступен API workflow с `Read` permission.

## Проверенное состояние

Staging проверен end-to-end:

* Terraform apply завершался успешно;
* повторный Terraform plan показывал `No changes`;
* Docker Engine и Docker Compose работают на VPS;
* PostgreSQL и Redis healthy;
* Alembic migration применена;
* API container healthy;
* UI и API images успешно публикуются в GHCR;
* API deployment workflow успешно скачивает private UI image;
* web/Nginx container запущен;
* `http://213.199.63.63/` возвращает HTTP 200;
* `http://213.199.63.63/healthcheck` возвращает HTTP 200 и `{"status":"ok"}`;
* прямой внешний доступ к `213.199.63.63:8000` закрыт.

## Оставшиеся инфраструктурные пункты

Текущий staging работает по HTTP через IP.

Ещё не настроены:

* DNS hostname/subdomain;
* TLS certificate;
* HTTPS на `443`.

Эти пункты требуют доступного домена/DNS-зоны или выделенного staging hostname.
