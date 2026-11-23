# Локальный запуск Restoka

Передаваемая папка `kt3` является корнем проекта. Все команды ниже выполняются
из неё. Виртуальное окружение `.venv`, локальный `.env`, рабочие модули,
`requirements.txt` и `docker-compose.yml` находятся или создаются в этой папке.
Python запускается на компьютере, а Docker Compose запускает только PostgreSQL 17.

## Что установить

- Python 3.10 или новее;
- Docker Desktop с командой `docker compose`.

Docker Desktop управляет контейнерами. Docker Compose читает `docker-compose.yml`
и создаёт одинаково настроенный локальный PostgreSQL у каждого участника.
В WSL 2 включите интеграцию своей Ubuntu в Docker Desktop → Settings → Resources → WSL integration.

## Первоначальная настройка

Распакуйте переданную папку и откройте терминал в её корне — там, где находятся
`SETUP.md`, `requirements.txt` и `docker-compose.yml`.

### macOS / Linux / WSL (Bash или Zsh)

Создайте и активируйте собственное окружение, затем установите зависимости:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Создайте локальный файл настроек, если его ещё нет:

```bash
[ -e .env ] || cp .env.example .env
```

### Windows, PowerShell

Создайте и активируйте собственное окружение, затем установите зависимости:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r .\requirements.txt
```

Создайте локальный файл настроек, если его ещё нет:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Если PowerShell запрещает активацию скрипта, разрешите её только для текущего
процесса командой `Set-ExecutionPolicy -Scope Process Bypass`, затем повторите
активацию.

Файл `.env` предназначен только для локальных настроек и не передаётся другим
участникам. Проверьте в нём `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`,
`POSTGRES_PORT` и `RESTOKA_DATABASE_URL`. URL должен содержать те же базу,
пользователя, пароль и порт. Значения из примера подходят только для разработки.
Если порт `5432` занят, укажите другой порт одновременно в `POSTGRES_PORT` и URL.
База, пользователь и пароль создаются только при первой инициализации пустого volume.
Последующее изменение этих значений в `.env` не перенастраивает уже сохранённую базу.

## Запуск PostgreSQL

Запустите Docker Desktop и дождитесь его готовности. Следующие команды для обеих
систем выполняются из корня переданной папки:

```text
docker compose up -d
docker compose ps
```

При первом запуске Compose скачает образ `postgres:17`, создаст контейнер и
именованный volume с данными. В выводе `docker compose ps` сервис `postgres`
должен перейти в состояние `healthy`. Дополнительная проверка готовности:

```text
docker compose exec postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

В Docker Desktop откройте раздел **Containers**, найдите проект `kt3`, раскройте
его и проверьте, что контейнер сервиса `postgres` запущен и отмечен как healthy.
При ошибке используйте `docker compose logs postgres` из корня переданной папки.

## Переменные для Python

Compose сам читает локальный `.env`. Обычный Python этого не делает, поэтому
переменные нужно загрузить в текущий терминал. `PYTHONPATH` указывает Python, где
находятся пакеты `database`, `tools`, `harness` и `baseline`.

На macOS / Linux / WSL выполните из корня переданной папки:

```bash
set -a
source .env
set +a
export PYTHONPATH="$PWD"
```

В PowerShell выполните из корня переданной папки:

```powershell
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]*)=(.*)$') {
        $name = $matches[1].Trim()
        $value = $matches[2].Trim().Trim('"')
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
}
$env:RESTOKA_DATABASE_URL = "postgresql+psycopg://$($env:POSTGRES_USER):$($env:POSTGRES_PASSWORD)@localhost:$($env:POSTGRES_PORT)/$($env:POSTGRES_DB)"
$env:PYTHONPATH = (Get-Location).Path
```

Повторяйте этот шаг в новом терминале и после изменения `.env`.

## Создание таблиц

После запуска PostgreSQL, активации `.venv` и загрузки переменных выполните из
корня переданной папки:

```text
python -m database.create_tables
```

Команда добавляет отсутствующие таблицы и не удаляет данные. Если существующая
схема несовместима с моделями, команда остановится и потребует отдельной миграции.

## Проверка КТ3

Здесь описан только порядок воспроизведения. Результаты и разбор ошибок находятся в §8 design doc, поэтому сами метрики в этой инструкции не дублируем. Все команды ниже выполняются **из корня проекта**, после настройки БД и переменных окружения по разделам выше.

### Набор сценариев

В `eval_visible.json` 130 видимых сценариев версии `restoka-eval-v1.4`. MS‑V‑009 и MS‑V‑010 добавили после полного прогона DeepSeek. `eval_hidden.json` содержит 38 сценариев для независимой оценки. Вместе с результатом сохраняйте версию набора, его контрольную сумму и версию исходников.

Для сравнения на текущем наборе используйте корневой `eval_visible.json` в обоих запусках. Сохранённые результаты и входные данные доступны в [отчёте baseline на 130 сценариях](reports/kt3/baseline_130_final/kt3_final_metrics.json) и в [архиве полного прогона DeepSeek на 128 сценариях](reports/kt3/llm_visible_128/eval_visible.json).

```bash
sha256sum eval_visible.json
git rev-parse HEAD
```

