# Sprint 2 contract handoff

Состояние на 7 октября 2026 года. Это рабочая передача контрактов между
задачами, а не отметка о полном командном approval PR #21. HTTP-контракт
определяется `openapi.yaml`, lifecycle и доставка — `PIPELINE_SPEC.md`, физическая
модель — `BACKEND_ERD.md`. Старый UI `SYSTEM_DESIGN.md` ещё содержит
`RUNNING + stage` и требует синхронизации с целевым status-only lifecycle.

## Решения текущей итерации

- GitHub-only scope текущего спринта подтверждён пользователем 7 октября;
  GitLab — последующая итерация. Текст
  [API #14](https://github.com/larchanka-training/dmc-268-api-t2/issues/14)
  и DoD [UI #15](https://github.com/larchanka-training/dmc-268-ui-t2/issues/15)
  всё ещё шире этого решения и должны быть синхронизированы владельцами задач.
  Контракт не должен выдавать GitHub-only за универсальную VCS-реализацию.
- Очередь задач в
  [API #13](https://github.com/larchanka-training/dmc-268-api-t2/issues/13)
  использует Redis. PostgreSQL outbox/job/lease остаются authoritative;
  `PIPELINE_SPEC.md` задаёт гарантии доставки, но не выбирает worker framework.
- Целевой `ReviewJob.status`: `QUEUED -> FETCHING_DIFF -> PARSING_CONTEXT ->
  LLM_PROCESSING -> terminal`. Отдельного current/public `stage` нет.
  Publication status остаётся независимым.
- Текущий UI-сценарий включает явное подключение доступного GitHub repository
  и refresh только действующей cookie/DB session. OpenAPI фиксирует эти
  HTTP-контракты; handlers и реальный UI adapter ещё не реализованы.
- Миграция contract foundation следует после уже слитой `0003` ownership как
  `0004`. Составной FK результата/контекста, selective `SET NULL`, DTO
  `ReviewJobSnapshot` и PostgreSQL migration checks из #19 сохраняются.

## Передача по задачам

| Задача | Состояние и граница передачи |
| --- | --- |
| [API #11](https://github.com/larchanka-training/dmc-268-api-t2/issues/11) LLM Gateway | In Progress, открытый [PR #18](https://github.com/larchanka-training/dmc-268-api-t2/pull/18). После согласования #21 автор #18 адаптирует Pydantic models/ports к проверяемому wire context/result, а provider-level retry/failover учитывает общий deadline и бюджет worker. |
| [API #12](https://github.com/larchanka-training/dmc-268-api-t2/issues/12) контракты | Tracker Todo, открытый [PR #21](https://github.com/larchanka-training/dmc-268-api-t2/pull/21) с requested changes. Status-only lifecycle принят как целевое направление; merge требует новой миграционной цепочки, тестов legacy data и согласования потребителей. Командный approval ещё не зафиксирован. |
| [API #13](https://github.com/larchanka-training/dmc-268-api-t2/issues/13) очередь/API | Tracker Todo. Redis adapter обязан подтвердить постановку outbox command, обеспечить at-least-once delivery, ack после durable processing decision, recovery незавершённой работы и safe DLQ. Конкретная реализация выбирается владельцем #13; RabbitMQ/Celery topology не является контрактом этой итерации. |
| [API #14](https://github.com/larchanka-training/dmc-268-api-t2/issues/14) VCS/diff | In Progress. Для текущего спринта — подписанный GitHub webhook, дедуп delivery, зафиксированный SHA snapshot, raw diff/metadata, parser files/hunks/line map и фильтрация binary/generated/lock/minified JS. GitLab требуется позже, несмотря на старый текст issue. Валидные zero-length стороны hunk допускают start 0; semantic validator проверяет counts/ranges/coordinates до persistence или inference. |
| [API #15](https://github.com/larchanka-training/dmc-268-api-t2/issues/15) QA/Eval | Tracker Todo. Benchmark и Eval Harness используют тот же checked-in JSON Schema и fixtures, что Gateway и parser; auth/ReviewJob API tests не должны предполагать старый `RUNNING + stage`. |
| [UI #15](https://github.com/larchanka-training/dmc-268-ui-t2/issues/15) OAuth/App Shell | In Progress, [UI PR #17](https://github.com/larchanka-training/dmc-268-ui-t2/pull/17) — подтверждённый mock-only этап. Реальный клиент должен разобрать nested `/me`, paginated connected/available repositories, отправлять `external_id` и Origin/CSRF при connect, а также вызывать refresh до истечения session. Backend handlers ещё не реализованы. |
| [UI #16](https://github.com/larchanka-training/dmc-268-ui-t2/issues/16) PR/Diff Viewer | Tracker Todo. Потребляет `GET /reviews/{id}`, findings и bounded diff из OpenAPI. `Finding.side` и номера строк относятся к конкретной стороне snapshot; UI не должен вычислять их из строки текста. Текущая mock-страница не подтверждает совместимость с backend API. |
| [API PR #20](https://github.com/larchanka-training/dmc-268-api-t2/pull/20) / [UI #18](https://github.com/larchanka-training/dmc-268-ui-t2/issues/18) CD | API PR #20 слит в main; UI-trigger/handoff #18 ещё pending. Это зависимость deployment, не основание менять review wire contracts. |

Статусы Tracker приведены по командной сводке на эту дату; они не доказывают
выполнение DoD или merge-готовность.

## Общие границы

1. **Context.** В БД `ContextPayload.payload_body` исключает `schema_version`,
   `review_id` и `chunk_id`; wire envelope собирается из authoritative колонок и
   body. В `schemas/context/v1.json` discussions — объекты
   `author/body/path?/line?`, file language nullable; сохранённый
   `metadata.output_language` может быть legacy nonempty string, новые настройки
   принимают только `ru`. JSON Schema проверяет структуру, а публичные helpers
   `validate_ranges(payload)` и `validate_hunks(hunks, public=False)` из
   `domain/contract_validation.py` — диапазоны, hunk counts и точные contiguous
   old/new coordinates (`public=True` для HTTP `old_lines/new_lines`). Пути
   нормализуются отдельно. `BuiltContextPayload` несёт `review_id` и `chunk_id`
   вместе с версией и body; `to_wire()` отклоняет envelope-поля в body. Orchestration
   передаёт ему authoritative UUID из ReviewJob и созданного ContextPayload.
2. **LLM result.** Модель генерирует только summary/findings/limitations.
   `schemas/llm-output/v1.json` задаёт только эти поля; gateway должен развернуть
   Finding `$ref` перед передачей structured-output schema провайдеру. PR #18
   после #21 согласует nested `location.path/side/line/end_line` с плоскими
   `path/side/start_line/end_line`: nullable `location.end_line` переходит в
   ту же строку, что `location.line`. Обязательное nullable `proposed_diff_fix`
   надо добавить в model output либо заполнить `null` в adapter до проверки
   canonical result; `fingerprint` вычисляет backend, модель и wire result его
   не содержат. В `ReviewChunkResult` backend добавляет версию, измеренную
   latency и `usage` только при наличии обоих token counts (иначе `null`);
   `requests` остаётся внутренней метрикой. Review/chunk IDs находятся в
   authoritative context и сохранённых агрегатах, status — в ReviewJob;
   provider/model provenance и safe reasons не являются полями model output.
   Общие checked fixtures `schemas/examples/context.v1.json` и
   `schemas/examples/review-result.v1.json` проверяются по canonical schemas,
   без требования текстового равенства независимо сгенерированных схем.
3. **Frozen settings и миграция.** `0004` не переписывает исторические
   settings/rules/ignores/digests/язык и frozen jobs. Только точный legacy `{}`
   возвращается как canonical empty read projection без пересчёта digest и
   изменения сохранённого языка (включая legacy `en`). Иные noncanonical legacy
   settings: `GET /settings` даёт `409 LEGACY_SETTINGS_UNSUPPORTED` с opaque
   ETag, новый start — `409` до quota/outbox; ранее принятые jobs и idempotency
   replay сохраняют frozen версию. Admin full canonical PUT с `If-Match` атомарно
   создаёт новую `ru`-версию и переключает current pointer без наследования
   неподдерживаемого JSON. `limitations={}` становится `[]`, точно
   `{"warnings": [<строки>]}` сохраняет строки в массиве, `usage={}` становится
   `null`; неизвестный noncanonical JSON останавливает upgrade на preflight с
   ID и инструкцией оператору, без вывода содержимого. Исторический неизвестный
   reason code сохраняется в audit БД, но публично отображается как `null`.
   Downgrade переводит `[]` обратно в `{}`, непустой массив — в объект
   `{"warnings": [строки]}`. Ранний exclusive lock на `review_job` и
   `chunk_result` удерживается от preflight до преобразования; deployment
   требует остановки старых writes и ограниченного оператором lock timeout.
   Unsupported pending runs нужно drain-нуть прежним worker либо дать новому
   совместимый processor, не переписывая frozen настройки.
4. **Доставка.** Queue envelope содержит только IDs/task/attempt/trace.
   Подтверждение постановки в Redis не означает завершение; PostgreSQL и lease
   определяют retry/recovery. Повторная доставка не создаёт второй активный
   эквивалентный ReviewJob. Terminal `status` и `finished_at` записываются одним
   SQL `UPDATE`/ORM flush.

## Ownership HTTP/UI handoff

Backend HTTP owner реализует уже заданные в `openapi.yaml` маршруты:

- `GET /repositories`: page подключённых repository с актуальным доступом
  текущего пользователя; `GET /repositories/available`: provider-backed page
  доступных, но ещё не подключённых для него GitHub repository;
- `POST /repositories/connect` с `provider=github`, `external_id`, cookie,
  allowlisted Origin и CSRF: роль выводится из GitHub permissions, Repository и
  RepositoryAccess создаются или переиспользуются атомарно; settings
  существующего repository не сбрасываются. Новая connection даёт `201`,
  повторная — `200`;
- `POST /auth/refresh`: только active unexpired JWT и PostgreSQL session,
  Origin/CSRF; атомарное продление на 30 минут с тем же `sid` и CSRF token,
  обновлённые cookie и `Me`. Expired/revoked session даёт `401` и новый OAuth,
  зависимость недоступна — `503`.

UI owner заменяет mock-контракт PR #17 на nested `Me`, page responses и
snake_case request/response DTO, берёт CSRF из `/me`, различает `401` и `503`,
обновляет активную сессию до expiry и сохраняет безопасный OAuth return path.
Ни один из этих handlers или real-client вызовов не считается готовым только
потому, что они появились в контракте.
