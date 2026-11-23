"""Exercise the HTTP payload, LangGraph, trace, evaluator and local mock services."""

from io import BytesIO
import json
import os
from pathlib import Path

import pytest

from evaluation.evaluate_baseline_visible import evaluate
from evaluation.model_faults import FinalOutputFault
from evaluation.run_baseline_visible import adapt_case, configure_case_run
from harness import RunConfig, ScenarioRunner
from llm import ChatCompletionsClient, LLMRunner
from tests.test_llm_runner import decision, response


ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(Path(path).read_text())


def visible(case_id):
    return next(case for case in read(ROOT / "eval_visible.json") if case["id"] == case_id)


def additional(case_id):
    return read(ROOT / "evaluation" / "scenarios" / f"{case_id}.json")


def tool(name, **arguments):
    return response(calls=[{"id": f"call-{name}", "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False),
    }}])


def last_tool(payload):
    msg = payload["messages"][-1]
    assert msg["role"] == "tool"
    parsed = json.loads(msg["content"])
    assert parsed["ok"], parsed
    return parsed["data"]


@pytest.fixture
def run_eval(tmp_path, monkeypatch):
    database_url = os.environ.get("RESTOKA_DATABASE_URL")
    if not database_url:
        pytest.skip("Requires the local PostgreSQL test database")

    def run(case, replies):
        captured = []
        replies = iter(replies)

        def fake_urlopen(request, *, timeout):
            payload = json.loads(request.data)
            captured.append(payload)
            reply = next(replies)
            return BytesIO(json.dumps(reply(payload) if callable(reply) else reply).encode())

        monkeypatch.setattr("llm.clients.urlopen", fake_urlopen)
        client = FinalOutputFault(ChatCompletionsClient(
            api_key="local-test-placeholder", endpoint="http://127.0.0.1:1"))
        runner = LLMRunner(client, model="scripted-local", model_version="test")
        config = RunConfig(eval_version="test", runner_name="llm", runner_version="test", output_dir=tmp_path)
        case["ground_truth"]["payload_test_marker"] = "only-in-gold"
        case["checks"][0]["payload_test_marker"] = "only-in-check"
        case["legacy"] = {"expected_behavior": "only-in-legacy"}
        config, runner = configure_case_run(case, config, runner)
        record = ScenarioRunner(database_url=database_url, configure_adapters=client.configure).run(
            adapt_case(case), config, runner)
        assert next(replies, None) is None, record.get("technical_errors")
        serialized = json.dumps(captured, ensure_ascii=False)
        assert all(marker not in serialized for marker in ("only-in-gold", "only-in-check", "only-in-legacy",
                   "ground_truth", "scenario_expectations", "expected_behavior", "llm_structured_output",
                   "validation_error", "json_syntax", "only-in-step-expectation"))
        private = read(record["evaluator_path"])
        result = evaluate(case, record, private)
        return result, record, private, captured

    return run


def assert_pass(result):
    assert result["all_verified_pass"], [row for row in result["checks"] if row["status"] != "pass"]


def test_final_json_recovery_and_actual_http_context(run_eval):
    case = additional("EV-V-001")
    good = response(content=decision(
        text="Перчатки, 20 штук по 50 ₽, доставка 7 ноября за 300 ₽.",
        facts={"material": "перчатки", "quantity": 20, "unit": "pcs", "unit_price_rub": 50,
               "currency": "RUB", "delivery_fee_rub": 300, "delivery_date": "2026-11-07",
               "delivery_confirmed": True},
    ))
    scored, _, _, payloads = run_eval(case, [good, good])
    assert_pass(scored)
    assert (scored["supplier_extraction"]["tp"], scored["supplier_extraction"]["fn"]) == (8, 0)
    first, retry = payloads
    assert all("tools" not in item for item in payloads)
    business = json.loads(first["messages"][-1]["content"])
    assert business["data"] == {"supplier_statement": case["events"][0]["input"]["supplier_statement"]}
    assert "invalid_final_json" not in json.dumps(first)
    assert retry["messages"][-2] == {"role": "assistant", "content": "{invalid JSON"}
    assert json.loads(retry["messages"][-1]["content"])["reason"] == "invalid_final_json"


@pytest.mark.parametrize("case_id", ["R-140", "R-145"])
def test_persistent_json_fault_remains_in_adapter(run_eval, case_id):
    case = visible(case_id)
    scored, _, _, payloads = run_eval(case, [response(content=decision()), response(content=decision())])
    assert_pass(scored)
    assert json.loads(payloads[0]["messages"][-1]["content"])["data"]["text"] == case["input"]["text"]
    assert "invalid_final_json" not in json.dumps(payloads[0])
    assert "tools" not in payloads[1]


