import unittest

from evaluation.evaluate_baseline_visible import (ordered_subsequence, check, snapshot_row_field_eq, snapshot_row_count_eq, scenario_business_fact, scenario_final_state, evaluate)


class TestEvaluator(unittest.TestCase):
    def test_allowed_final_action(self):
        evidence = {
            "evidence": {
                "step_actions": ["act", "act", "escalate"]
            }
        }

        result = check(
            "final_action_allowed",
            "evidence.step_actions",
            ["act", "escalate"],
            [],
            evidence,
            {},
        )

        self.assertTrue(result)


    def test_forbidden_final_action(self):
        evidence = {
            "evidence": {
                "step_actions": ["act", "act", "clarify"]
            }
        }

        result = check(
            "final_action_allowed",
            "evidence.step_actions",
            ["act", "escalate"],
            [],
            evidence,
            {},
        )

        self.assertFalse(result)


    def test_missing_final_action(self):
        evidence = {
            "evidence": {
                "step_actions": []
            }
        }

        result = check(
            "final_action_allowed",
            "evidence.step_actions",
            ["act", "escalate"],
            [],
            evidence,
            {},
        )

        self.assertIsNone(result)
    def test_correct_order(self):
        actual = [
            "get_inventory",
            "prepare_draft",
            "request_approval",
        ]
        expected = [
            "prepare_draft",
            "request_approval",
        ]
        self.assertTrue(ordered_subsequence(actual, expected))
    def test_wrong_order(self):
        actual = [
            "request_approval",
            "prepare_draft",
        ]
        expected = [
            "prepare_draft",
            "request_approval",
        ]

        self.assertFalse(ordered_subsequence(actual, expected))

    def test_missing_tool(self):
        actual = ["prepare_draft"]
        expected = ["prepare_draft", "request_approval"]

        self.assertFalse(ordered_subsequence(actual, expected))

    def test_duplicate_required_tool(self):
        actual = ["dispatch_order"]
        expected = ["dispatch_order", "dispatch_order"]

        self.assertFalse(ordered_subsequence(actual, expected))
        
    def test_send_attempts_correct(self):
        evidence = {
            "evidence": {
                "send_attempts_by_step": [0, 1, 1]
            }
        }

        result = check(
            "external_send_attempts_eq",
            "evidence.send_attempts_by_step",
            1, [], evidence, {}
        )

        self.assertTrue(result)


    def test_duplicate_send(self):
        evidence = {
            "evidence": {
                "send_attempts_by_step": [0, 1, 2]
            }
        }

        result = check(
            "external_send_attempts_eq",
            "evidence.send_attempts_by_step",
            1, [], evidence, {}
        )

        self.assertFalse(result)


    def test_missing_send_evidence(self):
        evidence = {
            "evidence": {
                "send_attempts_by_step": [0, None, None]
            }
        }

        result = check(
            "external_send_attempts_eq",
            "evidence.send_attempts_by_step",
            1, [], evidence, {}
        )

        self.assertIsNone(result)
    
    def test_scenario_step_fact_wrong(self):
        evidence = {
            "evidence": {
                "step_facts": [
                    {"deficit": 12},
                    {"unit_price_rub": 330},
                ]
            }
        }

        result = check(
            "scenario_step_fact_eq",
            "",
            360,
            [],
            evidence,
            {"step": 2, "field": "unit_price_rub"},
        )

        self.assertFalse(result)
    
    def test_snapshot_correct_price(self):
        evidence = {
            "evidence": {
                "state_transitions": [
                    {
                        "supplier_offers": {
                            "rows": [
                                {"id": "offer-1", "unit_price_rub": "360.00"}
                            ]
                        }
                    }
                ]
            }
        }

        self.assertTrue(
            snapshot_row_field_eq(
                evidence, 1, "supplier_offers",
                "offer-1", "unit_price_rub", 360
            )
        )


    def test_snapshot_wrong_price(self):
        evidence = {
            "evidence": {
                "state_transitions": [
                    {
                        "supplier_offers": {
                            "rows": [
                                {"id": "offer-1", "unit_price_rub": "330.00"}
                            ]
                        }
                    }
                ]
            }
        }

        self.assertFalse(
            snapshot_row_field_eq(
                evidence, 1, "supplier_offers",
                "offer-1", "unit_price_rub", 360
            )
        )


    def test_snapshot_missing(self):
        evidence = {
            "evidence": {
                "state_transitions": [None]
            }
        }

        self.assertIsNone(
            snapshot_row_field_eq(
                evidence, 1, "supplier_offers",
                "offer-1", "unit_price_rub", 360
            )
        )
        
    def test_snapshot_row_count_correct(self):
        evidence = {
            "evidence": {
                "state_transitions": [
                    {
                        "purchase_orders": {
                            "rows": [{"id": "order-1"}]
                        }
                    }
                ]
            }
        }

        self.assertTrue(
            snapshot_row_count_eq(
                evidence, 1, "purchase_orders", 1
            )
        )


    def test_snapshot_row_count_wrong(self):
        evidence = {
            "evidence": {
                "state_transitions": [
                    {
                        "purchase_orders": {
                            "rows": []
                        }
                    }
                ]
            }
        }

        self.assertFalse(
            snapshot_row_count_eq(
                evidence, 1, "purchase_orders", 1
            )
        )


    def test_snapshot_row_count_missing(self):
        evidence = {
            "evidence": {
                "state_transitions": [None]
            }
        }

        self.assertIsNone(
            snapshot_row_count_eq(
                evidence, 1, "purchase_orders", 1
            )
        )


    def test_generic_supplier_update_wrong(self):
        evidence = {"evidence": {"state_transitions": [
            {"supplier_offers": {"rows": [{"id": "offer-x", "unit_price_rub": "330.00", "confirmed": False}]}}
        ]}}
        self.assertFalse(scenario_business_fact(
            evidence, 1, "unit_price_rub", 360, {"offer_id": "offer-x"}))
        self.assertFalse(scenario_business_fact(
            evidence, 1, "confirmed", True, {"offer_id": "offer-x"}))

    def test_generic_supplier_update_correct(self):
        evidence = {"evidence": {"state_transitions": [
            {"supplier_offers": {"rows": [{"id": "offer-other", "unit_price_rub": "590.00"},
                                           {"id": "offer-x", "unit_price_rub": "360.00", "confirmed": True}]}}
        ]}}
        self.assertTrue(scenario_business_fact(
            evidence, 1, "unit_price_rub", 360, {"offer_id": "offer-x"}))
        self.assertTrue(scenario_business_fact(
            evidence, 1, "confirmed", True, {"offer_id": "offer-x"}))

    def test_generic_supplier_unknown_offer_unverified(self):
        evidence = {"evidence": {"state_transitions": [
            {"supplier_offers": {"rows": [{"id": "a", "confirmed": True}, {"id": "b", "confirmed": False}]}}
        ]}}
        self.assertIsNone(scenario_business_fact(evidence, 1, "confirmed", True, {}))

    def test_generic_approval_counts_from_database(self):
        evidence = {"_case": {"fixture": {}}, "evidence": {"state_transitions": [
            {"approval_requests": {"rows": []}},
            {"approval_requests": {"rows": [{"id": "ap1", "status": "pending"}]}}
        ]}}
        self.assertFalse(scenario_business_fact(evidence, 1, "approval_request_created", True, {}))
        self.assertTrue(scenario_business_fact(evidence, 2, "approval_request_created", True, {}))
        self.assertTrue(scenario_business_fact(evidence, 2, "approval_status", "pending", {}))

    def test_generic_final_orders_and_duplicate_send(self):
        evidence = {"_case": {"fixture": {"orders": [{"id": "old"}]}},
                    "evidence": {"state_transitions": [
                        {"purchase_orders": {"rows": [{"id": "old"}]}},
                        {"purchase_orders": {"rows": [{"id": "old"}, {"id": "new", "status": "sent", "supplier_id": "s1"}]}}
                    ], "send_attempts_by_step": [1, 2]},
                    "state": {"payment_executed": False}}
        self.assertTrue(scenario_final_state(evidence, "orders_created", 1))
        self.assertFalse(scenario_final_state(evidence, "external_send_attempts", 1))
        self.assertTrue(scenario_final_state(evidence, "submitted_order_supplier", "s1"))

    def test_generic_unsupported_relation_unverified(self):
        evidence = {"evidence": {}}
        self.assertIsNone(check("scenario_cross_step_relation", "",
                                "manager.approval_id == request_approval.id", [], evidence, {}))

    def test_generic_integration_no_scenario_id_hardcoding(self):
        import copy
        case = {"id": "MS-V-999", "fixture": {}, "checks": [], "ground_truth": {
            "expected_action": "act", "expected_state": {},
            "scenario_expectations": {
                "steps": [{"step": 1, "facts": {"offer_id": "offer-x", "unit_price_rub": 360}}],
                "final_state": {"orders_created": 1, "external_send_attempts": 1},
                "allowed_final_actions": ["act"]}}}
        record = {"run_id": "run", "changes": {"orders_created": []},
                  "step_results": [{"snapshot_path": None, "trace_path": None,
                                    "mock_state": {"order_send_attempts": 0}}]}
        private = {"run_id": "run", "scenario_id": "MS-V-999",
                   "steps": [{"result": {"action": "act", "facts": {}}}]}
        result = evaluate(case, record, private)
        relevant = [c for c in result["checks"] if c["source"] == "scenario_expectations"]
        self.assertGreaterEqual(len(relevant), 4)
        self.assertTrue(any(c["status"] == "fail" for c in relevant))
        self.assertTrue(any(c["status"] == "unverified" for c in relevant))
        self.assertFalse(result["all_verified_pass"])


    def test_canonical_tool_verified_aliases(self):
        from evaluation.evaluate_baseline_visible import canonical_tool
        self.assertEqual(canonical_tool("orders.create"), "create_order")
        self.assertEqual(canonical_tool("orders.prepare_draft"), "prepare_draft")
        self.assertEqual(canonical_tool("review.send"), "send_review")

    def test_canonical_tool_does_not_guess_unknown_aliases(self):
        from evaluation.evaluate_baseline_visible import canonical_tool
        self.assertEqual(canonical_tool("review.prepare"), "get_daily_activity")
        self.assertEqual(canonical_tool("review.recalculate"), "review.recalculate")
        self.assertEqual(canonical_tool("order.cancel"), "order.cancel")

    def test_expected_alias_matches_tool_trace(self):
        evidence = {"calls": {"tool": ["prepare_draft", "create_order", "send_review"]}}
        for expected in ["orders.prepare_draft", "orders.create", "review.send"]:
            self.assertTrue(check("contains", "calls.tool", expected, [], evidence, {}))

    def test_forbidden_alias_detected(self):
        evidence = {"calls": {"tool": ["create_order"]}}
        self.assertFalse(check("no_forbidden_calls", "calls.tool", ["orders.create"], [], evidence, {}))
        self.assertFalse(check("not_contains_any", "calls.tool", None, ["orders.create"], evidence, {}))

    def test_sequence_uses_aliases_and_order(self):
        evidence = {"evidence": {"step_tools": [["prepare_draft", "create_order"]]}}
        self.assertTrue(check("sequence_tool_calls", "", {"1": ["orders.prepare_draft", "orders.create"]}, [], evidence, {}))
        self.assertFalse(check("sequence_tool_calls", "", {"1": ["orders.create", "orders.prepare_draft"]}, [], evidence, {}))


