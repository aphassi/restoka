# Harness Restoka: событийные сценарии

Harness запускает исполнителя, применяет доверенные события, контролирует инструменты
и сохраняет фактические свидетельства. Deterministic baseline находится в `baseline/`.
Состояние хранится в PostgreSQL.

## Форматы сценариев и жизненный цикл

`ScenarioRunner.run(scenario, config, runner)` — синхронный вход;
`await ScenarioRunner.arun(...)` — вход из event loop. Оба принимают исполнителей
с `run(request, context)`, возвращающим `RunResult` либо awaitable этого результата.

Словарь `{id, input, fixture, ...}` запускает один запрос. По умолчанию runner
получает только `input`, а не весь словарь. При
`RunConfig(legacy_single_step_request=False)` он получает общий событийный контракт.
Результат содержит `result`, `trace_path`, `database_state`, `tool_calls`, `steps`.
`run_id` trace совпадает с `run_id` результата. Формат артефактов имеет
`format_version=2`.

`MultiStepScenario` содержит `scenario_id`, `fixture`, `events`.
Один независимый прогон:

1. Создаётся случайная схема `restoka_run_<uuid>` и таблицы существующей модели.
2. `load_fixture()` вызывается ровно один раз.
3. Создаются mocks, общий бюджет и начальный снимок.
4. Для каждого входящего события: новый trace → доверенный адаптер → новый RunContext
   и gateway → отдельный `runner.run()` → результат, снимок и изменения.
5. После последнего события сохраняются итоговые данные; удаляется только схема,
   созданная этим вызовом. Ошибки очистки сохраняются с именем оставшейся схемы.

Шаги используют одни PostgreSQL-таблицы и mock-сервисы. Между ними нет `sleep()`:
`ScenarioEvent.at` детерминированно меняет доверенное время. Независимый повтор, другой
runner и параллельный сценарий получают отдельную схему и изолированные mocks. Рабочие
таблицы не изменяются. Нужна база PostgreSQL с правом создавать схемы; Docker harness
не запускает.

## Типизированные события

Все события содержат `event_id`, aware `at`, `kind`, `input`; опционально —
`process_id`, пара `conversation_id`/`user_id`, `occurred_at` и `mocks`.

- `user_message`: передаётся runner как пользовательский запрос; текст о согласовании
  не меняет полномочия и состояние заявки.
- `manager_decision`: обязательный `ManagerDecision` с `decision`, `decided_by` и ровно
  одним из `approval_id` или `approval_from_event`. Вторая форма доверенно разрешается
  в единственную заявку, созданную указанным предыдущим событием; отсутствие или
  несколько заявок являются ошибкой сценария.
  Адаптер проверяет существующую заявку организации, не перезаписывает конфликтующее
  решение, записывает доверенную decision и вызывает `record_approval_decision` в той
  же транзакции. Коммерческие условия и привязку согласования всё равно проверяют actions.
- `supplier_reply`: обязательный `SupplierReplyEvent` с supplier/offer/message/revision,
  текстом, `changes` и при необходимости `confirmed_terms`. Проверяется принадлежность
  предложения, источник регистрируется в MockSupplierInbox. **Условия не обновляются**:
  runner вызывает `receive_supplier_reply`/`process_supplier_reply`, где остаются
  проверки источника, ревизии, текущих условий и идемпотентности.
- `external_result`: `ExternalResultEvent` с `service`, `outcome` и ровно одним из
  `operation_id` или `operation_from_event`. Ссылка на событие разрешается только в
  единственную внешнюю операцию нужного сервиса, созданную тем предыдущим событием.
  По фактическому operation_id находится существующее намерение организации; результат
  передаётся в mock `resolve`. Статус заказа не подменяется прямой записью: runner
  явно вызывает тот же инструмент для lookup/сверки. Для order допустимо подтверждение
  отправки, но не физической доставки.
- `time_tick`: доставляет runner событие нового времени. Какие проверки запускать,
  решает runner; скрытого планировщика нет.
- `scheduled_wakeup`: доставляет плановое пробуждение для самостоятельной
  проверки текущих рисков. Поле `input.reason` служит необязательной меткой.