@pytest.mark.parametrize(("case_id", "fee"), [("R-046", 300), ("R-056", 0)])
def test_supplier_shipping_fee_is_scored_from_quoted_statement(run_eval, case_id, fee):
    case = visible(case_id)
    gold = case["supplier_evaluation"][0]["facts"]
    assert gold["delivery_fee_rub"] == fee
    scored, _, _, payloads = run_eval(case, [response(content=decision(
        text="Подтверждённые условия предложения поставщика.", facts=gold))])
    assert_pass(scored)
    assert scored["supplier_extraction"]["by_field"]["delivery_fee_rub"]["tp"] == 1
    assert scored["supplier_extraction"]["by_field"]["delivery_fee_rub"]["fn"] == 0
    first = json.loads(payloads[0]["messages"][-1]["content"])
    assert first["data"]["text"] == case["input"]["text"]
    assert "tools" not in payloads[0]


def test_legacy_multistep_payload_uses_messages_without_step_expectations(run_eval):
    case = visible("R-036")
    case["scenario_steps"][0]["expected_behavior"] = "only-in-step-expectation"
    reply = response(content=decision(action="clarify", status="waiting"))
    _, _, _, payloads = run_eval(case, [reply, reply])
    for payload, step in zip(payloads, case["scenario_steps"], strict=True):
        data = json.loads(payload["messages"][-1]["content"])["data"]
        assert data["supplier_statement"] in step["incoming"]


def test_r014_uses_confirmed_supplier_terms_for_a_draft_and_rejects_the_wrong_supplier(run_eval, tmp_path):
    case = visible("R-014")
    callback = case["events"][2]["supplier"]
    material = callback["confirmed_terms"]["material"]
    replies = [
        response(content=decision("clarify", "Уточните дату поставки.",
                                  {"delivery_confirmed": False}, "waiting")),
        response(content=decision("clarify", "Количество и цена указаны. Уточните дату доставки.",
                                  {"quantity": 8, "unit_price_rub": 440, "delivery_confirmed": False}, "waiting")),
        tool("get_supplier_reply", supplier_id=callback["supplier_id"], message_id=callback["message_id"]),
        tool("get_supplier_offers", offer_id=callback["offer_id"]),
        lambda payload: tool("process_supplier_reply", supplier_id=callback["supplier_id"],
                             message_id=callback["message_id"], changes=callback["changes"],
                             expected={key: last_tool(payload)[0][key] for key in callback["confirmed_terms"]}),
        response(content=decision(text="Подтверждённые условия поставщика сохранены.")),
        tool("compare_suppliers", material=material, quantity="8", as_of="2026-11-01", required_by="2026-11-10"),
        lambda payload: tool("prepare_draft", offer_id=last_tool(payload)["recommended_offer_id"],
                             material=material, quantity="8", as_of="2026-11-01", required_by="2026-11-10",
                             operation_id="r014-test-draft", idempotency_key="r014-test-draft-key"),
        response(content=decision(text="Подготовлен черновик заказа.")),
    ]
    scored, record, private, _ = run_eval(case, replies)
    assert_pass(scored)
    assert record["step_results"][-1]["mock_state"]["order_send_attempts"] == 0

    final_step = record["step_results"][-1]
    snapshot = read(final_step["snapshot_path"])
    draft, = snapshot["purchase_orders"]["rows"]
    assert (draft["supplier_id"], draft["status"]) == ("s1", "draft")
    draft["supplier_id"] = "s2"
    wrong_snapshot = tmp_path / "wrong_supplier.json"
    wrong_snapshot.write_text(json.dumps(snapshot))
    final_step["snapshot_path"] = str(wrong_snapshot)
    reevaluated = evaluate(case, record, private)
    assert not reevaluated["all_verified_pass"]
    assert any(check["op"] == "scenario_business_fact_eq" and check["expected"] == "s1"
               and check["status"] == "fail" for check in reevaluated["checks"])


def purchase_replies(*, material, offer_id, today, required_by, need_from, quantity, operation_id, dispatch=False):
    terms = {"offer_id": offer_id, "as_of": today, "required_by": required_by}
    replies = [
        tool("calc_material_need", material=material, date_from=need_from, date_to=required_by, as_of=today),
        tool("compare_suppliers", material=material, quantity=quantity, as_of=today, required_by=required_by),
        tool("prepare_draft", **terms, material=material, quantity=quantity, need_from=need_from,
             operation_id=f"{operation_id}-draft", idempotency_key=f"{operation_id}-draft-key"),
        lambda payload: tool("create_order", **terms, order_id=last_tool(payload)["id"],
                             operation_id=operation_id, idempotency_key=f"{operation_id}-key"),
    ]
    if dispatch:
        replies.append(lambda payload: tool("dispatch_order", submission_id=last_tool(payload)["submission_id"],
                                            as_of=today))
    return replies


