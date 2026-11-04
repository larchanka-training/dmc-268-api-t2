# Handoff для UI-репозитория: автоматический деплой фронтенда

API-репозиторий деплоит фронтенд по модели ADR-0001 «каждый репозиторий деплоит себя»: UI-репозиторий инициирует деплой своей свежей версии запуском workflow `Deploy` в API-репозитории. Доступа к VPS и его секретов UI-репозиторию не нужно.

## Текущий контракт `dmc-268-ui-t2`

После merge UI PR #19 публикация образов из `main` и `develop` и шаг `Trigger API deploy` уже находятся в `ui-ci.yml`. Для работы триггера нужен fine-grained PAT:

1. Создать PAT:
   - Repository access → только `larchanka-training/dmc-268-api-t2`;
   - Permissions → Actions: **Read and write**.
2. Добавить PAT как секрет UI-репозитория `API_DEPLOY_PAT`.

После публикации образа job `publish` выполняет:

```yaml
      - name: Trigger API deploy
        env:
          GH_TOKEN: ${{ secrets.API_DEPLOY_PAT }}
        run: |
          case "${GITHUB_REF_NAME}" in
            main) environment=staging ;;
            develop) environment=develop ;;
            *) echo "Unexpected ref: ${GITHUB_REF_NAME}"; exit 1 ;;
          esac
          gh workflow run deploy-staging.yml \
            --repo larchanka-training/dmc-268-api-t2 \
            --ref main \
            -f "environment=${environment}" \
            -f "ui_sha=${GITHUB_SHA}"
```

Пуш в `main` обновляет staging, пуш в `develop` — develop-окружение. Для обеих веток UI CI сначала публикует immutable image, затем запускает `Deploy` API-репозитория.

## Как это работает

- `ui_sha` — полный 40-символьный SHA UI-коммита, чей образ уже опубликован в GHCR (`ghcr.io/larchanka-training/dmc-268-ui-t2:sha-<sha>`).
- API-репозиторий читает текущий `API_IMAGE` из env-файла окружения и обновляет только `UI_IMAGE` (ADR-0001) — версии API и UI не сбивают друг друга.
- Очередь: `concurrency`-группы `deploy-staging` / `deploy-develop` в API-репозитории — деплои, инициированные API и UI, не пересекаются внутри одного окружения.
- Первый деплой в свежее окружение — вручную (Actions → Deploy → `environment`, `ui_sha`), дальше автоматом.
