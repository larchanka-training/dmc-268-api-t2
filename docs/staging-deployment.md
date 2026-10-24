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
                         |     +-- worker / outbox-dispatcher (opt-in analysis, тот же API-образ)
                         |     +-- webhook-worker (GitHub intake, тот же API-образ)
                         |     +-- PostgreSQL, Redis
                         |
                         +-- develop: /opt/dmc-268-t2-develop  :8080
                               +-- та же конфигурация,
                                   свои контейнеры db/redis
```

Из application-сервисов публично доступен только web-контейнер каждого окружения: staging — порт `80`, develop — порт `8080`.

FastAPI, фоновые процессы, PostgreSQL и Redis доступны только внутри Docker network каждого окружения.

HTTPS завершает один контейнер Caddy на порту 443: `https://dmc268-t2.ddns.net/` для `main` (дальше на порт 80) и `https://dmc268t2-staging.ddns.net/` для `develop` (дальше на порт 8080). HTTP по IP на порту 80 остаётся, редиректа на HTTPS нет.

## Окружения

Один VPS обслуживает два изолированных окружения — по одному на деплой-ветку:

|  | staging | develop |
|---|---|---|
| Ветка-триггер | `main` | `develop` |
| Каталог на VPS | `/opt/dmc-268-t2` | `/opt/dmc-268-t2-develop` |
| Env-файл | `.env.staging` | `.env.staging.develop` |
| Compose-проект | `dmc-268-t2-staging` | `dmc-268-t2-develop` |
| Порт web | `80` | `8080` |
| URL | `https://dmc268-t2.ddns.net/` и `http://213.199.63.63/` | `https://dmc268t2-staging.ddns.net/` и `http://213.199.63.63:8080/` |

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
outbox-dispatcher
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

Фоновые процессы запускаются из того же immutable API-образа, что и FastAPI, и обновляются тем же деплоем: `api`, `worker`, `outbox-dispatcher` и `webhook-worker`.

`worker` выполняет `python -m app.analysis_worker`, `outbox-dispatcher` — `python -m app.outbox_dispatcher`. Обе команды работают постоянно, без `--once`, и включены в Compose profile `analysis`. По умолчанию профиль выключен. `webhook-worker` постоянно выполняет восстановление GitHub intake независимо от анализа.

### Включение T3 MVP analysis на staging

`StartReview → PostgreSQL Outbox → Redis → Analysis Worker → LLM → PostgreSQL → GET Review` использует существующий backend pipeline. Публикация в GitHub этим включением не запускается.

До готовности конфигурации оставьте repository Variable `STAGING_ANALYSIS_ENABLED` отсутствующей или равной `false`. API и webhook-worker продолжают работать; StartReview без manifest возвращает безопасный `503`. Develop остаётся без analysis независимо от staging-флага. Имена Compose-проектов, сети, БД и каталоги окружений разделены как прежде.

Порядок включения после merge:

