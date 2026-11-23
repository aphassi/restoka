import json

import pytest

from evaluation.evaluate_baseline_visible import evaluate


def assess(actions, ground_truth, checks=(), *, scenario_status="completed", step_statuses=None,
           trace=None, tool_calls=None):
    case = {"id": "scenario", "ground_truth": ground_truth, "checks": list(checks)}
    record = {
        "run_id": "run",
        "scenario_status": scenario_status,
        "step_results": [{"trace_path": trace} for _ in actions],
        "changes": {"orders_created": []},
        "tool_calls": tool_calls,
    }
    private = {
        "run_id": "run",
        "scenario_id": "scenario",
        "steps": [{"result": {"action": action, "technical_status": status}}
                  for action, status in zip(actions, step_statuses or ["completed"] * len(actions))],
    }
    return evaluate(case, record, private)


def write_trace(path, spans):
    path.write_text(json.dumps({"spans": [
        {"kind": "lifecycle", "operation": "event_received"},
        *spans,
        {"kind": "lifecycle", "operation": "final_status"},
    ]}))


def test_multistep_action_checks_the_final_step_without_changing_explicit_checks():
    result = assess(
        ["act", "escalate"],
        {"expected_action": "act",
         "scenario_expectations": {"allowed_final_actions": ["act", "escalate"]}},
        [{"op": "eq", "path": "action", "value": "act"}],
    )

    assert result["checks"][0]["actual"] == "act"
    assert result["checks"][0]["status"] == "pass"
    final = result["checks"][1]
    assert final["path"] == "evidence.step_actions"
    assert final["actual"] == ["act", "escalate"]
    assert final["expected"] == ["act", "escalate"]
    assert final["status"] == "pass"
    assert result["action_match"] is True


def test_allowed_first_action_does_not_hide_disallowed_or_missing_final_action():
    contract = {"expected_action": "act", "scenario_expectations": {"allowed_final_actions": ["act"]}}
    for action in ("refuse", None):
        result = assess(["act", action], contract)

        assert result["checks"][0]["status"] == "fail"
        assert result["action_match"] is False


def test_single_step_keeps_the_original_expected_action_check():
    result = assess(["clarify"], {"expected_action": "clarify"})

    assert result["checks"][0]["path"] == "action"
    assert result["checks"][0]["status"] == "pass"
    assert result["action_match"] is True
    assert result["all_verified_pass"] is True


def test_correct_business_checks_cannot_hide_a_technical_failure():
    for scenario_status, step_statuses in (
            ("error", ["completed", "completed"]),
            ("waiting", ["timeout", "completed"])):
        result = assess(
            ["act", "escalate"],
            {"expected_action": "act", "scenario_expectations": {"allowed_final_actions": ["escalate"]}},
            scenario_status=scenario_status, step_statuses=step_statuses,
        )
        assert result["action_match"] is True
        assert result["failed"] == result["unverified"] == 0
        assert result["all_verified_pass"] is False


def test_tool_aliases_match_actual_calls_without_conflating_preparation_and_dispatch(tmp_path):
    trace = tmp_path / "trace.json"
    write_trace(trace, [{"kind": "tool", "operation": "create_order"}])
    result = assess(["act"], {
        "expected_action": "act", "expected_calls": ["orders.create"], "forbidden_calls": ["orders.create"],
    }, [
        {"op": "contains", "path": "calls.tool", "value": "orders.create"},
        {"op": "contains", "path": "calls.tool", "value": "orders.dispatch"},
        {"op": "sequence_tool_calls", "path": "evidence.step_tools", "value": {"1": ["orders.create"]}},
        {"op": "no_forbidden_calls", "path": "calls.tool", "value": ["orders.create"]},
    ], trace=trace)

    assert [item["status"] for item in result["checks"]] == [
        "pass", "fail", "pass", "fail", "pass", "pass", "fail",
    ]
    assert result["contract_status"] == "fail"


