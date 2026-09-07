# DMC-268 API (Team 2)

Бэкенд-сервис проекта DMC-268, команда 2. Контекст покрывает каркас API и его эксплуатационные термины.

## Language

### Диагностика

**Healthcheck**:
Проверка живости (liveness): `GET /healthcheck` всегда отвечает `200 OK` и не обращается к зависимостям (БД, Redis).
_Avoid_: `/health`, health check, пинг

**Readiness**:
Проверка готовности: доступность зависимостей (PostgreSQL, Redis) через отдельный эндпоинт. Отдельное понятие, никогда не смешивается с Healthcheck.
_Avoid_: deep healthcheck, расширенный healthcheck