1. В GitHub → Settings → Secrets and variables → Actions проверьте, что effective HTTPS endpoint равен `https://api.eurouter.ai/api/v1`: repository Variable `STAGING_EUROROUTER_BASE_URL` либо организационная `AI_DMC268_URL`, если override пуст. Repository Variable `STAGING_EUROROUTER_MODEL` должна быть равна `qwen3-coder-next-80b`. Эти значения должны совпадать с versioned staging manifest. Установите `STAGING_LLM_PRIMARY_PROVIDER=eurorouter`, `STAGING_LLM_FALLBACK_ENABLED=false`.
2. Проверьте доступность организационного Secret `AI_DMC268_T2` либо задайте repository Secret `STAGING_EUROROUTER_API_KEYS` (ключи через запятую) для переопределения. Задайте repository Secret `STAGING_GITHUB_READ_TOKEN` (read-доступ к целевому репозиторию, PR и diff); workflow передаст его контейнерам как `GITHUB__TOKEN`. Write token для анализа не нужен. Существующие настройки browser auth, webhook и PostgreSQL сохраняются.
3. Источник **несекретного** staging manifest — [deploy/analysis-runtime.staging.json](../deploy/analysis-runtime.staging.json) из того же merged commit, что и выбранный API image. Он содержит только профиль `eurorouter/review-v1`, allowlist из одной модели `qwen3-coder-next-80b`, `credential_env=LLM__EUROROUTER__API_KEYS` и пустой `admission.policy.routing.fallback`. Prompt — `v2`, digest `a09dffb11fdd1481888eab77d2530e135e998765e22f9fc254a80cc94faef50b`; соответствие packaged artifacts проверяется regression-тестом и preflight внутри image. Сохраняйте утверждённые ограничения: context `16384`, input/output `10000/2000`, на run — до `8` запросов и `80000/16000` токенов, до `5` chunks, timeout запроса `120000 ms`, analysis duration `600000 ms`; retry policy также не меняйте без отдельного согласования. Формат описан в [analysis-runtime.md](examples/analysis-runtime.md); `docs/examples/analysis-runtime.v1.json` содержит тестовые selectors/endpoints и для staging не используется. Значения credentials в JSON запрещены.
4. CD автоматически доставляет manifest через существующий SSH/SCP с pinned host key. После `Resolve image versions` workflow извлекает SHA **выбранного `API_IMAGE`**, fetch-ит этот commit, проверяет `FETCH_HEAD^{commit}`, читает `deploy/analysis-runtime.staging.json` через `git show` и вычисляет SHA-256. Отдельный SHA-256 installer вычисляется из доверенного checkout workflow на runner и передаётся SSH-командой; digest не читается из загруженного writable-файла. При UI-only deployment используется сохранённый API image и его commit, даже если checkout/API CI уже новее. Отсутствующий commit/manifest или неверный JSON блокируют deployment.
5. После остановки прежних `worker` и `outbox-dispatcher`, до migrations/preflight, root-only installer проверяет SHA-256 и JSON, доверенные каталоги (root:root без group/world write, без symlink), атомарно заменяет `/opt/dmc-268-t2-runtime/analysis-runtime/analysis-runtime.v1.json` через временный файл в том же каталоге и `rename`, выполняет `fsync`, проверяет содержимое и `root:root 0644`. Затем выбранный API image валидирует runtime schema, в том числе при выключенном analysis. Ошибка любого gate завершает deployment без запуска analysis; старые процессы уже остановлены. Deployment directory `/opt/dmc-268-t2` остаётся доступным существующему SSH-пользователю для Compose, env и входящих файлов; installer не меняет его владельца или права. Отдельные `/opt/dmc-268-t2-runtime` и `analysis-runtime` создаются root и проверяются вместе со всеми родителями: root:root, без group/world write и symlink. Новый каталог монтируется как `/app/runtime:ro`. При ошибке записи прежний manifest остаётся целым, временный файл удаляется; при ошибке после `rename` deployment всё равно завершается без старта analysis.
6. **Доверенность исполняемого кода и права:** на VPS требуется системный `/usr/bin/python3` (только stdlib, без зависимостей приложения). Workflow запускает встроенный в SSH script verifier через `/usr/bin/python3 -I` от root либо через уже разрешённый `sudo -n`; `-I` исключает cwd, `PYTHONPATH` и пользовательский site-packages до импорта stdlib. Verifier открывает загруженный installer с `O_NOFOLLOW`, проверяет regular file и SHA-256 по digest runner, затем компилирует и исполняет **те же прочитанные байты**, не открывая путь повторно. Подмена installer или symlink блокирует установку до исполнения его кода; запускать напрямую `sudo python3` для файла из deployment directory нельзя. Terraform bootstrap требует root, но не доказывает sudo-права пользователя из `VPS_DMC268_U`; без VPS они не подтверждены. При `sudo: a password is required`, `Installer integrity mismatch` или `untrusted manifest directory` deployment блокируется; проверки доверенности и права manifest не ослаблять. Не выдавайте широкое sudo разрешение ради deployment: если существующая политика не разрешает этот вызов, администратору нужен root-owned helper с узким правилом и соответствующий вызов вместо встроенного verifier. Для `SSH_USER=root` прежнего конфликта владельцев нет, однако новая схема работает и с отдельным SSH-пользователем при существующих правах на установку. Terraform для доставки менять не требуется.
7. Установите repository Variable `STAGING_ANALYSIS_ENABLED=true`. Запустите `Deploy` из `main` с `environment=staging` после успешного API CI с T3. При обычном API-deploy `ui_sha` оставьте пустым. UI-only deploy сохраняет прежний API image; не используйте его для первого включения T3.

Workflow записывает `COMPOSE_PROFILES=analysis` и `ANALYSIS__CONFIG_PATH=/app/runtime/analysis-runtime.v1.json` только для включённого staging. Для staging, в том числе при выключенном analysis, env-файл задаёт `ANALYSIS_RUNTIME_DIR=/opt/dmc-268-t2-runtime/analysis-runtime`; API и worker используют этот каталог как `:ro`. На develop сохраняются собственные deployment directory и `ANALYSIS_RUNTIME_DIR=./analysis-runtime`, пустой config path и выключенный analysis. Общий Compose-файл по умолчанию также использует локальный `./analysis-runtime`; staging получает защищённый путь из своего env-файла, а не из Secrets/Variables.