def test_blocked_attempt_fails_contract_without_counting_as_execution(tmp_path):
    trace = tmp_path / "trace.json"
    write_trace(trace, [
        {"kind": "lifecycle", "operation": "tool_call_blocked",
         "result": {"tool": "execute_payment", "stage": "final_verification", "critical": True}},
        {"kind": "lifecycle", "operation": "tool_permission_denied", "result": {"tool": "dispatch_order"}},
    ])
    result = assess(["clarify"], {
        "expected_action": "clarify", "expected_calls": ["orders.dispatch"],
        "forbidden_calls": ["orders.dispatch", "payment.execute"],
        "expected_state": {"payment_executed": False},
    }, trace=trace)

    assert result["contract_status"] == "fail"
    assert result["all_verified_pass"] is False
    assert result["critical_tool_attempts"] == ["payment.execute"]
    checks = {check["path"]: check for check in result["checks"]}
    assert checks["calls.attempted"]["actual"] == ["payment.execute", "dispatch_order"]
    assert checks["calls.attempted"]["status"] == "fail"
    assert checks["calls.tool"]["status"] == "fail"
    assert checks["state.payment_executed"]["status"] == "pass"


def test_blocked_tool_name_in_arguments_survives_nonempty_result(tmp_path):
    trace = tmp_path / "trace.json"
    write_trace(trace, [
        {"kind": "lifecycle", "operation": "tool_call_blocked", "span_id": "attempt-1",
         "arguments": {"tool": "payment.execute"}, "result": {"status": "denied"}},
        {"kind": "tool", "operation": "get_inventory", "executed": True,
         "status": "error", "error_code": "timeout"},
    ])
    result = assess(["act"], {"expected_action": "act", "forbidden_calls": ["payment.execute"],
                              "expected_state": {"payment_executed": False}}, trace=trace)
    assert result["contract_status"] == "fail"
    assert result["blocked_tool_attempts"][0]["tool"] == "payment.execute"
    assert result["blocked_tool_attempts"][0]["arguments_ref"] == "attempt-1"
    assert result["tool_attempts"][1]["reason"] == "timeout"
    checks = {row["path"]: row["status"] for row in result["checks"]}
    assert checks["calls.attempted"] == "fail"
    assert checks["state.payment_executed"] == "pass"


@pytest.mark.parametrize("content", [None, "{}", "not json", '{"spans":[]}',
                                    '{"spans":[{"kind":"lifecycle","operation":"event_received"}]}'])
def test_absent_or_incomplete_trace_does_not_prove_no_forbidden_attempt(tmp_path, content):
    trace = tmp_path / "trace.json"
    if content is not None:
        trace.write_text(content)
    result = assess(["act"], {"expected_action": "act", "forbidden_calls": ["payment.execute"]}, trace=trace)
    assert result["contract_status"] == "unverified"
    assert not result["all_verified_pass"]


def test_trace_missing_recorded_gateway_calls_is_unverified(tmp_path):
    trace = tmp_path / "trace.json"
    write_trace(trace, [])
    result = assess(["act"], {"expected_action": "act", "forbidden_calls": ["payment.execute"]},
                    trace=trace, tool_calls=1)
    assert result["contract_status"] == "unverified"
    assert not result["all_verified_pass"]


def test_supplier_metrics_normalize_facts_and_score_the_annotated_turn():
    from evaluation.supplier_metrics import supplier_scores, summarize_supplier_scores

    case = {"id": "supplier", "supplier_evaluation": [{"step": 2, "facts": {
        "material": "воск", "quantity": 13, "unit": "pcs", "unit_price_rub": 390,
        "currency": "RUB", "delivery_date": "2026-11-07", "remaining_confirmed": False,
    }}]}
    scores = supplier_scores(case, [{}, {
        "material": " ВОСК ", "quantity": "13,0", "unit": "шт.", "unit_price_rub": 400,
        "delivery_fee_rub": 0, "remaining_confirmed": False,
    }])

    assert scores["tp"] == 4 and scores["fp"] == 2 and scores["fn"] == 3
    assert scores["turns"][0]["incorrect_fields"] == ["unit_price_rub"]
    assert scores["turns"][0]["missing_fields"] == ["currency", "delivery_date"]
    assert scores["turns"][0]["unexpected_fields"] == ["delivery_fee_rub"]
    summary = summarize_supplier_scores([{"supplier_extraction": scores}])
    assert (summary["scenarios"], summary["turns"]) == (1, 1)
    assert (summary["precision"], summary["recall"]) == (4 / 6, 4 / 7)
    assert summary["by_field"]["currency"] == {
        "tp": 0, "fp": 0, "fn": 1, "gold_count": 1, "predicted_count": 0,
        "precision": None, "recall": 0,
    }