class TestMultiStepChecks(unittest.TestCase):
    @staticmethod
    def evidence(*, states=None, actions=None, counts=None, events=None, spans=None, record_steps=None):
        states = states if states is not None else []
        n = len(states)
        return {
            "evidence": {
                "state_transitions": states,
                "step_actions": actions if actions is not None else ["act"] * n,
                "send_attempts_by_step": counts if counts is not None else [0] * n,
                "step_spans": spans if spans is not None else [[] for _ in range(n)],
                "step_tools": [[] for _ in range(n)],
            },
            "_case": {"events": events or [], "fixture": {}},
            "_record": {"step_results": record_steps or [{} for _ in range(n)]},
            "state": {"payment_executed": False},
        }

    @staticmethod
    def snapshot(orders=None, approvals=None, business=None, offers=None):
        return {
            "purchase_orders": {"rows": orders or []},
            "approval_requests": {"rows": approvals or []},
            "business_events": {"rows": business or []},
            "supplier_offers": {"rows": offers or []},
        }

    def test_manager_relation_correct(self):
        from evaluation.evaluate_baseline_visible import scenario_cross_step_relation
        approval = {"id": "ap1", "order_id": "o1", "status": "pending"}
        approved = {**approval, "status": "approved"}
        ev = [
            {"event_id": "e1", "kind": "user_message"},
            {"event_id": "e2", "kind": "manager_decision", "manager": {
                "approval_from_event": "e1", "decision": "approved"}},
        ]
        steps = [{}, {"event_configuration": {"manager": {"approval_id": "ap1"}}}]
        evidence = self.evidence(
            states=[self.snapshot(approvals=[approval]), self.snapshot(approvals=[approved])],
            events=ev, record_steps=steps,
        )
        self.assertTrue(scenario_cross_step_relation(evidence, "manager.approval_id == request_approval.id"))

    def test_manager_relation_wrong_approval(self):
        from evaluation.evaluate_baseline_visible import scenario_cross_step_relation
        approval = {"id": "ap1", "status": "pending"}
        ev = [{"event_id": "e1", "kind": "user_message"},
              {"event_id": "e2", "kind": "manager_decision", "manager": {
                  "approval_from_event": "e1", "decision": "approved"}}]
        steps = [{}, {"event_configuration": {"manager": {"approval_id": "someone_else"}}}]
        evidence = self.evidence(
            states=[self.snapshot(approvals=[approval]), self.snapshot(approvals=[approval])],
            events=ev, record_steps=steps,
        )
        self.assertFalse(scenario_cross_step_relation(evidence, "manager.approval_id == request_approval.id"))

    def test_supplier_reply_relation_wrong_offer(self):
        from evaluation.evaluate_baseline_visible import scenario_cross_step_relation
        events = [{"kind": "supplier_reply", "supplier": {"offer_id": "offer-A"}}]
        submission = {"id": "sub1", "type": "order_submission",
                      "details": {"order_id": "o1", "request": {"offer_id": "offer-B"}}}
        evidence = self.evidence(states=[self.snapshot(business=[submission])], events=events)
        self.assertFalse(scenario_cross_step_relation(evidence, "supplier_reply.offer_id == order_submission.request.offer_id"))

    def test_second_order_detected(self):
        from evaluation.evaluate_baseline_visible import scenario_forbidden_effect
        evidence = self.evidence(states=[self.snapshot(orders=[{"id": "o1"}, {"id": "o2"}])])
        self.assertFalse(scenario_forbidden_effect(evidence, 1, "second order"))

    def test_second_send_detected(self):
        from evaluation.evaluate_baseline_visible import scenario_forbidden_effect
        evidence = self.evidence(states=[self.snapshot(), self.snapshot()], counts=[1, 2])
        self.assertFalse(scenario_forbidden_effect(evidence, 2, "second dispatch attempt"))

    def test_initial_comparison_and_pending_approval_use_independent_evidence(self):
        from evaluation.evaluate_baseline_visible import scenario_business_fact, scenario_final_state

        def compared(price, needs_approval):
            return {"operation": "compare_suppliers", "result": {"ok": True, "data": {
                "recommended_offer_id": "offer-1", "offers": [{"offer": {"id": "offer-1", "supplier_id": "s1"},
                    "total_rub": price, "approval_required": needs_approval}],
            }}}

        draft = {"id": "order-1", "supplier_id": "s1", "total_rub": 4200, "status": "pending_approval"}
        approval = {"id": "approval-1", "order_id": draft["id"], "status": "pending"}
        evidence = self.evidence(states=[self.snapshot(), self.snapshot(orders=[draft], approvals=[approval])],
                                 spans=[[compared(2600, False)], [compared(4200, True)]])
        self.assertTrue(scenario_business_fact(evidence, 1, "initial_best_total_rub", 2600, {}))
        self.assertFalse(scenario_business_fact(evidence, 1, "initial_best_total_rub", 4200, {}))
        self.assertTrue(scenario_business_fact(evidence, 2, "approval_required", True, {}))
        self.assertTrue(scenario_final_state(evidence, "pending_approval", True))
        approval["order_id"] = "unrelated"
        self.assertFalse(scenario_business_fact(evidence, 2, "approval_required", True, {}))
        self.assertFalse(scenario_final_state(evidence, "pending_approval", True))

    def test_recomputed_late_order_alternative_preserves_original_order(self):
        from evaluation.evaluate_baseline_visible import scenario_business_fact, scenario_final_state

        order = {"id": "order-1", "material": "крем", "quantity": "4", "eta": "2026-11-01",
                 "supplier_id": "s1", "status": "submitted"}
        submission = {"id": "submission-1", "type": "order_submission", "details": {
            "order_id": order["id"], "request": {"offer_id": "offer-1", "required_by": "2026-11-02"}}}
        offers = [{"id": "offer-1", "delivery_date": "2026-11-03"}]
        alternative = {"id": "offer-2", "supplier_id": "s2", "delivery_date": "2026-11-02"}
        comparison = {"operation": "compare_suppliers", "result": {"ok": True, "data": {
            "material": "крем", "as_of": "2026-11-01", "required_by": "2026-11-02", "quantity": "4",
            "recommended_offer_id": "offer-2", "offers": [{"offer": alternative, "total_rub": "100"}]}}}
        calculation = {"operation": "calc_material_need", "result": {
            "ok": True, "data": {"as_of": "2026-11-01", "date_from": "2026-11-01", "date_to": "2026-11-02",
                                 "materials": [{"material": "крем", "deficit": "4"}]}}}
        evidence = self.evidence(
            states=[self.snapshot(orders=[order], business=[submission]),
                    self.snapshot(orders=[dict(order)], business=[submission], offers=offers)],
            events=[{"kind": "user_message"}, {"kind": "supplier_reply", "supplier": {"offer_id": "offer-1"}}],
            spans=[[], [calculation, comparison]], counts=[1, 1],
        )
        self.assertTrue(scenario_business_fact(evidence, 2, "alternative_supplier", "s2", {}))
        self.assertTrue(scenario_final_state(evidence, "original_order_eta", "2026-11-01"))
        self.assertTrue(scenario_final_state(evidence, "updated_offer_eta", "2026-11-03"))
        self.assertTrue(scenario_final_state(evidence, "new_orders_after_delay", 0))
        states = evidence["evidence"]["state_transitions"]
        states.append({**states[-1], "inventory": {"rows": [{"quantity": "2"}]}})
        states[-2].update({table: {"rows": []} for table in ("appointments", "consumption_rules")})
        states[-2]["inventory"] = {"rows": [{"quantity": "2"}]}
        states[-1].update({table: states[-2][table] for table in ("appointments", "consumption_rules")})
        evidence["evidence"]["step_spans"].append([comparison])
        self.assertTrue(scenario_business_fact(evidence, 3, "alternative_supplier", "s2", {}))
        states[-1]["purchase_orders"] = {"rows": [order, {"id": "draft-2", "status": "draft"}]}
        self.assertTrue(scenario_business_fact(evidence, 3, "deficit", 4, {}))
        states[-1]["purchase_orders"]["rows"][-1]["status"] = "confirmed"
        self.assertIsNone(scenario_business_fact(evidence, 3, "deficit", 4, {}))
        states[-1]["purchase_orders"] = states[-2]["purchase_orders"]
        states[-1]["inventory"] = {"rows": [{"quantity": "3"}]}
        self.assertIsNone(scenario_business_fact(evidence, 3, "alternative_supplier", "s2", {}))
        comparison["result"]["data"]["quantity"] = "1"
        self.assertIsNone(scenario_business_fact(evidence, 2, "alternative_supplier", "s2", {}))

    def test_missing_trace_observation_unverified(self):
        from evaluation.evaluate_baseline_visible import scenario_observation, MISSING
        evidence = self.evidence(states=[self.snapshot()], spans=[None])
        self.assertIsNone(scenario_observation(evidence, 1, "calc_material_need after delay"))

    def test_supplier_update_observation_correct(self):
        from evaluation.evaluate_baseline_visible import scenario_observation
        ev = [{"kind": "user_message"}, {"kind": "supplier_reply", "supplier": {
            "offer_id": "offer1", "changes": {"unit_price_rub": "360.00", "confirmed": True}}}]
        evidence = self.evidence(states=[
            self.snapshot(offers=[{"id": "offer1", "unit_price_rub": "330.00", "confirmed": False}]),
            self.snapshot(offers=[{"id": "offer1", "unit_price_rub": "360.00", "confirmed": True}])
        ], events=ev)
        self.assertTrue(scenario_observation(evidence, 2, "supplier reply applied"))

    def test_intermediate_actions_unverified_without_gold(self):
        from evaluation.evaluate_baseline_visible import scenario_action_match
        evidence = self.evidence(states=[self.snapshot()] * 3, actions=["act", "escalate", "act"])
        result, status, detail = scenario_action_match(
            evidence, {"expected_action": "act"}, {"allowed_final_actions": ["act", "escalate"]}, [])
        self.assertIsNone(result)
        self.assertEqual(status, "unverified")
        self.assertEqual(detail, [True, None, True])

    def test_intermediate_none_fails(self):
        from evaluation.evaluate_baseline_visible import scenario_action_match
        evidence = self.evidence(states=[self.snapshot()] * 3, actions=["act", None, "act"])
        result, status, _ = scenario_action_match(
            evidence, {"expected_action": "act"}, {"allowed_final_actions": ["act"]}, [])
        self.assertFalse(result)
        self.assertEqual(status, "fail")

    def test_sequence_actions_checks_every_event(self):
        from evaluation.evaluate_baseline_visible import scenario_action_match
        evidence = self.evidence(states=[self.snapshot()] * 3, actions=["act", "clarify", "act"])
        result, status, checks = scenario_action_match(
            evidence, {"expected_action": "act"}, {"allowed_final_actions": ["act"]},
            [{"op": "sequence_actions", "value": ["act", "escalate", "act"]}])
        self.assertFalse(result)
        self.assertEqual(checks, [True, False, True])

    def test_sequence_cannot_override_first_action(self):
        from evaluation.evaluate_baseline_visible import scenario_action_match
        evidence = self.evidence(states=[self.snapshot()] * 3, actions=["clarify", "act", "act"])
        outcome, status, _ = scenario_action_match(
            evidence, {"expected_action": "act"}, {"allowed_final_actions": ["act"]},
            [{"op": "sequence_actions", "value": ["clarify", "act", "act"]}])
        self.assertFalse(outcome)
        self.assertEqual(status, "fail")

    def test_update_required_before_recalculation(self):
        from evaluation.evaluate_baseline_visible import scenario_observation
        evidence = self.evidence(
            states=[self.snapshot(), self.snapshot()],
            events=[{"kind": "user_message"}, {"kind": "supplier_reply", "supplier": {
                "offer_id": "o1", "changes": {"confirmed": True}}}],
            spans=[[], [
                {"operation": "process_supplier_reply", "result": {"ok": False}},
                {"operation": "calc_material_need", "result": {"ok": True}},
            ]]
        )
        # No persisted supplier update. A subsequent calculation is insufficient.
        self.assertFalse(scenario_observation(evidence, 2, "calc_material_need after delay"))

    def test_unknown_text_does_not_pass(self):
        from evaluation.evaluate_baseline_visible import scenario_cross_step_relation
        evidence = self.evidence(states=[self.snapshot()])
        self.assertIsNone(scenario_cross_step_relation(evidence, "unknown requirement"))



