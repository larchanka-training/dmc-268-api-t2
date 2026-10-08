# GitHub webhook intake (#14)

Текущий scope — только GitHub `pull_request` actions `opened` и `synchronize`.
GitLab, OAuth, запуск review, LLM, обогащение context Level 2–4 и публикация
результата сюда не входят. Webhook подтверждает доставку и сохраняет
проверяемый Level 1 artifact, но **не создаёт `ReviewJob` автоматически**.
Подключение repository и авторизованный запуск review остаются отдельными
контрактами. Интеграции с Redis worker
[#13](https://github.com/larchanka-training/dmc-268-api-t2/issues/13),
LLM Gateway [#18](https://github.com/larchanka-training/dmc-268-api-t2/pull/18)
и UI [#22](https://github.com/larchanka-training/dmc-268-ui-t2/pull/22)
не являются частью этого intake.

## Граница HTTP

`POST /api/v1/webhooks/github` проверяет HMAC-SHA256 из
`X-Hub-Signature-256` по **исходным байтам** тела (максимум 25 МиБ), а также
`X-GitHub-Delivery` и `X-GitHub-Event`. Пустой secret никогда не разрешает
доставку. Неверная подпись даёт безопасный `401`, повреждённый envelope или
подписанный, но некорректный PR payload — `422`. Принятая доставка получает
`202`; повтор того же `(provider_name, delivery_id)` не создаёт новую запись.
Если PostgreSQL inbox недоступен до durable записи, маршрут возвращает
retryable `503 SERVICE_UNAVAILABLE` с `retry_after=30`, а не подтверждает
доставку.
Неподдерживаемые события/actions подтверждаются `202` без нового receipt.
Для `opened`/`synchronize`
принимается только открытый PR с согласованными base repository ID, number,
head/base SHA и repository identity. Обрабатываются только уже подключённые
GitHub repositories; webhook не подключает repository и не доверяет owner/name
из payload больше, чем сохранённой identity.
Форма сохранённых `ignores` должна содержать список строк `globs`; только
точный legacy `{}` допускается как пустой список без изменения настроек.
Неподдерживаемая историческая форма даёт безопасный `503` до receipt:
оператору нужно заменить current settings canonical версией по контракту
#13, а затем повторить delivery. Строка вместо списка не превращается в
побуквенные glob-правила.

`WebhookReceipt` хранит только выбранные поля запроса и зафиксированные на
приёме `ignore_globs` в `request_data`, а не полный webhook body. Состояния
intake: `PENDING`, `PROCESSING`, `READY`, `FAILED`, `IGNORED` (последнее также
является default для ранее существовавших receipts). Уникальность
`(provider_name, delivery_id)` обеспечивает дедупликацию. Попыток не больше
трёх; `PROCESSING` держит lease на 90 секунд, `retry_at` определяет следующий
захват, `reason_code` содержит только безопасный код отказа. `READY` хранит
`snapshot_data` и `raw_diff` в PostgreSQL; raw diff — чувствительный исходный
код, не содержимое очереди или логов.

## Снимок и восстановление

GitHub adapter последовательно читает PR metadata, compare JSON по
`base_sha...head_sha` для `merge_base_commit.sha`, raw diff по
`merge_base_sha...head_sha`, затем повторно проверяет PR. Запросы адресованы
только `api.github.com`, redirects не следуют; SHA, открытое состояние и
repository IDs должны оставаться согласованными. На весь capture действует
15-секундный дедлайн, на каждый ответ — лимит 10 МиБ. Rate limit и временная
недоступность дают retryable safe reason; stale/closed PR, недоступный
repository и недостоверный diff не раскрывают provider body или credentials.

Парсер формирует файлы, hunks, координаты строк и coverage Level 1: максимум
50 raw files и 2000 changed rows, с фильтрацией binary, generated, lock,
minified JS и frozen `ignore_globs`. Пустой diff либо отсутствие подходящих
файлов допускают **пустой Level 1 artifact**. Это не полный
`schemas/context/v1.json` `ContextPayload`: тот требует непустой `files`, а
также review/chunk identity, budget и другие поля. Создавать его автоматически
из пустого artifact нельзя.

Recovery для intake запускается отдельно от review worker #13:

```bash
uv run python -m app.webhook_worker --once  # одна ограниченная выборка до 100 receipts
uv run python -m app.webhook_worker         # цикл с опросом каждые 5 секунд
```

Worker выбирает готовые `PENDING` и просроченные `PROCESSING` leases. Источник
истины — PostgreSQL; повторный захват не создаёт новую delivery-запись.
В staging Compose работает отдельный `webhook-worker`; он не заменяет Redis
review worker #13. Deploy передаёт secret API и опциональный GitHub token
API/worker из внешних секретов.
UI ingress также должен содержать точный webhook location с лимитом 25 МиБ:
исправление находится отдельно в UI-ветке `codex/github-webhook-proxy`.
Без него прежний nginx отклоняет тела больше 1 МиБ до API. Документальный
PR #22 не содержит этого runtime-изменения.
`WEBHOOK__SECRET` обязателен для приёма. `GITHUB__TOKEN` — опциональный
внешний read token для private repositories (PAT или GitHub App installation
token); OAuth/login flow эта задача не реализует. Секреты
передаются окружением/secret store, не добавляются в репозиторий.

## Локальная и ручная проверка

Настройте PostgreSQL по [README](../README.md), примените
`uv run alembic upgrade head` и запустите API командой
`uv run uvicorn app.main:app --reload`. Для PR-сценария должна существовать
запись `Repository(provider_name='github', external_id=<id>)`. Для локального
signed smoke используйте отдельный test secret:

```bash
export WEBHOOK__SECRET='local-only-change-me'
payload='{
  "action": "opened",
  "number": 7,
  "repository": {"id": 100, "name": "project", "owner": {"login": "team"}},
  "pull_request": {
    "number": 7,
    "state": "open",
    "head": {"sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "ref": "topic", "repo": {"id": 200}},
    "base": {"sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "ref": "main", "repo": {"id": 100}}
  }
}'
signature=$(
  PAYLOAD="$payload" uv run python -c '
import hashlib
import hmac
import os
print(hmac.new(os.environ["WEBHOOK__SECRET"].encode(), os.environ["PAYLOAD"].encode(), hashlib.sha256).hexdigest())
'
)
curl -i -X POST http://localhost:8000/api/v1/webhooks/github \
  -H "X-Hub-Signature-256: sha256=$signature" \
  -H 'X-GitHub-Delivery: local-delivery-1' \
  -H 'X-GitHub-Event: pull_request' \
  -H 'Content-Type: application/json' --data-binary "$payload"
```

Повторите **тот же** `curl` с тем же delivery ID: ожидается `202` без второй
строки receipt. Невалидная подпись должна дать `401`; для неподключённого
repository не должно появиться receipt или `ReviewJob`. Недоступный inbox
должен дать `503`, после чего delivery можно повторить. Подставные SHA выше
годятся для
проверки приёма и дедупликации, но не для успешного GitHub capture. Для
проверки `READY` используйте реальные PR/SHA и доступный read token; после
`--once` или работающего worker проверьте `processing_status`, `attempts`,
`reason_code`, `retry_at`, `snapshot_data` в `webhook_receipt` и отсутствие
нового `review_job`.

```sql
SELECT delivery_id, processing_status, attempts, reason_code, retry_at,
       snapshot_data->>'context_level' AS context_level
FROM webhook_receipt
WHERE provider_name = 'github' AND delivery_id = 'local-delivery-1';
```

Для end-to-end ручной проверки настройте GitHub webhook на публичный HTTPS URL
того же маршрута, JSON content type, тот же secret и событие Pull requests.
Откройте PR или обновите head, проверьте `202` в GitHub Recent Deliveries и
одну запись на delivery GUID. Затем используйте **Redeliver** для проверки
дедупликации; `READY` должен содержать зафиксированные SHA, diff и Level 1
coverage. Реальная GitHub delivery в рамках этой реализации ещё не
выполнялась; локальные MockTransport/ASGI и PostgreSQL-тесты её не заменяют.

Дополнительно проверен подписанный HTTP replay через настоящий локальный
uvicorn: API самостоятельно получил metadata и diff открытого UI PR #22
из GitHub API и сохранил `READY` с одним файлом и raw diff 35 583 байта.
Повтор доставки сохранил одну попытку; `ReviewJob` не появился. Проверена
также миграция существующего receipt из `0004` в `IGNORED` на `0005`.
Это проверка реального VCS-клиента, но не доставки с серверов GitHub.

Docker-проверки env passthrough включаются через `DMC268_RUN_DOCKER_TESTS=1`
при работающем Docker; CI включает их вместе с PostgreSQL-тестами.