Runner получает `EventRequest` в виде `{kind, data: event.input, event_id}`.
`kind` определяет тип события. Каждое событие запускает отдельный вызов.
Доверенные изменения трёх внешних типов применяются перед вызовом.
Эталонные поля (`ground_truth`, `checks`, `gold_steps` и т. п.) не используются и не
передаются исполнителю. При приёме словаря берутся только поля исполнения.

`at` должен идти неубывающе и не раньше fixture.now. Запоздавшее событие доставляется
с текущим `at` и более ранним `occurred_at`. Для решения управляющего это время решения,
для результата сообщения — время внешнего наблюдения. Время получения остаётся `at`.
Повтор event_id с тем же содержимым не применяет доверенные изменения повторно.
Если сохранён `event_processed`, runner не вызывается, а harness восстанавливает
зафиксированный `RunResult`. Если есть только `event_applied`, runner вызывается для
восстановления. Изменённое содержимое под тем же ID отклоняется. Повторяемые
бизнес-действия runner обязаны использовать стабильные ключи.

Граница ответственности harness — доверенное применение события, сохранённый результат,
история и идемпотентные встроенные инструменты. Произвольный Python-код runner и его
побочные эффекты вне ToolGateway harness идемпотентными не делает. При неизвестном
внешнем исходе runner должен повторно вызвать тот же инструмент с тем же operation ID:
инструмент выполнит lookup/reconciliation и не начнёт новую отправку.

```ini
record = ScenarioRunner().run(scenario, config, runner)
```

`scenario` задаёт fixture и последовательность событий, `config` — параметры запуска
и версии, `runner` — обрабатывающий события исполнитель. `record` содержит результат,
trace, снимки состояния и зафиксированные изменения сценария.

## Восстановление runner и история

`RunContext` содержит `organization_id`, `now`, `timezone`, `tools`, `trace`, `step()`,
`event_id`, `step_id`, `process_id`, `conversation_id`, `state`, `history` и
`scenario_settings`. Необязательные поля имеют значения по умолчанию;
`scenario_settings` всегда пуст и не содержит доверенную конфигурацию симуляции.

- `context.state.load()` читает сохранённые бизнес-записи PostgreSQL: заказы, заявки,
  решения, предложения, BusinessEvent с идентификаторами операций. Таблицы истории
  диалогов здесь исключены, чтобы не обходить проверку владельца.
- При наличии conversation_id/user_id создаётся или проверяется диалог.
  `context.history.recent()` возвращает существующий ToolResult/RecentMessages,
  включая `incomplete_group` и `pending_tool_calls`.
  `context.history.add(ChatMessage(...), message_id=...)` сохраняет сообщение с временем
  текущего события и привязанным пользователем. Запись пользовательских и ассистентских
  сообщений контролирует runner, а не скрытый агентный цикл harness.
- Если `message_id` не передан, harness строит стабильный ID из conversation/event,
  роли и порядкового номера этой роли. Повтор того же сообщения дедуплицируется,
  конфликтующее содержимое отклоняется, а несколько сообщений одной роли получают
  разные ID.
- Загрузка состояния/истории действительно создаёт lifecycle `recovery`.
- `ScenarioRunner(runner_factory=...)` позволяет создавать новый экземпляр исполнителя
  перед каждым событием. Без factory используется переданный объект runner.

`organization_id` и `user_id` приходят только из аутентифицированного слоя. Хранилище
проверяет владельца диалога, но само не удостоверяет личность и членство пользователя
в организации. Эти идентификаторы нельзя отдавать на выбор модели.

История содержит не более 20 последних сообщений. Если граница попадает внутрь группы
assistant/tool, обрезанная группа исключается. Незавершённая последняя группа возвращается
через `incomplete_group` и `pending_tool_calls`: runner должен сначала безопасно сверить
или завершить эти вызовы, сохранить результаты и заново прочитать историю. Внешние
операции автоматически не повторяются. Долговременной памяти и суммаризации нет.

Нельзя хранить единственную копию order/submission ID только в памяти runner.
Восстанавливайте её из PostgreSQL или истории. In-memory mocks сохраняются между
событиями одного вызова ScenarioRunner, но не переживают перезапуск процесса.
Полный прогон воспроизводится из fixture и последовательности событий.

Для создания бизнес-записей gateway устанавливает доверенное время события через
`database.runtime.event_time`; системные часы не используются. Дата `BusinessEvent`
вычисляется в timezone организации, а точный timestamp хранится как `occurred_at`.
Решение управляющего записывается доверенным адаптером и журналируется в той же
транзакции; `record_approval_decision` не является инструментом самостоятельного
согласования для модели.