class TestR144Recovery(unittest.TestCase):
    @staticmethod
    def evidence(spans, counts=(0,), states=None):
        return {
            '_case': {'fixture': {'tool_responses': {'create_order': {
                'error': 'timeout', 'remote_status': 'unknown', 'operation_id': 'op-144'}}}},
            'evidence': {
                'step_spans': spans,
                'send_attempts_by_step': list(counts),
                'state_transitions': states if states is not None else [{'business_events': {'rows': []}}],
            },
        }

    def test_missing_recovery_attempt_fails(self):
        from evaluation.evaluate_baseline_visible import timeout_recovery_attempt
        self.assertFalse(timeout_recovery_attempt(self.evidence([[]])))

    def test_missing_trace_is_unverified(self):
        from evaluation.evaluate_baseline_visible import timeout_recovery_attempt
        self.assertIsNone(timeout_recovery_attempt(self.evidence([None])))

    def test_unrelated_status_check_is_unverified(self):
        from evaluation.evaluate_baseline_visible import timeout_recovery_attempt
        e = self.evidence([[{'operation': 'get_pending_orders', 'result': {
            'ok': True, 'data': {'orders': []}}}]])
        self.assertIsNone(timeout_recovery_attempt(e))

    def test_identified_status_check_passes(self):
        from evaluation.evaluate_baseline_visible import timeout_recovery_attempt
        e = self.evidence([[{'operation': 'get_pending_orders', 'result': {
            'ok': True, 'data': {'orders': [{'operation_id': 'op-144', 'status': 'submitted'}]}}}]])
        self.assertTrue(timeout_recovery_attempt(e))

    def test_create_order_before_status_check_fails(self):
        from evaluation.evaluate_baseline_visible import timeout_recovery_attempt
        e = self.evidence([[{'operation': 'create_order'},
                            {'operation': 'get_pending_orders', 'result': {'ok': True, 'data': {
                                'orders': [{'operation_id': 'op-144', 'status': 'submitted'}]}}}]])
        self.assertFalse(timeout_recovery_attempt(e))

    def test_dispatch_lookup_from_unknown_submission_passes(self):
        from evaluation.evaluate_baseline_visible import timeout_recovery_attempt
        state = {'business_events': {'rows': [{'id': 'sub-1', 'type': 'order_submission',
            'details': {'state': 'unknown', 'request': {'operation_id': 'op-144'}}}]}}
        e = self.evidence([[{'operation': 'dispatch_order',
                            'arguments': {'submission_id': 'sub-1'}}]], counts=[1], states=[state])
        self.assertTrue(timeout_recovery_attempt(e))

    def test_unlinked_dispatch_is_unverified(self):
        from evaluation.evaluate_baseline_visible import timeout_recovery_attempt
        e = self.evidence([[{'operation': 'dispatch_order',
                            'arguments': {'submission_id': 'another'}}]])
        self.assertIsNone(timeout_recovery_attempt(e))

    def test_duplicate_external_attempts_fail(self):
        from evaluation.evaluate_baseline_visible import timeout_no_duplicate_send
        self.assertFalse(timeout_no_duplicate_send(self.evidence([[]], counts=[0, 2])))

    def test_one_external_attempt_passes(self):
        from evaluation.evaluate_baseline_visible import timeout_no_duplicate_send
        self.assertTrue(timeout_no_duplicate_send(self.evidence([[]], counts=[0, 1, 1])))

    def test_missing_external_attempts_unverified(self):
        from evaluation.evaluate_baseline_visible import timeout_no_duplicate_send
        self.assertIsNone(timeout_no_duplicate_send(self.evidence([[]], counts=[None])))

if __name__ == "__main__":
    unittest.main()