Если проект передан без Git, вместо коммита укажите версию переданного архива.

### Интеграционные тесты

Три набора тестов проверяют бюджеты и согласования, идемпотентность и жизненный цикл заказа. Они работают с временными схемами PostgreSQL и локальными моками, без реальной отправки поставщикам:

```bash
python -m tests.test_purchase_policy
python -m tests.test_idempotency_events
python -m tests.test_order_lifecycle
```

В ранее проверенной версии все 15 тестов прошли. После изменения кода тесты нужно запустить заново: прошлый результат не подтверждает новую версию автоматически.

### Прогон baseline

Пробный запуск одного многошагового сценария:

```bash
python -m evaluation.run_baseline_visible --eval eval_visible.json --only MS-V-001
```

Прогон всего visible-набора:

```bash
python -m evaluation.run_baseline_visible --eval eval_visible.json
```

Оба запуска используют общий `ScenarioRunner`, который загружает fixture в отдельную схему БД и сохраняет состояние между событиями. Пробный запуск перезаписывает общую сводку, поэтому после него нужно выполнить полный прогон. Основной результат — `harness_runs/baseline_visible_summary.json`; подробные трассировки, ошибки и снимки состояния хранятся в подпапках `harness_runs/`.

### Оценивание

После **полного** прогона выполняются evaluator и дополнительные аудиты:

```bash
python -m evaluation.evaluate_baseline_visible --eval eval_visible.json
python -m evaluation.audit_budget_approvals --eval eval_visible.json
python -m evaluation.audit_daily_reviews --eval eval_visible.json
python -m evaluation.build_metrics_report
```

Evaluator сравнивает ответы, обязательные факты, вызовы инструментов и изменения состояния с `ground_truth` и `checks`. Аудит согласований учитывается отдельно, а результаты аудита ежедневных отчётов заменяют соответствующие непроверенные условия, не создавая дополнительных проверок в общей сводке.

Файлы с результатами:

| Файл в `harness_runs/` | Назначение |
|---|---|
| `baseline_visible_summary.json` | Статусы и длительность запусков, события и вызовы инструментов |
| `baseline_evaluation.json` | Проверки по `ground_truth` и `checks` |
| `budget_approvals_audit.json` | Политики закупок и наличие согласований |
| `daily_review_audit.json` | Доставка и содержание ежедневных отчётов |
| `kt3_final_metrics.json` | Объединённая сводка без двойного подсчёта |

В итоговой сводке нужно проверить, что `scenarios_total` совпадает с размером выбранного файла, все сценарии оценены и нет ошибок оценивания. `PASS`, `FAIL` и `UNVERIFIED` относятся к условиям проверки, а не к отдельным сценариям. Совпадение действия и обязательных фактов — приближённый показатель, а не полная Input Processing Accuracy из §7.2. Полную Contract Compliance Rate нельзя выводить только из отсутствия запрещённых операций: нужно учитывать покрытие разрешённых закупок и согласований.

Baseline не обращается к LLM API, поэтому стоимость токенов не измеряется. Инфраструктурные затраты в эту цифру не входят. Полную методику, критерий перехода к LLM и результаты см. в §7–8 design doc.

### Прогон DeepSeek

Команда запускает текущий набор из 130 сценариев. Для сравнения с baseline передайте тот же файл в `--eval` при запуске baseline и оценщика. [Настройка DeepSeek](llm/README.md#запуск-одного-сценария).

```bash
dataset=eval_visible.json
run_dir="harness_runs_llm/kt3-$(date +%Y%m%d-%H%M%S)"
RESTOKA_LLM_STREAM=1 RESTOKA_MAX_COMPLETION_TOKENS=4096 RESTOKA_LLM_TIMEOUT_SECONDS=80 \
  python -m evaluation.run_llm_visible_one --all --dataset "$dataset" --output-dir "$run_dir" \
    --model deepseek-v4.1-flash-1017 --temperature 0 --input-price-per-million 0.30 --output-price-per-million 1.20

python -m evaluation.evaluate_baseline_visible --eval "$dataset" \
  --runs "$run_dir/llm_visible_summary.json" --output "$run_dir/evaluation.json"
```

Запуск обрабатывает сценарии последовательно и обращается к платному API. MS‑V‑010 проверяет общий лимит шагов без вызова модели. В отчёте сохраняются длительность, параметры и оценка стоимости по `usage` без скидки за кэш.

Для воспроизведения полного прогона КТ3 используйте архивный набор из 128 сценариев, снимок исходников и параметры из сводки. В том прогоне работали пять параллельных исполнителей, в расчёте стоимости учтены кэшированные токены. [Сводка прогона](reports/kt3/llm_visible_128/summary.json), [снимок исходников](reports/kt3/llm_visible_128/source.tar.gz).

### Где что лежит

- `baseline/`, `harness/`, `tools/`, `database/` — реализация;
- `evaluation/` — скрипты прогона и оценки;
- `tests/` — интеграционные тесты;
- `harness_runs/` — результаты и трассировки;
- `reports/kt3/` — итоговый снимок и архивные отчёты.

Не включайте локальный `.env`, `.venv` и пароли в публичный репозиторий или архив для передачи. Hidden eval-набор передаётся только участникам, которым разрешён доступ к нему.




