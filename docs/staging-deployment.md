# Staging infrastructure and deployment

Документ описывает текущую staging-схему DMC-268 Team 2 API.

## Общая схема

Staging работает на существующем VPS.

Terraform не создаёт VPS. Он подключается к существующему серверу по SSH и подготавливает Docker runtime.

```text
GitHub
  |
  | GitHub Actions
  v
+-------------------------+
| CI / Terraform / Deploy |
+-------------------------+
       |             |
       |             +----> HCP Terraform
       |                    remote state
       |
       +---- SSH ----> Staging VPS
                        |
                        +-- Docker Compose
                            |
                            +-- FastAPI
                            +-- PostgreSQL
                            +-- Redis
```

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

Секреты не должны храниться в Git.

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

Terraform не управляет созданием или удалением самого VPS.

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

Файлы локального state исключены из Git:

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

После push в `main` с изменениями в `infra/terraform/**` или самом Terraform workflow дополнительно выполняется Terraform plan с remote state из HCP Terraform.

`terraform apply` выполняется только вручную:

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

Сервисы:

```text
api
db
redis
```

### API

FastAPI запускается из immutable GHCR image:

```text
ghcr.io/larchanka-training/dmc-268-api-t2:sha-<commit-sha>
```

API публикует:

```text
8000:8000
```

Текущий staging доступен напрямую по HTTP на порту `8000`. Reverse proxy, domain и TLS в текущем staging-блоке не настроены.

### PostgreSQL

Используется:

```text
postgres:18-alpine
```

PostgreSQL не публикует порт наружу и доступен только через внутреннюю Docker network.

Данные сохраняются в named volume:

```text
dmc-268-t2-staging_db-data
```

### Redis

Используется:

```text
redis:8-alpine
```

Redis также не публикует порт наружу и доступен только внутри Docker network.

## Runtime secrets

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
API_IMAGE=ghcr.io/larchanka-training/dmc-268-api-t2:sha-<commit-sha>
POSTGRES__USER=dmc
POSTGRES__PASSWORD=<secret>
POSTGRES__DB=dmc
```

`.env.staging` исключён из Git.

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

После push в `main`, если проверки успешны, CI собирает и публикует immutable Docker image в GitHub Container Registry:

```text
ghcr.io/larchanka-training/dmc-268-api-t2:sha-<full-commit-sha>
```

Feature branches и Pull Requests не публикуют images в GHCR.

## Staging deployment

Workflow:

```text
.github/workflows/deploy-staging.yml
```

Deployment запускается вручную только из `main`:

```text
Actions
→ Deploy staging
→ Run workflow
→ Branch: main
```

Workflow выполняет:

1. проверяет наличие required GitHub Variables и Secrets;
2. настраивает SSH с strict host key checking;
3. проверяет Docker и Docker Compose на VPS;
4. копирует `docker-compose.staging.yml` в `/opt/dmc-268-t2`;
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
12. проверяет:

```text
http://127.0.0.1:8000/healthcheck
```

13. выполняет logout из GHCR.

Deployment считается успешным только после успешного API healthcheck.

## Обычный процесс обновления staging

После merge application changes в `main`:

```text
merge to main
    ↓
API CI
    ↓
tests / lint / type checks
    ↓
Docker build
    ↓
GHCR image sha-<commit>
    ↓
manual Deploy staging
```

Terraform запускать для обычного обновления application code не требуется.

Terraform `apply` нужен только при изменении infrastructure/bootstrap configuration.

## Проверка staging

Публичный healthcheck:

```bash
curl -i --max-time 10 http://213.199.63.63:8000/healthcheck
```

Ожидаемый результат:

```text
HTTP/1.1 200 OK
```

```json
{"status":"ok"}
```

Корневой endpoint:

```bash
curl -i --max-time 10 http://213.199.63.63:8000/
```

## Security notes

* реальные secrets не хранятся в Git;
* Terraform state хранится в HCP Terraform;
* PostgreSQL и Redis не публикуются наружу;
* SSH host key verification не отключается;
* Docker images для deployment имеют immutable commit SHA tags;
* staging deployment разрешён только из `main`;
* Terraform apply выполняется только вручную;
* GHCR token используется только во время deployment.

## Проверенное состояние

Staging был проверен end-to-end:

* Terraform apply завершился успешно;
* повторный Terraform plan показывает `No changes`;
* Docker Engine и Docker Compose работают на VPS;
* PostgreSQL и Redis healthy;
* Alembic migration `0001` применена;
* API container запущен;
* deployment workflow получил успешный local healthcheck;
* внешний запрос к `http://213.199.63.63:8000/healthcheck` возвращает HTTP 200.