def test_unknown_dispatch_can_be_checked_without_false_cancellation_or_resend(run_eval):
    case = visible("MS-V-009")
    today = "2026-11-21"
    submission_id = None

    def dispatch(payload):
        nonlocal submission_id
        submission_id = last_tool(payload)["submission_id"]
        return tool("dispatch_order", submission_id=submission_id, as_of=today)

    replies = purchase_replies(material="шампунь", offer_id="of-msv009-a", today=today,
                               required_by="2026-12-01", need_from=today, quantity="10",
                               operation_id="test-cancel-unknown")
    waiting = response(content=decision("escalate", "Не могу подтвердить, что заказ отменён. Нужен управляющий.",
                                        status="waiting"))
    replies += [dispatch, waiting,
                lambda _: tool("dispatch_order", submission_id=submission_id, as_of=today),
                waiting, waiting,
                lambda _: tool("dispatch_order", submission_id=submission_id, as_of=today), waiting]
    scored, record, private, _ = run_eval(case, replies)
    assert_pass(scored)
    assert [step["mock_state"]["order_send_attempts"] for step in record["step_results"]] == [1] * 4

    private["steps"][-1]["result"]["text"] = "Я отменил заказ. Статус отправки неизвестен."
    rescored = evaluate(case, record, private)
    assert not rescored["all_verified_pass"]
    assert any(c["op"] == "cancellation_text_safety" and c["status"] == "fail" for c in rescored["checks"])


def test_scenario_budget_profile_stops_second_event_without_model_calls(run_eval):
    case = visible("MS-V-010")
    scored, record, private, requests = run_eval(case, [])
    assert_pass(scored)
    assert scored["technical_status"] == "limit_exceeded"
    assert record["runner"] == "controlled_budget" and requests == []
    assert record["steps"] == 1 and record["tool_calls"] == 0

    record["technical_errors"] = [{"stage": "cleanup", "code": "cleanup_error"}]
    assert not evaluate(case, record, private)["all_verified_pass"]


def test_r144_recovers_real_operation_with_zero_external_sends(run_eval, tmp_path):
    case = visible("R-144")
    operation_id = "test-dynamic-order-operation"
    replies = purchase_replies(material="масло массажное", offer_id="of144a", today="2026-11-01",
                               required_by="2026-11-10", need_from="2026-11-10", quantity="2",
                               operation_id=operation_id)
    scored, record, private, _ = run_eval(case, replies)
    assert_pass(scored)
    assert scored["contract_status"] == "pass"
    step = record["step_results"][0]
    assert step["mock_state"]["order_send_attempts"] == 0
    assert private["steps"][0]["result"]["facts"]["operation_id"] == operation_id
    trace = read(step["trace_path"])
    recovery = next(span for span in trace["spans"] if span["operation"] == "local_submission_reconciled")
    recovery["result"]["operation_id"] = "a-different-operation"
    bad_trace = tmp_path / "unrelated-recovery.json"
    bad_trace.write_text(json.dumps(trace))
    step["trace_path"] = str(bad_trace)
    reevaluated = evaluate(case, record, private)
    assert next(check["status"] for check in reevaluated["checks"]
                if check["op"] == "local_submission_recovery") == "fail"


def test_precommit_timeout_keeps_the_draft_without_sending(run_eval):
    case = visible("R-020")
    replies = purchase_replies(material="маски косметические", offer_id="of20a", today="2026-11-01",
                               required_by="2026-11-10", need_from="2026-11-01", quantity="8",
                               operation_id="test-precommit-timeout")
    scored, record, private, _ = run_eval(case, replies)
    assert_pass(scored)
    assert private["steps"][0]["result"]["facts"]["reason"] == "order_creation_timeout"
    assert record["step_results"][0]["mock_state"]["order_send_attempts"] == 0
    assert [order["status"] for order in record["changes"]["orders_created"]] == ["draft"]


@pytest.mark.parametrize("case_id", ["R-114", "R-117", "R-120"])
def test_incomplete_proposal_reports_required_approval_without_claiming_a_request(run_eval, case_id):
    case = visible(case_id)
    replies = [
        tool("get_organization_policy"),
        response(content=decision(action="escalate", status="waiting",
            text="Заявка уже ожидает согласования.", facts={"inventory.quantity": "2.000"})),
    ]
    scored, record, private, requests = run_eval(case, replies)
    assert_pass(scored)
    result = private["steps"][0]["result"]
    assert result["process_status"] == "waiting"
    assert result["facts"] == {"reason": "approval_required_by_policy", "missing_fields": ["quantity"]}
    assert "Уточните количество" in result["text"]
    assert "уже ожидает" not in result["text"]
    assert not record["changes"]["orders_created"] and not record["changes"]["approvals_created"]
    assert record["step_results"][0]["mock_state"]["order_send_attempts"] == 0
    assert all(not {"prepare_draft", "request_approval", "create_order", "dispatch_order"}
               & {spec["function"]["name"] for spec in request["tools"]}
               for request in requests)


