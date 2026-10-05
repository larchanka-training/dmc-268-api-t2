# test-prs-dataset

Золотой (gold) датасет синтетических Pull Request'ов с известными багами —
используется eval-харнессом (`scripts/eval_harness/`) для проверки LLM-ревьюера:
валидности формата ответа (JSON Schema) и точности детекции (Precision/Recall).

Все PR полностью синтетические — это небольшие, сконструированные примеры
кода, не привязанные к какому-либо реальному репозиторию.

## Структура

```
test-prs-dataset/
  manifest.json              # список всех фикстур: {id, category, title}
  fixtures/
    <id>/
      pr.diff                 # unified diff — один новый файл на фикстуру
      meta.json                # {title, description, language, category}
      expected_findings.json   # эталонные находки, размеченные вручную
      golden_response.json     # валидный ответ LLM для offline/replay-режима
```

`pr.diff` всегда оформлен как добавление одного нового файла
(`@@ -0,0 +1,N @@`, все строки — добавленные). Это убирает неоднозначность
нумерации строк: номер строки N в содержимом файла совпадает с номером
изменённой (`NEW`) строки диффа.

## Категории

| Префикс id | `category` в findings | Что проверяется |
|---|---|---|
| `sec-*`   | `security`        | Уязвимости безопасности (SQLi, захардкоженные секреты, SSRF, небезопасная десериализация, path traversal) |
| `mem-*`   | `performance`     | Утечки памяти/ресурсов (незакрытые файлы/сокеты/соединения, неограниченный рост кэша) |
| `logic-*` | `correctness`     | Логические ошибки (off-by-one, неверный оператор, race condition и т.п.) |
| `syn-*`   | `maintainability` | Синтаксический оверхед (мёртвый код, избыточная вложенность, ненужная абстракция) |
| `clean-*` | — (findings = []) | Чистый код — true negative, не должен порождать находок |

Текущее распределение (22 фикстуры): 5 security, 4 performance (memory/resource
leak), 5 correctness (логика), 4 maintainability (оверхед), 4 clean (чистый код).

`category`/`severity` используют тот же словарь значений, что и домен
ревью-пайплайна: категории — `security | correctness | performance |
maintainability`, severity — `critical | high | medium | low`.

## Формат `expected_findings.json`

Список объектов вида:

```json
{
  "path": "app/search.py",
  "side": "NEW",
  "start_line": 3,
  "end_line": 4,
  "category": "security",
  "severity": "critical"
}
```

Специально не включает `title`/`explanation`/`evidence`/`recommendation` —
сопоставление предсказанных находок с эталонными идёт только по
`path` + `category` + пересечению диапазона строк (см.
`scripts/eval_harness/scoring.py`); свободный текст и `severity` в сравнение
не входят.

## Формат `golden_response.json`

Полный ответ ревьюера, валидный по
`scripts/eval_harness/schema/review_result.schema.json`:
`{"summary": str, "findings": [...], "limitations": [...]}`. Используется
офлайн/replay-режимом харнесса вместо реального вызова LLM — быстро,
детерминированно, без необходимости в API-ключах (в том числе в CI).

## Как добавить фикстуру

1. Создать `fixtures/<prefix>-NNN-short-slug/`.
2. `pr.diff` — диффом добавить один небольшой файл (см. формат выше).
3. `meta.json` — `{"title", "description", "language", "category"}`.
4. `expected_findings.json` — вручную разметить ожидаемые находки (или `[]`
   для чистого кода).
5. `golden_response.json` — полный ответ, валидный по JSON-схеме харнесса;
   для `findings` можно переиспользовать `expected_findings.json`, дополнив
   каждую находку текстовыми полями (`title`, `explanation`, `evidence`,
   `recommendation`).
6. Добавить запись `{"id", "category", "title"}` в `manifest.json`.