`load_fixture(..., replace=True)` сначала удаляет сообщения и диалоги сбрасываемой
организации, затем её бизнес-данные. История других организаций сохраняется.

## ToolGateway, инъекции и лимиты

Разрешённые Python-имена и JSON Schema доступны через `context.tools.schemas`.
Например:

```python
result = context.tools.call("compare_suppliers", {
    "material": "масло массажное", "quantity": "5",
    "as_of": "2026-11-01", "required_by": "2026-11-10",
})
```

Организация, Session, sender, inbox, channel, verifier и runtime time не принимаются
от LLM. Manager decision, произвольный SQL, cancel_order и raw verifier-based update
не входят в реестр. Для ответа поставщика предусмотрен `process_supplier_reply`.
Каждый вызов, включая заблокированный, виден в trace с `executed` и `tool_kind`:
`read`, `compute`, `local_write`, `external` либо `forbidden`.
Ошибка расчёта/чтения имеет failed, а не неизвестный внешний эффект. Ошибка локального
commit может требовать сверки БД; неизвестный внешний исход требует сверки внешней операции.

`FixtureRuntime.tool_responses` используется как доверенная настройка:

- `{get_inventory: {error: "unavailable"|"service_unavailable"}}` запрещает реальное
  чтение и возвращает ошибку; `{error: "timeout"}` для чтения возвращает TIMEOUT.
  Настройка действует и при вложенном обращении planning к query в момент фактического
  чтения; контекст сбоя восстанавливается после каждого вызова.
- `{create_order: {error: "timeout", remote_status: "unknown", ...}}`
  возвращает заданную неопределённость **до выполнения**: это не свидетельство того,
  что в локальной БД уже создан заказ или что remote операция состоялась.
- `{create_order: {error: "timeout", after_commit: true}}` выполняет локальную
  запись и имитирует потерю ответа после транзакции. Проверить сохранённое
  намерение можно по исходным идентификаторам операции.
- `{send_supplier_message: {error: "503", retry_count: 3, max_retries: 3}}`
  блокирует отправку. Числа из fixture не запускают скрытые retries.
- Timeout/unknown для `dispatch_order`, `send_supplier_message`, `send_review`
  моделируются существующими mocks с unknown и сохранением намерения/результата.
- Для mock-результатов доступны `{dispatch_order: {outcome: "sent"|...}}` и
  `{send_supplier_message: {status: "delivered", delivered_at: ISO-time}}` (аналогично review).
  Нельзя передать выдуманные успешные складские данные вместо реальных таблиц.
- `llm_structured_output` и `supplier_callback` из fixture не являются Python
  инструментами реестра. Они доступны только доверенному allowlist-hook
  `configure_adapters(mocks, settings)` и не передаются runner.
  `configure_mocks(mocks, runtime)` также является доверенным hook настройки адаптеров.
  Неописанный инструмент выполняется обычно, если разрешён реестром.

`MockOverrides` события может заменить `tool_responses` целиком (`{}` снимает ошибки),
изменить default outcome/status/delivered_at mocks. Записанные операции не стираются.
Поздний исход уже начатой операции задавайте через `external_result`, а не новым
default. `configure_mocks(mocks, runtime)` остаётся доверенным hook первого шага.

`max_tool_calls`/`max_steps` ограничивают запрос; `scenario_max_tool_calls`/
`scenario_max_steps` — весь сценарий. Каждый допущенный запрос gateway расходует
бюджет, даже при неверных аргументах; `context.step()` отмечает нетуловый шаг. После
исчерпания вызовы не исполняются, результат получает limit_exceeded. Самостоятельный
бесконечный Python-цикл не является отмеченным шагом; harness не sandbox.

Политика времени настраивается отдельно:

- `read_timeout_seconds`: PostgreSQL `statement_timeout` для read/compute плюс проверка
  прошедшего времени после возврата Python-расчёта. Ошибка чтения безопасна для внешних эффектов.
- `request_timeout_seconds`: deadline на границах gateway/step, проверка после sync-runner
  и `asyncio.wait_for` для awaitable. CPU-bound код не прерывается принудительно.
