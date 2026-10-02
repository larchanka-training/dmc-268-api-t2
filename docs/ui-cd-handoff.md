# Handoff для UI-репозитория: автоматический деплой фронтенда

API-репозиторий деплоит фронтенд по модели ADR-0001 «каждый репозиторий деплоит себя»: UI-репозиторий инициирует деплой своей свежей версии запуском workflow `Deploy` в API-репозитории. Доступа к VPS и его секретов UI-репозиторию не нужно.

## Что нужно сделать в `dmc-268-ui-t2` (один раз)

1. Создать fine-grained PAT:
   - Repository access → только `larchanka-training/dmc-268-api-t2`;
   - Permissions → Actions: **Read and write**.
2. Добавить PAT как секрет UI-репозитория, например `API_DEPLOY_PAT`.
3. Добавить шаг в job `publish` workflow `ui-ci.yml` (после пуша образа в GHCR):

```yaml
      - name: Trigger API deploy
        if: github.ref == 'refs/heads/main'
        env:
          GH_TOKEN: ${{ secrets.API_DEPLOY_PAT }}
        run: |
          gh workflow run deploy-staging \
            --repo larchanka-training/dmc-268-api-t2 \
            --ref main \
            -f environment=staging \
            -f ui_sha=${{ github.sha }}
```

Когда в UI-репозитории появится ветка `develop` с публикацией образов (аналог API CI), добавить симметричный шаг с `-f environment=develop` и `ui_sha` коммита develop-образа — он будет обновлять develop-окружение.

## Как это работает

- `ui_sha` — полный 40-символьный SHA UI-коммита, чей образ уже опубликован в GHCR (`ghcr.io/larchanka-training/dmc-268-ui-t2:sha-<sha>`).
- API-репозиторий читает текущий `API_IMAGE` из env-файла окружения и обновляет только `UI_IMAGE` (ADR-0001) — версии API и UI не сбивают друг друга.
- Очередь: `concurrency`-группы `deploy-staging` / `deploy-develop` в API-репозитории — деплои, инициированные API и UI, не пересекаются внутри одного окружения.
- Первый деплой в свежее окружение — вручную (Actions → Deploy → `environment`, `ui_sha`), дальше автоматом.