После миграций и до запуска новых API/worker workflow выполняет `load_runtime_configuration()` и существующий `deployment_preflight()` внутри выбранного worker image. Gate проверяет read token, выбранный Eurorouter profile, отсутствие fallback, совпадение model/endpoint с утверждёнными Variables, prompt closure, совместимость pending jobs и локальную готовность LLM credentials. Он не читает Redis, не получает leases, не резервирует budget и не обращается к GitHub/LLM. Ошибка выводит только `Analysis preflight failed` и завершает deployment до старта analysis. Pending jobs с сохранённым fallback также блокируют MVP-включение, даже внутри одного провайдера. Существующий ограниченный scan pending jobs может блокировать запуск; сохранённые jobs для обхода gate не изменяйте.

Успешный preflight проверяет локальную конфигурацию, а не действительность credentials у провайдеров. Реальный платный smoke-run требует отдельного согласования. Локальные тесты проверяют versioned manifest и packaged prompt; фактическая установка на VPS, совместимость staging backlog, доступность модели и готовность внешних credentials проверяются при эксплуатационном включении.

Dispatcher и analysis worker запускаются вместе с webhook-worker и проверяются на состояние running. Для worker задано `restart: on-failure:3`: прямой ручной запуск с ошибочной конфигурацией не создаёт бесконечный crash-loop. После устранения причины выполните deployment заново. Чтобы выключить анализ, установите `STAGING_ANALYSIS_ENABLED=false` и повторите deployment: оба analysis-сервиса останавливаются, config path API очищается. Уже принятые jobs сохраняются в БД до следующего включения.

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

Ежедневные дампы staging лежат на диске VPS: `/opt/dmc-268-t2/backups/dmc-YYYY-MM-DD.sql.gz`, семь файлов, каталог с правами `700`. Восстановление описано в [postgres-backup.md](proposals/postgres-backup.md).

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
| `WEBHOOK__SECRET` | `WEBHOOK__SECRET` | HMAC-секрет входящих VCS webhooks |
| `GITHUB__TOKEN` | `GITHUB__TOKEN` | Read token GitHub API для PR/compare; не Actions `GITHUB_TOKEN` |
| `GITHUB__WRITE_TOKEN` | `GITHUB__WRITE_TOKEN` | Отдельный токен публикации GitHub с Pull requests write; только для API / будущего publisher |
| `GITHUB__BOT_USER_ID` | `GITHUB__BOT_USER_ID` | Числовой ID бота для проверки авторства опубликованного ревью |
| `AUTH__GITHUB_CLIENT_SECRET` | `AUTH__GITHUB_CLIENT_SECRET` | Секрет GitHub OAuth App для входа в браузере |
| `AUTH__JWT_SECRET` | `AUTH__JWT_SECRET` | Секрет сессии, не короче 32 байт |
| `AUTH__CREDENTIAL_KEY` | `AUTH__CREDENTIAL_KEY` | Ключ Fernet для сохранённых учётных данных |

Три незасекреченных значения — GitHub Actions Variables с теми же именами, что у контейнера. Пустое значение не попадает в env-файл, и деплой из-за этого не падает.

| Variable | Переменная контейнера | Назначение |
|---|---|---|
| `AUTH__GITHUB_CLIENT_ID` | `AUTH__GITHUB_CLIENT_ID` | Client id GitHub OAuth App |
| `AUTH__GITHUB_REDIRECT_URI` | `AUTH__GITHUB_REDIRECT_URI` | `https://dmc268-t2.ddns.net/api/v1/auth/github/callback` |
| `AUTH__FRONTEND_ORIGINS` | `AUTH__FRONTEND_ORIGINS` | JSON-список, `["https://dmc268-t2.ddns.net"]` |

Все шесть имён получает только сервис `api`. Вход остаётся недоступен, пока набор неполный: API проверяет его в момент запроса, не при старте.

`webhook-worker` запускает recovery GitHub intake отдельно от ReviewJob worker.
Нужны PostgreSQL и доступ к GitHub API; подробности: [GitHub intake](github-webhook-intake.md).

Write-токен не передаётся `webhook-worker`. Publisher подключается явно через
runtime factory; его очередь, session auth и production composition root
интегрируются с PR #29. Контракт и ограничения:
[GitHub review publication](github-review-publication.md).

Настройки LLM gateway (ключ и параметры Eurorouter/Ollama) описаны отдельно — см. раздел «LLM gateway: ключ Eurorouter» ниже.

Чтобы добавить секрет из таблицы, достаточно создать его в GitHub — деплой менять не нужно. Новая переменная вне таблицы — это одна строка в шаге «Write runtime environment» деплой-workflow и одна строка в `environment:` сервисов compose.