- `external_timeout_seconds`: измерение превышения после завершения вызова, без бросания
  потока/транзакции. Подтверждённый receipt сохраняется как факт; превышение даёт timeout
  запросу и блокирует дальнейшие инструменты. Unknown остаётся unknown. Ни один из
  исходов не означает безопасную автоматическую отмену или разрешение новой отправки.

Все значения timeout по умолчанию None. Синхронный gateway временно блокирует event
loop; detached tasks/threads исполнителя не поддерживаются. Встроенных retries нет.

## Решения, trace и данные evaluator

`RunResult.action`: act/clarify/escalate/refuse. `technical_status="completed"` без action
отклоняется. Корректное escalate/clarify — не техническая ошибка. `process_status`:
active/waiting/completed/error; ожидание не является новым action. Общий scenario_status
ошибочный, если хотя бы один запрос технически не завершён; иначе берётся состояние
процесса последнего запроса. Последующие события могут восстанавливать обработку после
ошибки, но история ошибки не исчезает.

Каждое событие имеет отдельный trace, общий scenario_id/run_id и при наличии process_id
и conversation_id. Пример связей (сокращённые идентификаторы):

```json
[
  {"run_id":"run1","scenario_id":"approval-later","event_id":"request-1","trace_id":"traceA"},
  {"run_id":"run1","scenario_id":"approval-later","event_id":"decision-1","trace_id":"traceB"},
  {"run_id":"run1","scenario_id":"approval-later","event_id":"status-1","trace_id":"traceC"}
]
```

В каждом trace есть lifecycle `event_received`, `decision`, `final_status`.
При технической ошибке до решения `decision` содержит action=None и available=False,
а не выдуманный успех. Дополнительно фиксируются реальные tool calls, external_error,
approval_wait (ошибка APPROVAL_REQUIRED), process_waiting и recovery. Эти названия
совместимы с lifecycle-списками `trace_required`; evaluator должен адаптировать структуру
артефакта, harness не вычисляет проверки/метрики.

Аргументы инструментов сохраняются структурой: количество, поставщик, сроки и ключи
операций видны. Секретные ключи, authorization, cookies, user_id/decided_by, email/phone
маскируются. Свободный текст по умолчанию заменяется отпечатком; `trace_free_text=True`
разрешает текст в trace только для доверенных синтетических данных, сохраняя фильтры
секретных полей и credential patterns. Trace использует wall-clock только для телеметрии;
бизнес-события всегда используют event.at/occurred_at.

Артефакты сохраняются в `harness_runs/<run_id>/` с правами каталога 0700:

- `result.json`: безопасный итог, step_results, ссылки на все traces, ошибки, конфигурация,
  runner/model/prompt версии, температура, seed и seed_support, generation_parameters,
  бюджеты/timeout policy, hash начальной fixture, снимок и changes.
- `trace_NNNN.json` или `trace.json` для одношагового формата: диагностика запроса.
- `state_NNNN.json`: подробный маскированный снимок после шага.
- `evaluator.json` (0600): результат каждого запроса с **фактическим текстом ответа**,
  а не единственным необратимым хешем. Остальные чувствительные поля маскируются.
  Этот файл предназначен только доверенному evaluator. Если сам агент вывел приватные
  сведения в текст ответа, они останутся в этом защищённом файле и не должны публиковаться.

`capture_response_text=True` дополнительно разрешает текст в диагностическом result.json;
это не требуется для evaluator.json. Не публикуйте артефакты с приватными ответами.

`changes` для каждого шага и всего сценария: orders_before, orders_created,
order_status_changes, approvals_created, decisions, offers_changed,
external_observations, external_completed, unknown_operations. Начало операции не
считается подтверждённой отправкой; external_completed требует sent/delivered result.
Несколько переходов одного заказа не создают несколько orders_created.

В step_results также сохраняются маскированная `event_configuration` и `mock_state`: ключи
внешних операций, receipts и фактическое число попыток отправки. Это позволяет отличить
потерю записи результата в БД от отсутствия внешнего эффекта, без вычисления метрик.

`trace.record_llm(...)` сохраняет модель/версию, prompt_version, token counts, Decimal cost,
reported_duration_seconds и ошибки; API не вызывается. Baseline не обязан иметь LLM-поля.
Seed можно указать только с seed_support="supported"; unsupported/unknown фиксируются
явно. Это не обещание абсолютной детерминированности реальной LLM.
