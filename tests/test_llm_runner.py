"""Тесты оркестратора без сетевых запросов к модели и без PostgreSQL."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

from database.history import FunctionCall, ToolCall
from harness.contracts import StepLimitExceeded
from llm import LLMRunner
from tools.contracts import ToolError, ToolErrorCode, ToolResult


def response(content=None, calls=None, prompt_tokens=18, completion_tokens=12):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return {"choices": [{"message": message}], "usage": {
        "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
    }}


def decision(action="act", text="Запрос обработан.", facts=None, status="completed"):
    return json.dumps({"action": action, "text": text, "facts": facts or {},
                       "process_status": status}, ensure_ascii=False)


class ScriptedClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


class FailingClient(ScriptedClient):
    def complete(self, **kwargs):
        if not self.replies:
            raise RuntimeError("simulated provider failure")
        return super().complete(**kwargs)


class Trace:
    def __init__(self):
        self.llm = []
    def record_llm(self, **kwargs):
        self.llm.append(kwargs)


class State:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = 0
    def load(self):
        self.calls += 1
        return self.snapshot


class History:
    def __init__(self):
        self.saved = []
        self.pending = []
        self.recent_calls = 0
    def recent(self):
        self.recent_calls += 1
        return SimpleNamespace(ok=True, data=SimpleNamespace(
            messages=list(self.saved), pending_tool_calls=list(self.pending),
            omitted_partial_group=False))
    def add(self, message):
        class Entry:
            def __init__(self, m):
                self.m = m
            def to_api(self):
                return self.m.to_api()
        self.saved.append(Entry(message))
        if message.role == "tool":
            self.pending = [call for call in self.pending if call.id != message.tool_call_id]
        return SimpleNamespace(ok=True)


class Tools:
    def __init__(self):
        self.schemas = {"get_pending_orders": {"type": "object", "properties": {}, "additionalProperties": False}}
        self.calls = []
    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name != "get_pending_orders":
            return ToolResult(ok=False, error=ToolError(
                code=ToolErrorCode.INVALID_ARGUMENTS, message="Tool is not allowed",
                operation_outcome="not_started"))
        return ToolResult(ok=True, data={"orders": [{"id": "order-1", "eta": "2026-11-10", "status": "submitted"}]})


def snapshot(delivery_date="2026-11-10"):
    def table(rows):
        return {"rows": rows, "count": len(rows)}
    return {
        "purchase_orders": table([{"id": "order-1", "status": "submitted", "supplier_id": "s1",
            "material": "крем", "eta": "2026-11-10", "quantity": "5", "total_rub": "2200"}]),
        "supplier_offers": table([{"id": "offer-1", "supplier_id": "s1", "material": "крем",
            "quantity": "5", "delivery_date": delivery_date, "unit_price_rub": "440", "confirmed": True}]),
        "business_events": table([{"id": "draft-event", "type": "order_draft_prepared",
            "details": {"request": {"offer_id": "offer-1"}, "result": {"id": "order-1"}}}]),
        "approval_requests": table([]),
        "organization_policies": table([]),
    }


class Context:
    def __init__(self, state, history=None, tools=None, max_steps=30):
        self.state = state
        self.history = history
        self.tools = tools or Tools()
        self.trace = Trace()
        self.organization_id = "org-test"
        self.now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
        self.timezone = "Europe/Moscow"
        self.event_id = "evt-1"
        self.process_id = None  # Проверяем восстановление без process_id.
        self.steps = 0
        self.max_steps = max_steps
    def step(self):
        if self.steps >= self.max_steps:
            raise StepLimitExceeded("budget")
        self.steps += 1


def test_uses_fresh_postgres_state_and_history_between_instances():
    state = State(snapshot())
    history = History()
    first_model = ScriptedClient([response(content=decision("clarify", "Нужна новая дата.", status="waiting"))])
    result1 = LLMRunner(first_model, model="fake", model_version="test").run(
        {"kind": "supplier_reply", "event_id": "e1", "data": {"text": "Срок уточняется"}},
        Context(state, history))
    assert result1.action == "clarify"
    assert len(history.saved) == 2
    # Обновление поставщика уже записано в состояние; новый runner должен его увидеть.
    state.snapshot = snapshot(delivery_date="2026-11-15")
    tools = Tools()
    calls = [{"id": "model-call-1", "type": "function", "function": {
        "name": "get_pending_orders", "arguments": "{}"}}]
    next_model = ScriptedClient([
        response(calls=calls),
        response(content=decision("act", "Заказ в статусе submitted.", {"order_id": "order-1"})),
    ])
    context = Context(state, history, tools)
    context.event_id = "e2"
    result2 = LLMRunner(next_model, model="fake", model_version="test").run(
        {"kind": "user_message", "event_id": "e2", "data": {"text": "Какой статус и срок доставки заказа?"}},
        context)
    assert result2.action == "act"
    assert tools.calls == [("get_pending_orders", {})]
    assert result2.facts["order_offer_discrepancies"][0]["stored_order_eta"] == "2026-11-10"
    assert result2.facts["order_offer_discrepancies"][0]["current_offer_delivery_date"] == "2026-11-15"
    assert result2.facts["order_offer_discrepancies"][0]["order_delivery_change_confirmed"] is False
    assert "не подтверждено" in result2.text
    assert "2026-11-15" in next_model.calls[0]["messages"][2]["content"]
    assert any("Нужна новая дата" in str(msg) for msg in next_model.calls[0]["messages"])
    assert len(history.saved) == 6  # Сообщение, вызов, результат и итоговый ответ.
    assert state.calls >= 3 and history.recent_calls == 2
    assert len(context.trace.llm) == 2


def test_unlisted_tool_attempt_is_rejected_and_logged():
    tools = Tools()
    calls = [{"id": "forbidden", "type": "function", "function": {
        "name": "payment.execute", "arguments": "{}"}}]
    client = ScriptedClient([response(calls=calls),
                             response(content=decision("refuse", "Оплата запрещена."))])
    result = LLMRunner(client, model="fake", model_version="test").run(
        {"text": "Оплати поставщику немедленно"}, Context(State(snapshot()), tools=tools))
    assert result.action == "refuse"
    assert tools.calls == [("payment.execute", {})]
    assert all(tool["function"]["name"] != "payment.execute" for tool in client.calls[0]["tools"])


def test_incomplete_tool_group_does_not_drop_the_next_event_or_repeat_the_old_write():
    history = History()
    history.pending = [ToolCall(id="call-previous",
                                function=FunctionCall(name="dispatch_order", arguments="{}"))]
    calls = [
        {"id": "unexpected-send", "type": "function", "function": {
            "name": "dispatch_order", "arguments": '{"submission_id": "fresh"}'}},
        {"id": "read", "type": "function", "function": {
            "name": "get_pending_orders", "arguments": "{}"}},
    ]
    client = ScriptedClient([response(calls=calls), response(content=decision())])
    tools = Tools()
    tools.schemas["dispatch_order"] = {"type": "object"}
    result = LLMRunner(client, model="fake", model_version="test").run(
        {"text": "Отправь заказ и покажи статус"},
        Context(State(snapshot()), history, tools))
    assert result.technical_status == "completed"
    assert tools.calls == [("get_pending_orders", {})]
    saved_request = next(entry.m.content for entry in history.saved if entry.m.role == "user")
    assert json.loads(saved_request)["data"]["text"] == "Отправь заказ и покажи статус"
    assert all(tool["function"]["name"] != "dispatch_order" for tool in client.calls[0]["tools"])
    assert history.saved[0].m.role == "tool"
    assert "unknown" in history.saved[0].m.content


def test_model_round_budget_stop():
    client = ScriptedClient([response(calls=[{"id": "read-1", "type": "function",
        "function": {"name": "get_pending_orders", "arguments": "{}"}}])])
    ctx = Context(State(snapshot()), max_steps=1)
    result = LLMRunner(client, model="fake", model_version="test").run({"text": "Статус?"}, ctx)
    assert result.technical_status == "limit_exceeded"
    assert ctx.tools.calls == [("get_pending_orders", {})]
    assert len(client.calls) == 1


def test_tool_blocked_by_event_policy_is_written_to_trace():
    from harness.trace import TraceCollector

    ctx = Context(State(snapshot()))
    ctx.trace = TraceCollector()
    model = ScriptedClient([
        response(calls=[{"id": "blocked", "type": "function", "function": {
            "name": "create_order", "arguments": "{}"}}]),
        response(content=decision(action="clarify")),
    ])
    result = LLMRunner(model, model="fake", model_version="test").run({"kind": "time_tick"}, ctx)

    assert result.action == "clarify"
    assert ctx.tools.calls == []
    attempts = [span for span in ctx.trace.spans if span["operation"] == "tool_call_blocked"]
    assert [span["result"] for span in attempts] == [{
        "tool": "create_order", "stage": "permission_denied", "critical": True,
    }]


def test_action_claim_guard_distinguishes_intentions_and_negation_from_completed_effects():
    from llm.verification import _unsafe_action_claim

    state = snapshot()
    for text in (
        "Черновик можно подготовить после уточнения.",
        "Черновик пока не был подготовлен.",
        "Заказ будет оформлен после согласования.",
        "Предложение поставщика не было обновлено.",
        "Нужно проверить предложение поставщика после обновления.",
        "Заявка на согласование не создана.",
    ):
        assert not _unsafe_action_claim(text, {}, [], state), text
    for text in (
        "Черновик подготовлен.", "Я подготовил черновик.", "Заказ оформлен.",
        "Предложение поставщика обновлено.", "Заявка на согласование создана.",
    ):
        assert _unsafe_action_claim(text, {}, [], state), text


def test_status_fallback_reports_only_the_order_read_from_storage():
    unsafe = decision("act", "Я отправил заказ order-1.", {"order_id": "order-1"})
    model = ScriptedClient([
        response(calls=[{"id": "read", "type": "function", "function": {
            "name": "get_pending_orders", "arguments": "{}"}}]),
        response(content=unsafe),
        response(content=unsafe),
    ])
    ctx = Context(State(snapshot()))
    result = LLMRunner(model, model="fake", model_version="test").run({"text": "Статус заказа?"}, ctx)

    assert result.action == "act"
    assert result.facts["order_status"] == "submitted"
    assert result.metadata == {"recovery": "verified_order_status"}
    assert "я отправил" not in result.text.casefold()
    assert ctx.tools.calls == [("get_pending_orders", {})]


def test_invalid_json_escalates_after_retry():
    ctx = Context(State(snapshot()))
    client = ScriptedClient([response(content="Вот ответ без JSON"),
                             response(content="Вторая попытка без JSON")])
    result = LLMRunner(client, model="fake", model_version="test").run({"text": "Статус?"}, ctx)
    assert (result.action, result.process_status) == ("escalate", "waiting")
    assert result.facts == {"reason": "invalid_model_json"}
    assert [call["error_code"] for call in ctx.trace.llm] == ["invalid_final_json", "invalid_final_json"]


def test_llm_tool_descriptions_have_distinct_order_lifecycle():
    from llm.tool_descriptions import DESCRIPTIONS
    assert "DRAFT" in DESCRIPTIONS["prepare_draft"]
    assert "LOCAL SUBMISSION INTENT" in DESCRIPTIONS["create_order"]
    assert "EXTERNAL SIDE EFFECT" in DESCRIPTIONS["dispatch_order"]


def test_unknown_external_result_cannot_be_reported_as_completed():
    from tools.contracts import ToolResult

    class UnknownTools(Tools):
        def __init__(self):
            super().__init__()
            self.schemas["dispatch_order"] = {"type": "object", "properties": {
                "submission_id": {"type": "string"}}, "required": ["submission_id"]}
        def call(self, name, arguments):
            self.calls.append((name, arguments))
            return ToolResult(ok=False, error=ToolError(
                code=ToolErrorCode.OPERATION_OUTCOME_UNKNOWN,
                message="External outcome unknown", operation_outcome="unknown"))

    tools = UnknownTools()
    calls = [{"id": "unknown-send", "type": "function", "function": {
        "name": "dispatch_order", "arguments": '{"submission_id": "sid-1"}'}}]
    client = ScriptedClient([response(calls=calls),
                             response(content=decision("act", "Заказ успешно отправлен."))])
    state = snapshot()
    state["business_events"]["rows"].append(
        {"id": "sid-1", "type": "order_submission", "details": {"state": "unknown"}})
    result = LLMRunner(client, model="fake", model_version="test").run(
        {"text": "Отправь заказ"}, Context(State(state), tools=tools))
    assert result.action == "escalate" and result.process_status == "waiting"
    assert "Исход операции неизвестен" in result.text
    assert len(tools.calls) == 1


def test_missing_token_usage_does_not_claim_zero_ruble_cost():
    from decimal import Decimal
    ctx = Context(State(snapshot()))
    client = ScriptedClient([{"choices": [{"message": {"content": decision("clarify", "Уточните запрос.", status="waiting")}}]}])
    result = LLMRunner(client, model="fake", model_version="test",
        input_price_per_million=Decimal("1"),
        output_price_per_million=Decimal("1")).run({"text": "Статус?"}, ctx)
    assert result.action == "clarify"
    assert ctx.trace.llm[0]["cost"] is None


def test_does_not_accept_fabricated_sent_order_without_tool_evidence():
    fake = ScriptedClient([response(content=decision("act", "Заказ order-FAKE успешно отправлен.",
                                                      {"order_id": "order-FAKE", "status": "submitted"}))])
    ctx = Context(State(snapshot()))
    result = LLMRunner(fake, model="fake", model_version="test").run({"text": "Отправь заказ"}, ctx)
    assert result.technical_status == "error"
    assert result.action is None
    assert result.facts == {}
    assert "order-FAKE" not in result.text
    assert ctx.trace.llm[0]["error_code"] == "ungrounded_final_claim"


def test_does_not_accept_fabricated_existing_status_without_read_tool():
    fake = ScriptedClient([response(content=decision("act", "Заказ отправлен.",
                                                      {"order_id": "order-1", "status": "submitted"}))])
    result = LLMRunner(fake, model="fake", model_version="test").run({"text": "Статус?"}, Context(State(snapshot())))
    assert result.technical_status == "error"  # Нужна актуальная проверка статуса.


def test_can_report_verified_order_status_after_read():
    calls = [{"id": "read-status", "type": "function", "function": {
        "name": "get_pending_orders", "arguments": "{}"}}]
    fake = ScriptedClient([
        response(calls=calls),
        response(content=decision("act", "В сохранённом заказе указан статус submitted.",
                                  {"order_id": "order-1", "status": "submitted"})),
    ])
    ctx = Context(State(snapshot()))
    result = LLMRunner(fake, model="fake", model_version="test").run({"text": "Статус?"}, ctx)
    assert result.action == "act"
    assert result.facts == {"order_id": "order-1", "status": "submitted"}
    assert ctx.trace.llm[1]["error_code"] is None


def test_legacy_supplier_and_table_events_keep_original_kind():
    for kind in ("supplier_message", "scheduled_review", "table"):
        fake = ScriptedClient([response(content=decision("clarify", "Нужны уточнения.", status="waiting"))])
        result = LLMRunner(fake, model="fake", model_version="test").run(
            {"kind": kind, "text": "Исходный текст", "table": [{"a": 1}]}, Context(State(snapshot())))
        assert result.action == "clarify"
        last = json.loads(fake.calls[0]["messages"][-1]["content"])
        assert last["kind"] == kind
        assert last["data"]["text"] == "Исходный текст"
        assert last["data"]["table"] == [{"a": 1}]


def test_request_size_limit_prevents_llm_call():
    fake = ScriptedClient([])
    result = LLMRunner(fake, model="fake", model_version="test", max_request_chars=1000).run(
        {"text": "a" * 30000}, Context(State(snapshot())))
    assert result.technical_status == "limit_exceeded"
    assert fake.calls == []


def test_invalid_final_json_is_repaired_without_tool_calls():
    read = response(calls=[{"id": "read", "type": "function", "function": {
        "name": "get_pending_orders", "arguments": "{}"}}])
    fake = ScriptedClient([read, response(content="Вот статус заказа без JSON"),
                           response(content=decision(text="Есть заказ order-1.",
                                                     facts={"order_id": "order-1"}))])
    ctx = Context(State(snapshot()))
    result = LLMRunner(fake, model="fake", model_version="test").run({"text": "Статус?"}, ctx)
    assert result.technical_status == "completed"
    assert ctx.trace.llm[1]["error_code"] == "invalid_final_json"
    assert fake.calls[2]["tools"] == []
    assert ctx.tools.calls == [("get_pending_orders", {})]


def test_truncated_model_envelope_retries_without_executing_partial_calls():
    read = {"id": "read", "type": "function", "function": {
        "name": "get_pending_orders", "arguments": "{}"}}
    partial = response(calls=[{**read, "id": "truncated"}])
    partial["choices"][0]["finish_reason"] = "length"
    client = ScriptedClient([response(calls=[read]), partial, response(content=decision(
        text="Проверил заказ.", facts={"order_id": "order-1"}))])
    ctx = Context(State(snapshot()))
    result = LLMRunner(client, model="fake", model_version="test").run({"text": "Статус?"}, ctx)
    assert result.technical_status == "completed"
    assert ctx.tools.calls == [("get_pending_orders", {})]
    assert [entry["error_code"] for entry in ctx.trace.llm] == [None, "invalid_response_envelope", None]


def test_unknown_operation_never_reuses_model_success_claim():
    class UnknownTools(Tools):
        def __init__(self):
            super().__init__()
            self.schemas["dispatch_order"] = {"type": "object", "properties": {"submission_id": {"type": "string"}}}
        def call(self, name, arguments):
            self.calls.append((name, arguments))
            return ToolResult(ok=False, error=ToolError(
                code=ToolErrorCode.OPERATION_OUTCOME_UNKNOWN,
                message="Unknown", operation_outcome="unknown"))

    fake = ScriptedClient([response(calls=[{"id": "send", "type": "function", "function": {
        "name": "dispatch_order", "arguments": '{"submission_id": "abc"}'}}]),
        response(content=decision("act", "Заказ успешно отправлен."))])
    state = snapshot()
    state["business_events"]["rows"].append(
        {"id": "abc", "type": "order_submission", "details": {"state": "unknown"}})
    result = LLMRunner(fake, model="fake", model_version="test").run(
        {"text": "Отправь заказ"}, Context(State(state), tools=UnknownTools()))
    assert result.action == "escalate"
    assert "успешно отправлен" not in result.text


def test_missing_fact_is_rejected_even_with_successful_tool_call():
    calls = [{"id": "read", "type": "function", "function": {
        "name": "get_pending_orders", "arguments": "{}"}}]
    fake = ScriptedClient([
        response(calls=calls),
        response(content=decision("act", "Заказ проверен.", {"order_id": "invented-order"})),
    ])
    result = LLMRunner(fake, model="fake", model_version="test").run(
        {"text": "Статус?"}, Context(State(snapshot())))
    assert result.technical_status == "error"


def test_completion_token_cap_and_digitalocean_endpoint(monkeypatch):
    from llm.clients import ChatCompletionsClient, DIGITALOCEAN_ENDPOINT
    from io import BytesIO

    observed = {}
    class Response(BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()

    def fake_urlopen(req, timeout):
        observed["url"] = req.full_url
        observed["timeout"] = timeout
        observed["authorization"] = req.get_header("Authorization")
        observed["body"] = json.loads(req.data.decode("utf-8"))
        return Response(json.dumps(response(content=decision("clarify", "Уточните.", status="waiting"))).encode())

    monkeypatch.setattr("llm.clients.urlopen", fake_urlopen)
    client = ChatCompletionsClient(api_key="TEST_ONLY", max_completion_tokens=512, timeout_seconds=9)
    result = client.complete(model="deepseek-v4.1-flash-1017", messages=[{"role": "user", "content": "hi"}],
                             tools=[], temperature=0)
    assert result["choices"]
    assert observed["url"] == DIGITALOCEAN_ENDPOINT
    assert observed["authorization"] == "Bearer TEST_ONLY"
    assert observed["timeout"] == 9
    assert observed["body"]["model"] == "deepseek-v4.1-flash-1017"
    assert observed["body"]["max_completion_tokens"] == 512
    assert observed["body"].get("stream") is None


def test_streaming_reassembles_tool_calls_and_usage(monkeypatch):
    from llm.clients import ChatCompletionsClient
    from io import BytesIO

    events = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                                               "function": {"name": "get_pending_orders", "arguments": "{\""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]},
                      "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 12}},
    ]
    class Response(BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()

    def fake_urlopen(req, timeout):
        body = json.loads(req.data)
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        data = "".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n"
        return Response(data.encode("utf-8"))

    monkeypatch.setattr("llm.clients.urlopen", fake_urlopen)
    client = ChatCompletionsClient(api_key="TEST_ONLY", stream=True)
    result = client.complete(model="test", messages=[], tools=[], temperature=0)
    tool = result["choices"][0]["message"]["tool_calls"][0]
    assert tool["function"]["name"] == "get_pending_orders"
    assert tool["function"]["arguments"] == '{"}'
    assert result["usage"]["prompt_tokens"] == 50
    assert result["_restoka_metrics"]["ttft_seconds"] is not None


def test_false_order_id_in_text_rejected_after_unrelated_read():
    calls = [{"id": "read", "type": "function", "function": {
        "name": "get_pending_orders", "arguments": "{}"}}]
    fake = ScriptedClient([
        response(calls=calls),
        response(content=decision("act", "Заказ order-FAKE успешно отправлен.")),
    ])
    result = LLMRunner(fake, model="fake", model_version="test").run(
        {"text": "Статус?"}, Context(State(snapshot())))
    assert result.technical_status == "error"
    assert "order-FAKE" not in result.text


def test_refusal_about_payment_is_not_mistaken_for_completed_payment():
    fake = ScriptedClient([response(content=decision("refuse", "Оплата запрещена в Restoka."))])
    result = LLMRunner(fake, model="fake", model_version="test").run(
        {"text": "Оплати поставщику"}, Context(State(snapshot())))
    assert result.action == "refuse"


def test_out_of_domain_refusal_can_explain_its_decision_without_tools():
    model = ScriptedClient([response(content=decision(
        action="refuse", text="Этот запрос вне задач закупок.", facts={"reason": "out_of_domain"}))])
    result = LLMRunner(model, model="fake", model_version="test").run(
        {"text": "Напиши стихотворение."}, Context(State(snapshot())))
    assert result.action == "refuse"
    assert result.technical_status == "completed"
    assert result.facts["reason"] == "out_of_domain"
    assert result.technical_status == "completed"


def test_provider_timeout_has_distinct_technical_status():
    from llm.clients import ModelTimeoutError
    class TimeoutClient:
        def complete(self, **kwargs):
            raise ModelTimeoutError("network timed out")

    ctx = Context(State(snapshot()))
    result = LLMRunner(TimeoutClient(), model="fake", model_version="test").run(
        {"text": "Статус заказа?"}, ctx)
    assert result.technical_status == "timeout"
    assert ctx.trace.llm[0]["error_code"] == "provider_timeout"


def test_calc_material_need_accepts_verified_shortage_aliases():
    """Синонимы расчётных полей не превращают корректный ответ в ошибку."""
    class CalculationTools(Tools):
        def __init__(self):
            super().__init__()
            self.schemas["calc_material_need"] = {"type": "object", "properties": {}, "additionalProperties": False}

        def call(self, name, arguments):
            self.calls.append((name, arguments))
            return ToolResult(ok=True, data={
                "date_from": "2026-11-01", "date_to": "2026-11-10",
                "materials": [{"material": "краска для волос", "required_quantity": "12.000",
                               "inventory": {"quantity": "4.000"}, "deficit": "8.000",
                               "first_shortage_date": "2026-11-10"}],
            })

    tools = CalculationTools()
    call = [{"id": "calc-1", "type": "function", "function": {
        "name": "calc_material_need", "arguments": "{}"}}]
    model = ScriptedClient([response(calls=call), response(content=decision(
        "act", "Нужно 12, в наличии 4, дефицит 8 единиц краски.",
        {"material": "краска для волос", "needed": 12,
         "stock": 4, "shortage_quantity": 8,
         "shortage_date": "2026-11-10"}))])
    result = LLMRunner(model, model="fake", model_version="test").run(
        {"text": "Посчитай дефицит краски"}, Context(State(snapshot()), tools=tools))
    assert result.action == "act"
    assert result.facts["shortage_quantity"] == 8


def test_calc_material_need_rejects_wrong_shortage_and_reports_field_name():
    """Алиас не означает разрешение модели выдумывать количество."""
    class CalculationTools(Tools):
        def __init__(self):
            super().__init__()
            self.schemas["calc_material_need"] = {"type": "object", "properties": {}, "additionalProperties": False}

        def call(self, name, arguments):
            self.calls.append((name, arguments))
            return ToolResult(ok=True, data={"materials": [
                {"material": "краска для волос", "deficit": "8.000"}]})

    call = [{"id": "calc-1", "type": "function", "function": {
        "name": "calc_material_need", "arguments": "{}"}}]
    model = ScriptedClient([response(calls=call), response(content=decision(
        "act", "Дефицит 9 единиц.", {"shortage_quantity": 9}))])
    result = LLMRunner(model, model="fake", model_version="test",
                       max_verification_retries=0).run(
        {"text": "Посчитай дефицит краски"}, Context(State(snapshot()), tools=CalculationTools()))
    assert result.technical_status == "error"
    assert result.metadata["verification_reason"] == "unverified_fact_fields"
    assert result.metadata["unverified_fact_fields"] == ["shortage_quantity"]


def test_r001_inventory_date_and_no_pending_orders_verified_from_source():
    """Подтверждаем дату остатков и отсутствие заказов по конкретным данным."""
    class CalcTools(Tools):
        def __init__(self):
            super().__init__()
            self.schemas["calc_material_need"] = {"type": "object", "properties": {}, "additionalProperties": False}

        def call(self, name, arguments):
            self.calls.append((name, arguments))
            return ToolResult(ok=True, data={"materials": [{
                "material": "краска для волос", "deficit": "8.000",
                "inventory": {"quantity": "4.000", "as_of": "2026-11-01"},
            }]})

    state = snapshot()
    state["purchase_orders"] = {"count": 0, "rows": []}
    state["inventory"] = {"count": 1, "rows": [{
        "material": "краска для волос", "quantity": "4.000", "as_of": "2026-11-01"}]}
    call = [{"id": "calc-1", "type": "function", "function": {
        "name": "calc_material_need", "arguments": "{}"}}]
    model = ScriptedClient([response(calls=call), response(content=decision(
        "act", "К 10 ноября ожидается дефицит 8 единиц краски.",
        {"deficit": 8, "inventory_as_of": "2026-11-01", "pending_orders_count": 0}))])
    result = LLMRunner(model, model="fake", model_version="test").run(
        {"text": "Каких материалов не хватит?"}, Context(State(state), tools=CalcTools()))
    assert result.action == "act"
    assert result.facts["deficit"] == 8
    assert result.facts["pending_orders_count"] == 0


def test_pending_orders_count_not_trusted_when_snapshot_contains_orders():
    """Нулевое значение нельзя подтвердить пустым orders_to_review."""
    from llm.runner import _unverified_fact_fields
    state = snapshot()  # В базе есть submitted-заказ.
    successful_tools = [("calc_material_need", {"orders_to_review": [], "materials": [{"deficit": "8.000"}]})]
    assert _unverified_fact_fields({"pending_orders_count": 0}, state, successful_tools) == ["pending_orders_count"]
    assert _unverified_fact_fields({"pending_orders_count": 1}, state, successful_tools) == ["pending_orders_count"]


def test_pending_orders_count_not_trusted_without_order_table():
    """Если состояние заказов неизвестно, ноль нельзя утверждать."""
    from llm.runner import _unverified_fact_fields
    state = {"inventory": {"count": 1, "rows": [{"as_of": "2026-11-01"}]}}
    successful_tools = [("calc_material_need", {"materials": [{"deficit": "8.000"}]})]
    assert _unverified_fact_fields({"pending_orders_count": 0}, state, successful_tools) == ["pending_orders_count"]


def test_inventory_as_of_requires_inventory_source_and_correct_date():
    """Совпадение с посторонним as_of не доказывает дату остатков."""
    from llm.runner import _unverified_fact_fields
    state = {
        "purchase_orders": {"count": 0, "rows": []},
        "supplier_offers": {"count": 1, "rows": [{"as_of": "2026-11-09"}]},
        "inventory": {"count": 1, "rows": [{"as_of": "2026-11-01"}]},
    }
    successful_tools = [("calc_material_need", {"materials": [{"deficit": "8.000"}]})]
    assert _unverified_fact_fields({"inventory_as_of": "2026-11-09"}, state, successful_tools) == ["inventory_as_of"]
    assert _unverified_fact_fields({"inventory_as_of": "2026-11-01"}, state, successful_tools) == []
    assert _unverified_fact_fields({"pending_orders_count": 2}, state, successful_tools) == ["pending_orders_count"]


def test_r001_final_accepts_verified_period_and_zero_created_orders():
    """Финальный JSON реального типа R-001 не должен требовать ручных алиасов."""
    class CalculationTools(Tools):
        def __init__(self):
            super().__init__()
            self.schemas["calc_material_need"] = {"type": "object", "properties": {}, "additionalProperties": False}
        def call(self, name, arguments):
            self.calls.append((name, arguments))
            return ToolResult(ok=True, data={
                "date_from": "2026-11-01", "date_to": "2026-11-10",
                "materials": [{"material": "краска для волос", "deficit": "8.000"}],
            })

    state = snapshot()
    state["purchase_orders"] = {"count": 0, "rows": []}
    calls = [{"id": "calc-1", "type": "function", "function": {
        "name": "calc_material_need", "arguments": "{}"}}]
    model = ScriptedClient([response(calls=calls), response(content=decision(
        "act", "Дефицит краски 8 единиц, заказов не создавал.",
        {"deficit": 8, "from": "2026-11-01", "to": "2026-11-10", "orders_created": 0}))])
    result = LLMRunner(model, model="fake", model_version="test").run(
        {"text": "Каких материалов не хватит?"}, Context(State(state), tools=CalculationTools()))
    assert result.action == "act"
    assert result.facts["deficit"] == 8
    assert result.facts["orders_created"] == 0
    assert len(model.calls) == 2


def test_created_orders_is_derived_from_state_delta_not_ambiguous_counts():
    from llm.runner import _unverified_fact_fields
    before = snapshot()
    after = snapshot()
    after["purchase_orders"] = {"count": 2, "rows": [
        *before["purchase_orders"]["rows"], {"id": "order-2", "status": "draft"}]}
    evidence = [("get_pending_orders", {"count": 0, "orders": []})]
    assert _unverified_fact_fields({"orders_created": 1}, after, evidence,
                                   initial_snapshot=before) == []
    assert _unverified_fact_fields({"orders_created": 0}, after, evidence,
                                   initial_snapshot=before) == ["orders_created"]
    assert _unverified_fact_fields({"orders_created": 2}, after, evidence,
                                   initial_snapshot=before) == ["orders_created"]
    # Снимок должен быть полным: отдельный count не является доказательством.
    after["purchase_orders"]["count"] = 3
    assert _unverified_fact_fields({"orders_created": 1}, after, evidence,
                                   initial_snapshot=before) == ["orders_created"]


def test_untrusted_new_order_count_is_not_accepted_without_initial_snapshot():
    from llm.runner import _unverified_fact_fields
    state = snapshot()
    assert _unverified_fact_fields({"orders_created": 0}, state,
                                   [("get_pending_orders", {"count": 0})]) == ["orders_created"]


def test_wrong_period_dates_rejected_even_when_other_dates_exist():
    from llm.runner import _unverified_fact_fields
    state = snapshot()
    evidence = [("calc_material_need", {"date_from": "2026-11-01", "date_to": "2026-11-10",
                                         "materials": [{"deficit": "8.000"}]})]
    assert _unverified_fact_fields({"from": "2026-11-04"}, state, evidence) == ["from"]
    assert _unverified_fact_fields({"to": "2026-11-15"}, state, evidence) == ["to"]


def test_unverified_extra_field_gets_one_final_only_retry():
    calls = [{"id": "read", "type": "function", "function": {
        "name": "get_pending_orders", "arguments": "{}"}}]
    model = ScriptedClient([
        response(calls=calls),
        response(content=decision("act", "Есть заказ order-1.",
                                  {"order_id": "order-1", "strange_field": "неподтверждено"})),
        response(content=decision("act", "Есть заказ order-1.", {"order_id": "order-1"})),
    ])
    result = LLMRunner(model, model="fake", model_version="test").run(
        {"text": "Статус?"}, Context(State(snapshot())))
    assert result.action == "act"
    assert result.facts == {"order_id": "order-1"}
    assert len(model.calls) == 3
    assert model.calls[2]["tools"] == []
    assert "unverified_fact_fields" in model.calls[2]["messages"][-1]["content"]


def test_verification_retry_cannot_execute_tool_calls():
    calls = [{"id": "read", "type": "function", "function": {
        "name": "get_pending_orders", "arguments": "{}"}}]
    forbidden_on_retry = [{"id": "fake-send", "type": "function", "function": {
        "name": "dispatch_order", "arguments": '{"submission_id":"sid-1"}'}}]
    model = ScriptedClient([
        response(calls=calls),
        response(content=decision("act", "Есть заказ order-1.", {"invented": "bad"})),
        response(calls=forbidden_on_retry),
    ])
    tools = Tools()
    ctx = Context(State(snapshot()), tools=tools)
    result = LLMRunner(model, model="fake", model_version="test").run({"text": "Статус?"}, ctx)
    assert result.technical_status == "error"
    assert tools.calls == [("get_pending_orders", {})]
    assert model.calls[2]["tools"] == []


def test_unverified_fields_after_retry_fail_closed():
    calls = [{"id": "read", "type": "function", "function": {
        "name": "get_pending_orders", "arguments": "{}"}}]
    model = ScriptedClient([
        response(calls=calls),
        response(content=decision("act", "Есть заказ order-1.", {"invented": 10})),
        response(content=decision("act", "Есть заказ order-1.", {"invented": 10})),
    ])
    result = LLMRunner(model, model="fake", model_version="test").run(
        {"text": "Статус?"}, Context(State(snapshot())))
    assert result.technical_status == "error"
    assert result.metadata["unverified_fact_fields"] == ["invented"]
    assert result.metadata["verification_retries"] == 1


def test_comparison_passes_verified_terms_after_repeated_unsupported_facts():
    class ComparisonTools(Tools):
        def __init__(self):
            super().__init__()
            self.schemas["compare_suppliers"] = {"type": "object"}

        def call(self, name, arguments):
            self.calls.append((name, arguments))
            return ToolResult(ok=True, data={
                "material": "крем", "quantity": "5", "required_by": "2026-11-10",
                "recommended_offer_id": "offer-1",
                "offers": [{"offer": {"id": "offer-1", "supplier_id": "s1", "delivery_date": "2026-11-10"},
                            "total_rub": "2200", "suitability": "eligible"}],
            })

    tools = ComparisonTools()
    calls = [{"id": "compare", "type": "function", "function": {"name": "compare_suppliers", "arguments": "{}"}}]
    invalid = response(content=decision("act", "Сравнил предложения.", {
        "incoming_quantity": 99, "unit_price_rub": 20, "delivery_date": "2026-11-01",
    }))
    client = ScriptedClient([response(calls=calls), invalid, invalid])
    result = LLMRunner(client, model="fake", model_version="test").run(
        {"text": "Сравни предложения."}, Context(State(snapshot()), tools=tools))
    assert result.action == "act"
    assert result.metadata == {"recovery": "verified_supplier_comparison"}
    assert "incoming_quantity" not in result.facts
    assert "unit_price_rub" not in result.facts
    assert result.facts["chosen_supplier"] == "s1"
    assert result.facts["offers"][0]["total_rub"] == "2200"
    assert result.facts["offers"][0]["delivery_date"] == "2026-11-10"
    assert tools.calls == [("compare_suppliers", {})]


def test_pending_approval_status_recovers_from_unverified_historical_calculation():
    calls = [{"id": "read", "type": "function", "function": {"name": "get_pending_orders", "arguments": "{}"}}]
    state = snapshot()
    state["purchase_orders"]["rows"][0]["status"] = "pending_approval"
    state["approval_requests"]["rows"].append({"id": "approval-1", "order_id": "order-1", "status": "pending"})
    for action in ("act", "escalate"):
        invalid = response(content=decision(action, "Согласование ожидается.", {"order_id": "order-1", "need": 99}))
        client = ScriptedClient([response(calls=calls), invalid, invalid])
        result = LLMRunner(client, model="fake", model_version="test").run(
            {"text": "Управляющий согласовал заказ?"}, Context(State(state)))
        assert result.action == "act"
        assert "need" not in result.facts
        assert result.facts["order_status"] == "pending_approval"
        assert result.facts["approval_status"] == "pending"


def test_qualified_fact_must_match_the_named_tool_in_the_current_event():
    from llm.verification import _unverified_fact_fields

    key = "calc_material_need.required_quantity"
    current_calculation = {"materials": [{"material": "крем", "required_quantity": "14.000"}]}
    assert _unverified_fact_fields({key: 14}, snapshot(), [("calc_material_need", current_calculation)]) == []
    assert _unverified_fact_fields({key: 13}, snapshot(), [("calc_material_need", current_calculation)]) == [key]
    assert _unverified_fact_fields({key: 14}, snapshot(), [("get_inventory", current_calculation)]) == [key]


def test_clarification_recovers_only_for_a_verified_missing_offer_term():
    class ComparisonTools(Tools):
        def __init__(self, offers):
            super().__init__()
            self.schemas["compare_suppliers"] = {"type": "object"}
            self.offers = offers

        def call(self, name, arguments):
            self.calls.append((name, arguments))
            return ToolResult(ok=True, data={"material": "крем", "quantity": "5",
                                            "required_by": "2026-11-10", "offers": self.offers})

    missing_offer = {
        "offer": {"id": "offer-1", "supplier_id": "s1", "unit_price_rub": None},
        "commercial_suitability": "incomplete",
        "reasons": ["unknown_unit_price_rub"],
    }
    calls = [{"id": "compare", "type": "function", "function": {"name": "compare_suppliers", "arguments": "{}"}}]
    invalid = response(content=decision(
        "clarify", "Уточните цену.", {"missing_fields": ["unit_price_rub"], "inventory.quantity": 99}))
    second_offer = {**missing_offer, "offer": {**missing_offer["offer"], "id": "offer-2"}}
    for offers, expected in (([missing_offer], "completed"), ([missing_offer, second_offer], "error")):
        tools = ComparisonTools(offers)
        client = ScriptedClient([response(calls=calls), invalid, invalid])
        result = LLMRunner(client, model="fake", model_version="test").run(
            {"text": "Поставщик забыл цену. Оформляй?"}, Context(State(snapshot()), tools=tools))
        assert result.technical_status == expected
        if expected == "completed":
            assert result.action == "clarify"
            assert result.facts["missing_fields"] == ["unit_price_rub"]
            assert "inventory.quantity" not in result.facts
        assert tools.calls == [("compare_suppliers", {})]


def test_http_client_omits_tool_choice_when_tools_are_disabled(monkeypatch):
    from llm.clients import ChatCompletionsClient
    from io import BytesIO
    observed = {}

    class Response(BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()

    def fake_urlopen(req, timeout):
        observed.update(json.loads(req.data))
        return Response(json.dumps(response(content=decision("clarify", "Уточните.", status="waiting"))).encode())

    monkeypatch.setattr("llm.clients.urlopen", fake_urlopen)
    client = ChatCompletionsClient(api_key="TEST_ONLY")
    client.complete(model="test", messages=[{"role": "user", "content": "hi"}],
                    tools=[], temperature=0)
    assert "tools" not in observed
    assert "tool_choice" not in observed


def test_single_material_need_and_deficit_are_available_at_expected_facts_paths():
    """Потребность и дефицит доступны отдельно, даже если модель вложила их в materials."""
    class CalculationTools(Tools):
        def __init__(self):
            super().__init__()
            self.schemas["calc_material_need"] = {"type": "object", "properties": {}, "additionalProperties": False}

        def call(self, name, arguments):
            self.calls.append((name, arguments))
            return ToolResult(ok=True, data={
                "date_from": "2026-11-01", "date_to": "2026-11-10",
                "materials": [{"material": "краска для волос", "required_quantity": "12.000",
                               "deficit": "8.000"}],
            })

    tools = CalculationTools()
    call = [{"id": "calc", "type": "function", "function": {
        "name": "calc_material_need", "arguments": "{}"}}]
    client = ScriptedClient([
        response(calls=call),
        response(content=decision("act", "Нужно 12 единиц краски, не хватает 8.",
            {"materials": [{"material": "краска для волос", "required_quantity": "12.000",
                            "deficit": "8.000"}]})),
    ])
    result = LLMRunner(client, model="fake", model_version="test").run(
        {"text": "Сколько краски потребуется для выполнения запланированных услуг?"},
        Context(State(snapshot()), tools=tools))
    assert result.action == "act"
    assert result.process_status == "completed"
    assert result.facts["need"] == "12.000"
    assert result.facts["deficit"] == "8.000"
    assert result.facts["materials"][0]["deficit"] == "8.000"


def test_several_materials_do_not_get_invented_totals():
    """Разные материалы нельзя сводить к одному количеству без определённой единицы."""
    from llm.runner import _include_single_material_quantities

    facts = {"materials": [{"material": "краска", "deficit": "8"},
                           {"material": "шампунь", "deficit": "3"}]}
    data = {"materials": [{"material": "краска", "required_quantity": "12", "deficit": "8"},
                          {"material": "шампунь", "required_quantity": "5", "deficit": "3"}]}
    _include_single_material_quantities(facts, [("calc_material_need", data)])
    assert "need" not in facts
    assert "deficit" not in facts


def test_read_only_user_question_is_instructed_to_finish_without_unasked_purchase():
    """Модель получает явное правило не подменять расчёт предложением закупки."""
    client = ScriptedClient([response(content=decision("clarify"))])
    LLMRunner(client, model="fake", model_version="test").run(
        {"text": "Что нужно докупить?"}, Context(State(snapshot())))

    prompt = " ".join(client.calls[0]["messages"][0]["content"].split())
    assert "Distinguish a request for INFORMATION from a request to BUY" in prompt
    assert "action=act and process_status=completed" in prompt
    assert "Do not ask whether to place a purchase order" in prompt
    assert "inspect upcoming appointments and use the calculated needs" in prompt
    assert all(term not in prompt for term in ("send_review", "approved_draft", "process_supplier_reply",
                                               "proposed_order", "trusted scheduled wakeup"))


def test_workflow_prompts_are_selected_by_event_kind():
    cases = [
        ("time_tick", "Prepare the daily review", ("JSON body", "prepare_draft", "approved_draft")),
        ("scheduled_review", "JSON body", ("prepare_draft", "approved_draft")),
        ("scheduled_wakeup", "There is no user request", ("send_review", "approved_draft", "process_supplier_reply")),
        ("supplier_reply", "process_supplier_reply.changes", ("send_review", "prepare_draft", "approved_draft")),
        ("supplier_event", "Assess the reported supplier change", ("send_review", "prepare_draft")),
        ("manager_decision", "approved_draft", ("send_review", "Compare the offers in the attachment")),
        ("external_result", "Inspect the trusted external outcome", ("send_review", "approved_draft")),
    ]
    for kind, required, excluded in cases:
        client = ScriptedClient([response(content=decision("clarify"))])
        LLMRunner(client, model="fake", model_version="test").run(
            {"kind": kind, "data": {}}, Context(State(snapshot())))
        prompt = client.calls[0]["messages"][0]["content"]
        assert "FinalDecision JSON" in prompt, kind
        assert required in prompt, kind
        assert all(term not in prompt for term in excluded), kind


def test_structured_inputs_receive_their_specialized_instructions():
    quoted = "Поставщик предлагает 5 шт. по 100 ₽."
    events = [
        ({"kind": "supplier_message", "data": {"text": quoted}}, "Summarize only what that statement says."),
        ({"kind": "user_message", "data": {"text": quoted, "supplier_statement": quoted}},
         "Summarize only what that statement says."),
        ({"kind": "table", "data": {"format": "json_rows", "table": [{
            "offer_id": "of-1", "supplier_id": "s1", "material": "крем",
            "available_quantity": 10, "order_quantity": 5, "unit_price_rub": 100,
            "delivery_fee_rub": 0, "confirmed": True,
        }]}}, "Explain the validated attached_offer_comparison"),
    ]
    state = snapshot()
    state["suppliers"] = {"rows": [{"id": "s1", "approved": True}]}
    for event, required in events:
        client = ScriptedClient([response(content=decision("clarify"))])
        LLMRunner(client, model="fake", model_version="test").run(event, Context(State(state)))
        prompt = client.calls[0]["messages"][0]["content"]
        assert required in prompt
        assert all(term not in prompt for term in ("prepare_draft", "send_review", "approved_draft"))
        assert client.calls[0]["tools"] == []

    client = ScriptedClient([response(content=decision("clarify"))])
    LLMRunner(client, model="fake", model_version="test").run(
        {"kind": "user_message", "data": {"text": "Оформи закупку.", "proposed_order": {"material": "крем"}}},
        Context(State(state)))
    prompt = client.calls[0]["messages"][0]["content"]
    assert "For an attached proposed_order" in prompt
    assert "Distinguish a request for INFORMATION from a request to BUY" in prompt


def test_clarification_can_report_an_error_returned_by_a_tool():
    class MissingInventoryTools(Tools):
        def __init__(self):
            super().__init__()
            self.schemas["get_inventory"] = {"type": "object"}

        def call(self, name, arguments):
            return ToolResult(ok=False, error=ToolError(
                code=ToolErrorCode.MISSING_DATA, message="No snapshot", operation_outcome="not_started"))

    calls = [{"id": "inventory", "type": "function",
              "function": {"name": "get_inventory", "arguments": "{}"}}]
    for code, expected in (("missing_data", "completed"), ("timeout", "error")):
        client = ScriptedClient([response(calls=calls), response(content=decision(
            action="clarify", text="Уточните актуальные остатки.", facts={"error_code": code}))])
        result = LLMRunner(client, model="fake", model_version="test", max_verification_retries=0).run(
            {"text": "Покажи остатки."}, Context(State(snapshot()), tools=MissingInventoryTools()))
        assert result.technical_status == expected
        if expected == "completed":
            assert result.facts["missing_fields"] == ["inventory.quantity"]


def test_scheduled_review_body_uses_the_journal_matching_its_digest():
    from llm.runner import _scheduled_review_arguments

    arguments = {"report_date": "2026-11-01", "facts_digest": "current", "text": "Модель прислала текст"}
    source = {"report_date": "2026-11-01", "facts_digest": "current",
              "events": [{"id": "event-1", "type": "order_prepared", "date": "2026-11-01", "details": {}}]}
    evidence = [("get_daily_activity", source)]
    sent = _scheduled_review_arguments(arguments, evidence)
    assert sent["facts_digest"] == "current"
    assert json.loads(sent["text"]) == {
        "report_date": source["report_date"],
        "events": [{"id": "event-1", "type": "order_prepared", "date": "2026-11-01"}],
    }
    assert _scheduled_review_arguments(dict(arguments, facts_digest="other"), evidence)["text"] == arguments["text"]


def test_report_send_remains_confirmed_after_rejecting_the_models_final_facts():
    class ReviewTools(Tools):
        def __init__(self):
            super().__init__()
            self.schemas["send_review"] = {"type": "object"}

        def call(self, name, arguments):
            self.calls.append((name, arguments))
            return ToolResult(ok=True, data={"report_date": "2026-11-09", "event_id": "review-id", "status": "sent"})

    tools = ReviewTools()
    calls = [{"id": "review", "type": "function", "function": {"name": "send_review", "arguments": "{}"}}]
    client = ScriptedClient([
        response(calls=calls),
        response(content=decision(facts={"operation_id": "unrelated-purchase", "status": "delivered"})),
    ])
    result = LLMRunner(client, model="fake", model_version="test", max_verification_retries=0).run(
        {"kind": "time_tick", "data": {}}, Context(State(snapshot()), tools=tools))
    assert result.action == "act"
    assert result.technical_status == "completed"
    assert result.facts == {"report_date": "2026-11-09", "event_id": "review-id", "status": "sent"}
    assert tools.calls == [("send_review", {})]


def test_status_request_can_reconcile_only_an_already_attempted_submission():
    tools = Tools()
    tools.schemas["dispatch_order"] = {"type": "object"}
    state = snapshot()
    state["business_events"]["rows"].extend([
        {"id": "prepared", "type": "order_submission", "details": {"state": "prepared"}},
        {"id": "unknown", "type": "order_submission", "details": {"state": "unknown"}},
    ])
    model = ScriptedClient([response(calls=[
        {"id": identifier, "type": "function", "function": {
            "name": "dispatch_order", "arguments": json.dumps({"submission_id": identifier})}}
        for identifier in ("prepared", "unknown")
    ]), response(content=decision(action="clarify"))])
    history = History()
    result = LLMRunner(model, model="fake", model_version="test").run(
        {"text": "Только покажи статус заказа. Ничего не отправляй."},
        Context(State(state), tools=tools, history=history))
    assert result.technical_status == "completed"
    assert tools.calls == [("dispatch_order", {"submission_id": "unknown"})]
    blocked = next(entry.m for entry in history.saved if entry.m.tool_call_id == "prepared")
    assert json.loads(blocked.content)["error"]["operation_outcome"] == "not_started"


def test_purchase_request_can_send_its_new_submission_without_manual_grants():
    class PurchaseTools(Tools):
        def __init__(self):
            super().__init__()
            self.schemas.update({name: {"type": "object"} for name in ("create_order", "dispatch_order")})

        def call(self, name, arguments):
            self.calls.append((name, arguments))
            if name == "create_order":
                return ToolResult(ok=True, data={"submission_id": "new"})
            return ToolResult(ok=True, data={"id": "order-1", "status": "submitted"})

    def tool_call(name, identifier, arguments):
        return {"id": identifier, "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)}}

    state = snapshot()
    state["business_events"]["rows"].append(
        {"id": "older", "type": "order_submission", "details": {"state": "prepared"}})
    model = ScriptedClient([
        response(calls=[tool_call("create_order", "create", {})]),
        response(calls=[
            tool_call("dispatch_order", "send-old", {"submission_id": "older"}),
            tool_call("dispatch_order", "send-new", {"submission_id": "new"}),
        ]),
        response(content=decision()),
    ])
    tools = PurchaseTools()
    result = LLMRunner(model, model="fake", model_version="test").run(
        {"text": "Оформи закупку."}, Context(State(state), tools=tools))
    assert result.technical_status == "completed"
    assert tools.calls == [("create_order", {}), ("dispatch_order", {"submission_id": "new"})]
    assert not any(spec["function"]["name"] == "dispatch_order" for spec in model.calls[0]["tools"])
    assert any(spec["function"]["name"] == "dispatch_order" for spec in model.calls[1]["tools"])


def test_manager_decision_grants_writes_only_for_the_approved_order():
    from llm.permissions import tool_permissions

    state = snapshot()
    state["approval_requests"]["rows"] = [
        {"id": "approval-1", "order_id": "order-1", "status": "approved", "decision": "approved"},
        {"id": "approval-2", "order_id": "order-2", "status": "rejected", "decision": "rejected"},
    ]
    state["business_events"]["rows"].extend([
        {"id": "submission-1", "type": "order_submission",
         "details": {"state": "prepared", "order_id": "order-1"}},
        {"id": "submission-2", "type": "order_submission",
         "details": {"state": "prepared", "order_id": "order-2"}},
    ])
    decision_event = {"kind": "manager_decision", "data": {"approval_id": "approval-1"}}
    permissions = tool_permissions(decision_event, state)
    assert permissions.allows("create_order", {"order_id": "order-1"}, set())
    assert not permissions.allows("create_order", {"order_id": "order-2"}, set())
    assert permissions.allows("dispatch_order", {"submission_id": "submission-1"}, set())
    assert not permissions.allows("dispatch_order", {"submission_id": "submission-2"}, set())
    assert permissions.allows("dispatch_order", {"submission_id": "new"}, {"new"})

    decision_event["data"]["approval_id"] = "approval-2"
    permissions = tool_permissions(decision_event, state)
    assert not permissions.expose("create_order", set())
    assert not permissions.expose("dispatch_order", set())


def test_supplier_fact_uses_supplier_of_recommended_offer():
    class ComparisonTools(Tools):
        def __init__(self):
            super().__init__()
            self.schemas["compare_suppliers"] = {"type": "object", "properties": {}}

        def call(self, name, arguments):
            return ToolResult(ok=True, data={
                "offers": [
                    {"offer": {"id": "offer-a", "supplier_id": "first"}},
                    {"offer": {"id": "offer-b", "supplier_id": "second"}},
                ],
                "recommended_offer_id": "offer-b",
            })

    compare = response(calls=[{"id": "compare", "type": "function", "function": {
        "name": "compare_suppliers", "arguments": "{}"}}])
    for facts, expected in (({}, "second"), ({"chosen_supplier": "first"}, None)):
        client = ScriptedClient([compare, response(content=decision(facts=facts))])
        result = LLMRunner(client, model="fake", model_version="test",
                           max_verification_retries=0).run(
            {"text": "Сравни предложения"}, Context(State(snapshot()), tools=ComparisonTools()))
        assert result.facts.get("chosen_supplier") == expected
        assert result.technical_status == ("completed" if expected else "error")


def test_offer_sources_use_the_offers_read_and_cited_in_the_answer():
    from llm.verification import _include_offer_sources, _offer_source_ids, _unverified_fact_fields

    evidence = [("get_supplier_offers", [
        {"id": "offer-1"}, {"id": "offer-2"},
    ])]
    sources = _offer_source_ids(evidence)
    facts = {"offers": [{"offer_id": "offer-1"}]}
    _include_offer_sources(facts, sources)
    assert facts["source_ids"] == ["offer-1"]
    assert _unverified_fact_fields(facts, snapshot(), evidence, offer_source_ids=sources) == []
    invented = dict(facts, source_ids=["invented"])
    assert _unverified_fact_fields(invented, snapshot(), evidence, offer_source_ids=sources) == ["source_ids"]


def test_pending_approval_can_be_reported_without_claiming_dispatch():
    from llm.verification import _pending_approval_reason, _unsafe_action_claim, _unverified_fact_fields

    state = snapshot()
    state["purchase_orders"]["rows"][0]["status"] = "pending_approval"
    state["organization_policies"]["rows"] = [{"mandatory_human_approval": True}]
    state["approval_requests"]["rows"] = [{
        "id": "approval-1", "status": "pending", "order_id": "order-1",
    }]
    tools = [("prepare_draft", {"id": "order-1"}), ("request_approval", {"id": "approval-1"})]
    facts = {"reason": "approval_required_by_policy"}
    approval_reason = _pending_approval_reason(state, tools)
    assert _unverified_fact_fields(facts, state, tools, approval_reason=approval_reason) == []
    assert _pending_approval_reason(state, tools[:1]) is None
    assert _unverified_fact_fields(facts, state, tools[:1], approval_reason=None) == ["reason"]
    text = "Заявка на согласование создана. Заказ order-1 поставщику не отправлен."
    assert not _unsafe_action_claim(text, facts, tools, state)
    assert not _unsafe_action_claim("Заказ отправлен на согласование менеджеру.", facts,
                                   [*tools[:1], ("request_approval", {"order_id": "order-1"})], state)
    assert _unsafe_action_claim("Заказ отправлен на согласование менеджеру.", facts, tools[:1], state)
    assert _unsafe_action_claim(text, facts, tools[:1], state)
    assert _unsafe_action_claim(text.replace("не отправлен", "отправлен"), facts, tools, state)
    order_read = [("get_pending_orders", {"orders": state["purchase_orders"]["rows"]})]
    assert not _unsafe_action_claim(text, {}, order_read, state)
    assert _unverified_fact_fields({"approval_id": "approval-1"}, state, order_read) == []
    assert not _unsafe_action_claim("Черновик заказа уже подготовлен.", {}, order_read, state)
    assert _unsafe_action_claim("Я только что подготовил черновик.", {}, order_read, state)


def test_report_delivery_status_is_verified_against_the_message_receipt():
    from llm.runner import _unsafe_action_claim

    tools = [("send_review", {"status": "sent"})]
    assert not _unsafe_action_claim("Отчёт отправлен.", {"status": "sent"}, tools, snapshot())
    assert _unsafe_action_claim("Отчёт доставлен.", {"status": "delivered"}, tools, snapshot())
    assert _unsafe_action_claim("Отчёт доставлен.", {}, tools, snapshot())
    assert not _unsafe_action_claim("Отчёт не доставлен.", {}, tools, snapshot())
    assert _unsafe_action_claim("Заказ отправлен.", {"order_id": "order-1", "status": "sent"}, tools, snapshot())


def test_time_tick_uses_local_date_and_exposes_persisted_submission_id():
    state = snapshot()
    state["business_events"]["rows"].append({
        "id": "submission-1", "type": "order_submission",
        "details": {"state": "unknown", "order_id": "order-1",
                    "request": {"operation_id": "client-operation"}},
    })
    context = Context(State(state))
    context.now = datetime(2026, 11, 21, 21, 5, tzinfo=timezone.utc)
    client = ScriptedClient([response(content=decision(action="escalate"))])
    result = LLMRunner(client, model="fake", model_version="test").run(
        {"kind": "time_tick", "data": {}}, context)

    assert result.technical_status == "completed"
    messages = client.calls[0]["messages"]
    assert json.loads(messages[1]["content"])["previous_local_date"] == "2026-11-21"
    view = json.loads(messages[2]["content"].split(": ", 1)[1])["business"]
    assert view["unresolved_external_operations"] == [{
        "event_id": "submission-1", "kind": "order_submission", "state": "unknown",
        "operation_id": "client-operation", "submission_id": "submission-1", "order_id": "order-1",
    }]


def test_order_aliases_use_current_state_for_one_order_read_by_a_tool():
    from llm.runner import _unverified_fact_fields

    state = snapshot()
    orders = state["purchase_orders"]["rows"]
    orders.append({"id": "order-2", "eta": "2026-12-01", "status": "draft"})
    evidence = [("get_pending_orders", orders)]
    facts = {"order_id": "order-1", "order_eta": "2026-11-10",
             "order_status": "submitted", "order_quantity": 5, "order_total_rub": 2200}
    assert _unverified_fact_fields(facts, state, evidence) == []
    wrong_order = dict(facts, order_eta="2026-12-01")
    assert _unverified_fact_fields(wrong_order, state, evidence) == ["order_eta"]
    assert _unverified_fact_fields({"order_eta": "2026-11-10"}, state, evidence) == ["order_eta"]
    unrelated_read = [("get_inventory", [{"as_of": "2026-11-10"}])]
    assert _unverified_fact_fields(facts, state, unrelated_read) == [
        "order_eta", "order_quantity", "order_status", "order_total_rub",
    ]


def test_order_totals_are_verified_against_the_order_named_in_each_fact():
    from llm.runner import _unverified_fact_fields

    state = snapshot()
    orders = state["purchase_orders"]["rows"]
    orders.append({"id": "order-2", "status": "draft", "total_rub": "800.00"})
    evidence = [("get_pending_orders", orders)]

    assert _unverified_fact_fields({"order_id": "order-1", "total_rub": 2200}, state, evidence) == []
    assert _unverified_fact_fields({"order_id": "order-1", "total_rub": 800}, state, evidence) == [
        "total_rub",
    ]
    totals = {"orders": [{"id": "order-1", "total_rub": 2200},
                         {"id": "order-2", "total_rub": 800}]}
    assert _unverified_fact_fields(totals, state, evidence) == []
    totals["orders"][0]["total_rub"] = 800
    assert _unverified_fact_fields(totals, state, evidence) == ["total_rub"]

    offers = {"offers": [{"id": "order-1", "total_rub": 800}]}
    comparison = [("compare_suppliers", offers)]
    assert _unverified_fact_fields(offers, state, comparison) == []


def test_order_facts_keep_each_quantity_with_its_order_and_verify_id_lists():
    from llm.verification import _unverified_fact_fields

    state = snapshot()
    orders = state["purchase_orders"]["rows"]
    orders.append(dict(orders[0], id="order-2", quantity="17"))
    read = [("get_pending_orders", {"orders": orders})]

    facts = {"results": [{"order_id": "order-1", "quantity": "5"},
                        {"order_id": "order-2", "quantity": "17"}],
             "order_ids": ["order-1", "order-2"]}
    assert _unverified_fact_fields(facts, state, read) == []
    facts["results"][0]["quantity"] = "17"
    facts["order_ids"].append("order-999")
    assert _unverified_fact_fields(facts, state, read) == ["order_ids", "quantity"]


def test_currency_amount_in_text_must_match_verified_facts():
    read = {"id": "read", "type": "function", "function": {
        "name": "get_pending_orders", "arguments": "{}"}}
    facts = {"order_id": "order-1", "total_rub": "2200"}
    client = ScriptedClient([
        response(calls=[read]),
        response(content=decision(text="Сумма заказа 999999 рублей.", facts=facts)),
        response(content=decision(text="Сумма заказа 2 200,00 ₽.", facts=facts)),
    ])
    context = Context(State(snapshot()))
    result = LLMRunner(client, model="fake", model_version="test").run(
        {"text": "Какая сумма заказа?"}, context)

    assert result.technical_status == "completed"
    assert result.text == "Сумма заказа 2 200,00 ₽."
    assert context.trace.llm[1]["error_code"] == "ungrounded_final_claim"
    assert client.calls[-1]["tools"] == []


def test_approval_and_offer_facts_are_scoped_to_the_order_and_read_offer():
    from llm.runner import _unverified_fact_fields

    state = snapshot()
    state["approval_requests"]["rows"] = [
        {"id": "approval-1", "order_id": "order-1", "status": "approved", "decision": "approved"},
        {"id": "approval-2", "order_id": "order-2", "status": "rejected", "decision": "rejected"},
    ]
    read = [("get_pending_orders", state["purchase_orders"]["rows"]),
            ("get_supplier_offers", state["supplier_offers"]["rows"])]
    facts = {"order_id": "order-1", "offer_id": "offer-1", "offer_confirmed": True,
             "approval_request_id": "approval-1", "approval_status": "approved",
             "approval_decision": "approved"}
    assert _unverified_fact_fields(facts, state, read) == []
    unrelated = dict(facts, approval_request_id="approval-2", offer_confirmed=False)
    assert _unverified_fact_fields(unrelated, state, read) == [
        "approval_decision", "approval_request_id", "approval_status", "offer_confirmed",
    ]


def test_flat_approval_status_can_describe_the_explicitly_linked_approval():
    from llm.verification import _unverified_fact_fields

    state = snapshot()
    state["purchase_orders"]["rows"][0]["status"] = "pending_approval"
    first = {"id": "approval-1", "order_id": "order-1", "status": "pending"}
    other = {"id": "approval-2", "order_id": "order-2", "status": "approved"}
    state["approval_requests"]["rows"] = [first, other]
    evidence = [("request_approval", first)]
    facts = {"order_id": "order-1", "approval_id": "approval-1", "status": "pending"}

    assert _unverified_fact_fields(facts, state, evidence) == []
    assert _unverified_fact_fields(dict(facts, status="approved"), state, evidence) == ["status"]
    assert _unverified_fact_fields(dict(facts, approval_id="approval-2"), state, evidence) == [
        "approval_id", "status",
    ]


def test_confirmed_submission_uses_its_own_operation_id():
    from llm.verification import _confirmed_submission_facts, _unverified_fact_fields

    state = snapshot()
    order = state["purchase_orders"]["rows"][0]
    order["operation_id"] = "draft-op"
    details = {"state": "sent", "order_id": order["id"], "request": {"operation_id": "submission-op"}}
    state["business_events"]["rows"].append({
        "id": "submission-1", "type": "order_submission", "details": details,
    })
    evidence = [("create_order", {"submission_id": "submission-1"}), ("dispatch_order", order)]
    facts = _confirmed_submission_facts(state, evidence)

    assert facts["operation_id"] == "submission-op"
    assert _unverified_fact_fields(facts, state, evidence) == []
    assert _unverified_fact_fields(dict(facts, operation_id="draft-op"), state, evidence) == ["operation_id"]
    details["state"] = "unknown"
    assert not _confirmed_submission_facts(state, evidence)


def test_approval_request_can_be_reported_after_invalid_model_final():
    from llm.runner import _verified_effect_result

    state = snapshot()
    approval = {"id": "approval-1", "order_id": "order-1", "status": "pending"}
    state["approval_requests"]["rows"] = [approval]
    event = {"kind": "user_message", "data": {"text": "Оформи закупку."}}
    tools = [("request_approval", approval)]
    result = _verified_effect_result(event, state, tools, {}, "approval_required_by_policy")
    assert result.action == "escalate" and result.process_status == "waiting"
    assert result.facts == {"approval_id": "approval-1", "order_id": "order-1",
                            "approval_status": "pending", "reason": "approval_required_by_policy"}
    assert _verified_effect_result(event, state, [], {}, "approval_required_by_policy") is None


def test_provider_failure_after_saved_approval_reports_the_verified_effect():
    state = snapshot()
    approval = {"id": "approval-1", "order_id": "order-1", "status": "pending"}
    state["purchase_orders"]["rows"][0]["status"] = "pending_approval"
    state["organization_policies"] = {"rows": [{"mandatory_human_approval": True}]}

    class ApprovalTools(Tools):
        def __init__(self):
            super().__init__()
            self.schemas["request_approval"] = {"type": "object"}

        def call(self, name, arguments):
            self.calls.append((name, arguments))
            state["approval_requests"]["rows"].append(approval)
            return ToolResult(ok=True, data=approval)

    call = response(calls=[{"id": "approval", "type": "function", "function": {
        "name": "request_approval", "arguments": "{}"}}])
    ctx = Context(State(state), tools=ApprovalTools())
    result = LLMRunner(FailingClient([call]), model="fake", model_version="test").run({"text": "Оформи заказ."}, ctx)
    assert (result.technical_status, result.action) == ("completed", "escalate")
    assert result.facts["approval_id"] == approval["id"]
    assert ctx.trace.llm[-1]["error_code"] == "provider_error"
    assert ctx.tools.calls == [("request_approval", {})]


def test_proposal_can_report_mandatory_approval_before_request_is_created():
    request = {"text": "Подготовь заявку на оформление крема.", "proposed_order": {"material": "крем"}}
    for mandatory in (True, False):
        state = snapshot()
        state["organization_policies"] = {"rows": [{"mandatory_human_approval": mandatory}]}
        tools = Tools()
        client = ScriptedClient([response(content=decision("escalate", "Нужно решение менеджера.", status="waiting"))])
        result = LLMRunner(client, model="fake", model_version="test").run(request, Context(State(state), tools=tools))
        assert result.technical_status == "completed"
        assert result.facts.get("reason") == ("approval_required_by_policy" if mandatory else None)
        assert not tools.calls


def test_complete_proposal_keeps_approval_tool_available_under_mandatory_policy():
    state = snapshot()
    state["organization_policies"] = {"rows": [{"mandatory_human_approval": True}]}
    tools = Tools()
    tools.schemas["request_approval"] = {"type": "object", "properties": {}}
    client = ScriptedClient([response(content=decision("escalate", "Нужно решение менеджера.", status="waiting"))])
    runner = LLMRunner(client, model="fake", model_version="test")
    result = runner.run({"text": "Подготовь закупку крема.", "proposed_order": {
        "material": "крем", "supplier_id": "s1", "quantity": 5, "total_rub": 2200,
    }}, Context(State(state), tools=tools))

    assert result.technical_status == "completed"
    assert result.facts == {"reason": "approval_required_by_policy"}
    assert "request_approval" in {spec["function"]["name"] for spec in client.calls[0]["tools"]}


def test_verified_supplier_update_can_be_reported_after_invalid_model_facts():
    state = snapshot(delivery_date="2026-11-14")

    class SupplierTools(Tools):
        def __init__(self):
            super().__init__()
            self.schemas["process_supplier_reply"] = {"type": "object", "properties": {}}

        def call(self, name, arguments):
            self.calls.append((name, arguments))
            return ToolResult(ok=True, data=state["supplier_offers"]["rows"][0])

    call = response(calls=[{"id": "update", "type": "function", "function": {
        "name": "process_supplier_reply", "arguments": "{}"}}])
    unsupported = response(content=decision(
        text="Заказ отменён.", facts={"invented_status": "cancelled"}))
    for client in (ScriptedClient([call, unsupported, unsupported]), FailingClient([call])):
        tools = SupplierTools()
        result = LLMRunner(client, model="fake", model_version="test").run(
            {"kind": "supplier_reply", "data": {"offer_id": "offer-1"}},
            Context(State(state), tools=tools),
        )
        assert result.technical_status == "completed" and result.action == "escalate"
        assert result.metadata["recovery"] == "verified_supplier_update"
        assert "отменён" not in result.text
        assert result.facts["offer"]["delivery_date"] == "2026-11-14"
        assert result.facts["order_offer_discrepancies"][0]["stored_order_eta"] == "2026-11-10"
        assert tools.calls == [("process_supplier_reply", {})]


def test_repeated_inventory_outage_stops_the_workflow_without_starting_a_purchase():
    class InventoryTools(Tools):
        def __init__(self):
            super().__init__()
            for name in ("get_inventory", "calc_material_need", "prepare_draft"):
                self.schemas[name] = {"type": "object", "properties": {}}

        def call(self, name, arguments):
            self.calls.append((name, arguments))
            return ToolResult(ok=False, error=ToolError(
                code=ToolErrorCode.EXTERNAL_SERVICE_ERROR, message="Source unavailable",
                operation_outcome="not_started"))

    def call(name):
        return {"id": name, "type": "function", "function": {"name": name, "arguments": "{}"}}

    model = ScriptedClient([
        response(calls=[call("get_inventory")]),
        response(calls=[call("calc_material_need"), call("prepare_draft")]),
    ])
    tools, history = InventoryTools(), History()
    result = LLMRunner(model, model="fake", model_version="test").run(
        {"text": "Проверь остатки и подготовь закупку."},
        Context(State(snapshot()), history, tools))

    assert (result.action, result.process_status, result.technical_status) == ("escalate", "waiting", "completed")
    assert result.facts == {"reason": "inventory_unavailable"}
    assert tools.calls == [("get_inventory", {}), ("calc_material_need", {})]
    skipped = [entry.to_api() for entry in history.saved
               if entry.to_api().get("tool_call_id") == "prepare_draft"]
    assert json.loads(skipped[0]["content"])["error"]["operation_outcome"] == "not_started"


def test_approval_reason_uses_amount_of_the_order_linked_to_the_pending_request():
    from llm.verification import _pending_approval_reason, _unverified_fact_fields

    state = snapshot()
    state["purchase_orders"]["rows"][0]["status"] = "pending_approval"
    state["organization_policies"]["rows"] = [{"auto_order_limit_rub": "2000.00"}]
    state["approval_requests"]["rows"] = [{
        "id": "approval-1", "status": "pending", "order_id": "order-1",
    }]
    evidence = [("request_approval", {"id": "approval-1"})]
    facts = {"reason": "auto_limit_exceeded"}
    approval_reason = _pending_approval_reason(state, evidence)
    assert _unverified_fact_fields(facts, state, evidence, approval_reason=approval_reason) == []
    unrelated = [("request_approval", {"id": "other"})]
    assert _pending_approval_reason(state, unrelated) is None
    assert _unverified_fact_fields(facts, state, unrelated, approval_reason=None) == ["reason"]
    state["organization_policies"]["rows"][0]["auto_order_limit_rub"] = "2200.00"
    assert _pending_approval_reason(state, evidence) is None
    assert _unverified_fact_fields(facts, state, evidence, approval_reason=None) == ["reason"]


def test_registered_approval_reports_policy_reason_instead_of_model_label(monkeypatch):
    from unittest.mock import Mock

    from llm import runner, verification

    calculate_reason = Mock(wraps=verification._pending_approval_reason)
    monkeypatch.setattr(runner, "_pending_approval_reason", calculate_reason)
    monkeypatch.setattr(verification, "_pending_approval_reason", calculate_reason)

    state = snapshot()
    state["purchase_orders"]["rows"][0]["status"] = "pending_approval"
    state["organization_policies"]["rows"] = [{"auto_order_limit_rub": "2000.00"}]
    state["approval_requests"]["rows"] = [{
        "id": "approval-1", "status": "pending", "order_id": "order-1",
    }]

    class ApprovalTools(Tools):
        def __init__(self):
            super().__init__()
            self.schemas["request_approval"] = {"type": "object", "properties": {}}

        def call(self, name, arguments):
            return ToolResult(ok=True, data={"id": "approval-1", "order_id": "order-1"})

    model = ScriptedClient([response(calls=[{"id": "approval", "type": "function", "function": {
        "name": "request_approval", "arguments": "{}"}}]), response(content=decision(
            action="escalate", text="Заявка на согласование создана.",
            facts={"reason": "over_budget"}, status="waiting"))])
    result = LLMRunner(model, model="fake", model_version="test").run(
        {"text": "Оформи закупку."},
        Context(State(state), tools=ApprovalTools()))

    assert (result.technical_status, result.action, result.process_status) == ("completed", "escalate", "waiting")
    assert result.facts["reason"] == "auto_limit_exceeded"
    calculate_reason.assert_called_once()
    assert len(model.calls) == 2


def test_context_limit_discards_old_turn_without_splitting_tool_call_and_result():
    from database.history import ChatMessage, FunctionCall, ToolCall

    history = History()
    history.add(ChatMessage(role="user", content="Старый запрос"))
    history.add(ChatMessage(role="assistant", tool_calls=[
        ToolCall(id="old-call", function=FunctionCall(name="get_pending_orders", arguments="{}")),
    ]))
    history.add(ChatMessage(role="tool", tool_call_id="old-call", content="x" * 20_000))
    history.add(ChatMessage(role="assistant", content="Старый ответ"))
    history.add(ChatMessage(role="user", content="Недавний запрос"))
    history.add(ChatMessage(role="assistant", content="Недавний ответ"))

    model = ScriptedClient([response(content=decision(action="clarify", text="Какой заказ?"))])
    runner = LLMRunner(model, model="fake", model_version="test", max_request_chars=12_000)
    result = runner.run({"text": "Есть обновления?"}, Context(State(snapshot()), history))

    assert result.technical_status == "completed"
    messages = model.calls[0]["messages"]
    assert json.loads(messages[2]["content"].split(": ", 1)[1])["partial_history"] is True
    assert [message["content"] for message in messages[3:5]] == ["Недавний запрос", "Недавний ответ"]
    assert "Есть обновления?" in messages[-1]["content"]
    assert all(message["role"] != "tool" and not message.get("tool_calls") for message in messages)


def test_new_draft_after_supplier_update_requires_current_comparison_for_its_scope():
    from llm.runner import _needs_fresh_supplier_comparison

    state = snapshot()
    state["business_events"]["rows"].append({
        "type": "supplier_offer_updated",
        "details": {"result": {"ok": True, "data": {"material": "крем"}, "error": None}},
    })
    scope = {"material": "крем", "quantity": "12.000",
             "as_of": "2026-11-08", "required_by": "2026-11-10"}

    assert _needs_fresh_supplier_comparison(scope, state, [])
    assert _needs_fresh_supplier_comparison(scope, state, [
        ("compare_suppliers", dict(scope, quantity="10")),
    ])
    assert not _needs_fresh_supplier_comparison(scope, state, [
        ("compare_suppliers", dict(scope, quantity=12)),
    ])
    assert not _needs_fresh_supplier_comparison(dict(scope, material="гель"), state, [])


def test_scheduled_wakeup_only_exposes_procurement_with_automatic_policy():
    from llm.permissions import tool_permissions

    state = snapshot()
    event = {"kind": "scheduled_wakeup", "data": {"reason": "timer"}}
    state["organization_policies"]["rows"] = [{"automatic_ordering_enabled": False}]
    assert not tool_permissions(event, state).expose("prepare_draft", set())
    state["organization_policies"]["rows"][0]["automatic_ordering_enabled"] = True

    permissions = tool_permissions(event, state)
    assert permissions == tool_permissions({"kind": "scheduled_wakeup", "data": {}}, state)
    assert permissions.expose("prepare_draft", set())
    assert permissions.expose("create_order", set())
    assert not permissions.expose("send_review", set())
    assert not permissions.allows("dispatch_order", {"submission_id": "new"}, set())
    assert permissions.allows("dispatch_order", {"submission_id": "new"}, {"new"})

    review = tool_permissions({"kind": "time_tick", "data": {"reason": "Scheduled wakeup"}}, state)
    assert review.expose("send_review", set())
    assert not review.expose("prepare_draft", set())


def test_scheduled_wakeup_blocks_unscoped_draft_and_unrelated_existing_order():
    state = snapshot()
    state["organization_policies"]["rows"] = [{"automatic_ordering_enabled": True}]
    tools = Tools()
    tools.schemas.update({name: {"type": "object"} for name in ("prepare_draft", "create_order")})
    calls = [{"id": name, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
             for name, args in (
                 ("prepare_draft", {"as_of": "2026-11-10"}),
                 ("create_order", {"as_of": "2026-11-10", "order_id": "order-1"}),
             )]
    client = ScriptedClient([response(calls=calls), response(content=decision("escalate"))])
    result = LLMRunner(client, model="fake", model_version="test").run(
        {"kind": "scheduled_wakeup", "data": {"reason": "internal wakeup label"}},
        Context(State(state), tools=tools))
    assert result.action == "escalate"
    assert not tools.calls
    assert "There is no user request" in client.calls[0]["messages"][0]["content"]
    assert "internal wakeup label" not in json.dumps(client.calls[0]["messages"])


def test_partial_confirmation_uses_linked_order_quantity_and_deadline():
    from llm.context import partial_order_coverage

    state = snapshot()
    state["business_events"]["rows"].append({
        "type": "order_submission", "details": {
            "order_id": "order-1", "state": "sent",
            "request": {"offer_id": "offer-1", "required_by": "2026-11-12"},
        },
    })
    offer = {"id": "offer-1", "quantity": "20", "confirmed_quantity": "2", "confirmed": True}
    assert partial_order_coverage(state, offer) == {
        "order_id": "order-1", "unconfirmed_order_quantity": "3",
        "requires_order_review": True, "required_by": "2026-11-12",
    }
    assert partial_order_coverage(state, {**offer, "id": "unlinked"}) is None
    assert partial_order_coverage(state, {**offer, "confirmed_quantity": "5"}) is None
