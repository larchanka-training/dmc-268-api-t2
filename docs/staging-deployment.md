# Staging infrastructure and deployment

Документ описывает текущее staging-окружение DMC-268 Team 2, Terraform bootstrap, CI/CD и процедуру deployment frontend и backend.

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
  +-- API CI
  |     |
  |     +-- API image -> GHCR
  |
  +-- Terraform / Deploy
        |
        +----> HCP Terraform
        |      remote state
        |
        +---- SSH ----> Staging VPS
                         |
                         +-- Docker Compose
                             |
                             +-- web / Nginx :80
                             |     |
                             |     +-- React static
                             |     +-- /api/* -> FastAPI
                             |     +-- /healthcheck -> FastAPI
                             |     +-- /readiness -> FastAPI
                             |
                             +-- FastAPI :8000
                             +-- PostgreSQL
                             +-- Redis
```

Из application-сервисов публично доступен только web-контейнер на порту `80`.

FastAPI, PostgreSQL и Redis доступны только внутри Docker network.

DNS и TLS/HTTPS в текущем staging пока не настроены.

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
* проксирует `/healthcheck` и `/readiness` в FastAPI;
* публикует порт `80`.

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
POSTGRES__USER=dmc
POSTGRES__PASSWORD=<secret>
POSTGRES__DB=dmc
```

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
```

После push в `main`, если проверки успешны, публикуется immutable Docker image:

```text
ghcr.io/larchanka-training/dmc-268-api-t2:sha-<full-commit-sha>
```

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

Frontend использует:

```text
Node 24
pnpm 12.4.1
```

## Staging deployment

Workflow:

```text
.github/workflows/deploy-staging.yml
```

Deployment запускается вручную только из `main`.

При запуске требуется указать полный 40-character SHA frontend-коммита:

```text
ui_sha
```

Этот SHA должен соответствовать уже опубликованному UI image.

Пример:

```text
Actions
→ Deploy staging
→ Run workflow
→ Branch: main
→ ui_sha: <full-ui-commit-sha>
```

API image определяется текущим API commit SHA.

Workflow:

1. проверяет GitHub Variables, Secrets и формат `ui_sha`;
2. настраивает SSH с strict host key checking;
3. проверяет Docker и Docker Compose на VPS;
4. копирует `docker-compose.staging.yml`;
5. создаёт `.env.staging`;
6. временно авторизуется в GHCR;
7. выполняет `docker compose pull`;
8. запускает PostgreSQL и Redis;
9. ждёт их healthchecks;
10. выполняет:

```text
alembic upgrade head
```

11. запускает API;
12. ждёт Docker healthcheck API;
13. запускает web/Nginx;
14. проверяет:

```text
http://127.0.0.1/
```

15. выполняет logout из GHCR.

Deployment считается успешным только после успешной проверки web endpoint.

## Обычный процесс обновления staging

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
merge API -> main
       ↓
API CI
       ↓
quality / tests
       ↓
Docker build
       ↓
GHCR API image sha-<api-commit>
```

После этого staging deployment запускается вручную из API repository с нужным `ui_sha`.

Terraform запускать для обычного обновления application code не требуется.

Terraform `apply` нужен только при изменении infrastructure/bootstrap configuration.

## Проверка staging

Frontend:

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

Прямой доступ к FastAPI не должен работать:

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
* staging deployment разрешён только из `main`;
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