`.env.staging` исключён из Git и существует только на staging VPS.

### LLM gateway: ключ Eurorouter

Ключ Eurorouter идёт тем же путём, что и пароль PostgreSQL: GitHub Actions Secret → `.env.staging` на VPS → переменные окружения API и analysis worker. Значения записываются через существующий formatter с экранированием dotenv/Compose и передаются по SSH stdin, без вывода в log. Подробности: `docs/llm-gateway-review-pipeline.md` §6, §6a.

| Имя в GitHub Actions | Тип | Переменная в контейнере |
|---|---|---|
| `STAGING_EUROROUTER_API_KEYS` → `AI_DMC268_T2` | Repository Secret → организационный fallback (один ключ или несколько через запятую) | `LLM__EUROROUTER__API_KEYS` |
| `STAGING_EUROROUTER_BASE_URL` → `AI_DMC268_URL` | Repository Variable → организационный fallback, только `https://…/v1` | `LLM__EUROROUTER__BASE_URL` |
| `STAGING_EUROROUTER_MODEL` | Variable | `LLM__EUROROUTER__MODEL` |
| `STAGING_LLM_PRIMARY_PROVIDER` | Variable, `eurorouter` (по умолчанию) или `ollama` | `LLM__PRIMARY_PROVIDER` |
| `STAGING_LLM_FALLBACK_ENABLED` | Variable, `false` (по умолчанию) или `true` | `LLM__FALLBACK_ENABLED` |
| `STAGING_OLLAMA_BASE_URL`, `STAGING_OLLAMA_MODEL` | Variables, нужны только если Ollama в цепочке | `LLM__OLLAMA__BASE_URL`, `LLM__OLLAMA__MODEL` |

Непустые repository overrides имеют приоритет; иначе используются организационные `AI_DMC268_T2` и `AI_DMC268_URL`, доступные репозиторию Team 2. `STAGING_EUROROUTER_MODEL` остаётся repository Variable. Подключение организационных настроек не включает анализ: `STAGING_ANALYSIS_ENABLED` оставьте отсутствующей или равной `false` до отдельного включения.

Пока Ollama не развёрнута в staging, используется режим `eurorouter` без резерва.

Шаг «Validate deployment configuration» предупреждает об отсутствующих LLM значениях без вывода ключей. При выключенном анализе это не блокирует API/webhook. При включённом T3 обязательный restricted preflight блокирует запуск analysis при неполной или несовместимой конфигурации. Frozen analysis использует model/policy из доверенного manifest, а не legacy routing-флаги; для MVP задайте Eurorouter без fallback, как описано выше. Неверные значения legacy-флагов и `BASE_URL` не на `https://` остаются ошибкой validation.

Ротация ключа: обновить используемый Secret (`STAGING_EUROROUTER_API_KEYS` при непустом override, иначе `AI_DMC268_T2`) и перезапустить workflow deploy. Горячей смены ключа нет.

Staging-хосту нужен исходящий HTTPS-доступ к Eurorouter.

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

Workflow стартует через событие `workflow_run` после успешного завершения API CI на `main` или `develop`. API image берётся из коммита, который собрал API CI (`workflow_run.head_sha`). Обновляются API и включённые фоновые процессы (один образ), версия UI сохраняется (см. ниже).

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
6. для staging извлекает manifest из SHA выбранного `API_IMAGE` и проверяет источник; копирует `docker-compose.staging.yml`, manifest и installer через SCP в writable deployment directory;
7. создаёт env-файл окружения (включая passthrough секретов);
8. временно авторизуется в GHCR;
9. выполняет `docker compose pull`;
10. останавливает прежние analysis-процессы; только для staging проверяет SHA-256 кода installer и атомарно устанавливает manifest в отдельный root-owned runtime directory с проверкой SHA-256, root:root 0644 и runtime schema выбранного image; запускает PostgreSQL и Redis;
11. ждёт их healthchecks;
12. выполняет:

```text
alembic upgrade head
```

13. при включённом staging analysis выполняет restricted preflight; запускает API;
14. ждёт Docker healthcheck API;
15. запускает `webhook-worker` и, при включённом analysis после успешного preflight, `worker` и `outbox-dispatcher`; проверяет состояние running;
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

Backend и включённые фоновые процессы деплоятся автоматически: после успешного API CI workflow `Deploy` обновляет `API_IMAGE` в окружении, соответствующем ветке, сохраняя текущую версию UI.

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

DNS-имена и HTTPS на порту 443 настроены: `https://dmc268-t2.ddns.net/` для `main`, `https://dmc268t2-staging.ddns.net/` для `develop`. HTTP на порту 80 остаётся, редиректа на HTTPS нет.