def test_review_keeps_saved_approval_when_the_provider_fails_to_report_it(run_eval):
    case = visible("MS-V-006")
    approval_id = "test-saved-manager-approval"
    order_id = None
    report_date = "2026-11-21"
    replies = purchase_replies(material="ватные диски", offer_id="of-msv006-a", today=report_date,
                               required_by="2026-11-30", need_from=report_date, quantity="15",
                               operation_id="test-approval-recovery")[:-1]

    def request_approval(payload):
        nonlocal order_id
        order_id = last_tool(payload)["id"]
        return tool("request_approval", request_id=approval_id, order_id=order_id,
                    reason="Сумма превышает лимит автоматической закупки.")

    def disconnected(_payload):
        raise OSError("simulated provider failure after the tool committed")

    def status_reply(_payload):
        return response(content=decision(action="escalate", status="waiting",
            text="Согласование ожидается.", facts={"order_id": order_id, "required_quantity": 99}))

    replies += [
        request_approval,
        disconnected,
        tool("get_daily_activity", report_date=report_date),
        lambda payload: tool("send_review", report_date=report_date, operation_id="test-approval-review",
                             facts_digest=last_tool(payload)["facts_digest"],
                             text=f"Закупка ожидает согласования.\n{approval_id} = pending"),
        response(content=decision(text="Ежедневный отчёт отправлен.")),
        lambda _payload: tool("get_pending_orders", order_id=order_id, statuses=["pending_approval"]),
        status_reply,
        status_reply,
    ]
    scored, record, private, _ = run_eval(case, replies)
    assert_pass(scored)
    assert private["steps"][0]["result"]["metadata"]["recovery"] == "verified_approval"
    assert private["steps"][-1]["result"]["facts"]["approval_status"] == "pending"
    assert len(record["changes"]["approvals_created"]) == 1
    assert record["step_results"][-1]["mock_state"]["order_send_attempts"] == 0


def test_ms_v008_partial_delivery_ends_in_persisted_manager_review(run_eval):
    case = visible("MS-V-008")
    replies = purchase_replies(material="краска для волос", offer_id="of-msv008-a", today="2026-11-24",
                               required_by="2026-11-25", need_from="2026-11-24", quantity="8",
                               operation_id="test-wakeup-operation", dispatch=True)
    callback = case["events"][1]["supplier"]
    expected = {**callback["confirmed_terms"], "confirmed_quantity": None}
    review_request_id = None

    def compare_remaining(payload):
        nonlocal review_request_id
        coverage = last_tool(payload)["order_coverage"]
        review_request_id = coverage["review_request_id"]
        assert "request_approval" not in {spec["function"]["name"] for spec in payload["tools"]}
        return tool("compare_suppliers", material=expected["material"], quantity=coverage["unconfirmed_order_quantity"],
                    as_of="2026-11-24", required_by=coverage["required_by"])

    def request_review(payload):
        names = {spec["function"]["name"] for spec in payload["tools"]}
        assert "request_approval" in names and "create_order" not in names
        assert last_tool(payload)["recommended_offer_id"] == "of-msv008-b"
        return tool("request_approval", request_id=review_request_id, order_id=None,
                    reason="Проверить непокрытый остаток и альтернативу s2 по частично подтверждённому заказу.")

    replies += [
        response(content=decision(text="Заказ отправлен поставщику.")),
        tool("get_supplier_reply", supplier_id=callback["supplier_id"], message_id=callback["message_id"]),
        tool("compare_suppliers", material=expected["material"], quantity="5",
             as_of="2026-11-24", required_by="2026-11-25"),
        tool("process_supplier_reply", supplier_id=callback["supplier_id"], message_id=callback["message_id"],
             expected=expected, changes=callback["changes"]),
        compare_remaining,
        request_review,
        response(content=decision(action="escalate", status="waiting", text="Заявка на согласование создана.",
                                  facts={"offer_id": callback["offer_id"], "confirmed_quantity": 3,
                                         "unconfirmed_order_quantity": 5, "requires_order_review": True,
                                         "alternative_offer": {"supplier_id": "s2", "total_rub": 2550}})),
    ]
    scored, record, private, _ = run_eval(case, replies)
    assert_pass(scored)
    assert private["steps"][-1]["result"]["process_status"] == "waiting"
    assert [step["mock_state"]["order_send_attempts"] for step in record["step_results"]] == [1, 1]
    assert len(record["changes"]["orders_created"]) == len(record["changes"]["approvals_created"]) == 1
    assert record["changes"]["approvals_created"][0]["id"] == review_request_id