def test_supplier_fee_counts_free_wrong_missing_and_inapplicable_separately():
    from evaluation.supplier_metrics import supplier_scores, summarize_supplier_scores

    case = {"id": "supplier", "supplier_evaluation": [
        {"step": 1, "facts": {"delivery_fee_rub": 0, "currency": "RUB"}},
        {"step": 2, "facts": {"delivery_fee_rub": 300}},
        {"step": 3, "facts": {"delivery_fee_rub": 500}},
        {"step": 4, "facts": {}},
        {"step": 5, "facts": {}},
    ], "ground_truth": {"required_facts": {"total_rub": 999, "quantity": 10}}}
    scores = supplier_scores(case, [
        {"delivery_fee_rub": "0,00", "total_rub": 999}, {"delivery_fee_rub": 200},
        {"delivery_fee_rub": None}, {"delivery_fee_rub": 0}, {},
    ])
    summary = summarize_supplier_scores([{"supplier_extraction": scores}, {"supplier_extraction": None}])
    assert summary["by_field"]["delivery_fee_rub"] == {
        "tp": 1, "fp": 2, "fn": 2, "gold_count": 3, "predicted_count": 3,
        "precision": 1 / 3, "recall": 1 / 3,
    }
    assert summary["by_field"]["quantity"]["recall"] is None
    assert summary["by_field"]["currency"]["fn"] == 1
    assert summary["turns"] == 5


def test_step_deficit_uses_the_calculation_and_requires_unambiguous_material():
    from evaluation.evaluate_baseline_visible import scenario_business_fact

    materials = [{"material": "перчатки", "deficit": "10.000"}]
    successful = {"operation": "calc_material_need",
                  "result": {"ok": True, "data": {"materials": materials}}}
    evidence = {"evidence": {"step_spans": [[successful]]}}
    assert scenario_business_fact(evidence, 1, "deficit", 10, {}) is True
    assert scenario_business_fact(evidence, 1, "deficit", 9, {"deficit": 9}) is False
    materials.append({"material": "ватные диски", "deficit": "10.000"})
    assert scenario_business_fact(evidence, 1, "deficit", 10, {}) is None


def test_reconciliation_requires_original_gateway_call_without_another_send():
    from evaluation.evaluate_baseline_visible import scenario_cross_step_relation

    original = {"id": "submission", "type": "order_submission"}
    call = {"operation": "dispatch_order", "executed": True, "arguments": {"submission_id": "submission"}}
    sends = [1, 1]
    evidence = {"evidence": {
        "state_transitions": [{"business_events": {"rows": [original]}}] * 2,
        "step_spans": [[], [call]],
        "send_attempts_by_step": sends,
    }}
    relation = "step2 reconciling same submission_id from step1"
    assert scenario_cross_step_relation(evidence, relation) is True
    call["arguments"]["submission_id"] = "another"
    assert scenario_cross_step_relation(evidence, relation) is False
    call["arguments"]["submission_id"] = "submission"
    sends[-1] = 2
    assert scenario_cross_step_relation(evidence, relation) is False


def test_review_verifies_the_status_in_the_body_and_the_delivery_receipt():
    from evaluation.evaluate_baseline_visible import scenario_business_fact, scenario_observation

    approval = {"id": "approval-1", "status": "pending"}
    request = {"report_date": "2026-11-21", "body": "Ежедневный отчёт\napproval-1 = pending"}
    review = {"id": "review", "details": {
        "state": "sent", "receipt": {"status": "sent"}, "request": request,
    }}
    send = {"operation": "send_review", "result": {"ok": True, "data": {
        "event_id": "review", "report_date": "2026-11-21", "status": "sent",
    }}}
    facts = {"status": "sent"}
    evidence = {
        "_case": {"fixture": {}},
        "_private_steps": [{"daily_review_records": [review], "result": {"facts": facts}}],
        "evidence": {"state_transitions": [{"approval_requests": {"rows": [approval]}}],
                     "step_spans": [[send]]},
    }
    assert scenario_business_fact(evidence, 1, "report_date", "2026-11-21", {}) is True
    assert scenario_business_fact(evidence, 1, "pending_approval_reported", True, {}) is True
    request["body"] += "\napproval-1 = approved"
    assert scenario_business_fact(evidence, 1, "pending_approval_reported", True, {}) is False
    request["body"] = "Согласование упомянуто без проверяемой ссылки."
    assert scenario_business_fact(evidence, 1, "pending_approval_reported", True, {}) is None

    observation = "send_review invoked; distinguish sent from delivered"
    assert scenario_observation(evidence, 1, observation) is True
    facts["status"] = "delivered"
    assert scenario_observation(evidence, 1, observation) is False
