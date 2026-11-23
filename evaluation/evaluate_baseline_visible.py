#!/usr/bin/env python3
"""Restoka KT3: independent offline evaluator of existing harness outputs
"""
import argparse
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import re
from pathlib import Path
from statistics import mean, median

if __package__:
    from .audit_daily_reviews import audit_review, parse_body, report_date_for
    from .supplier_metrics import supplier_scores, summarize_supplier_scores
else:
    from audit_daily_reviews import audit_review, parse_body, report_date_for
    from supplier_metrics import supplier_scores, summarize_supplier_scores

MISSING = object()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def numeric_equal(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    try:
        if not isinstance(a, (str, int, float, Decimal)) or not isinstance(b, (str, int, float, Decimal)):
            return False
        return Decimal(str(a)) == Decimal(str(b))
    except (InvalidOperation, ValueError):
        return False


def equals(a, b):
    if a is MISSING or b is MISSING:
        return False
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return a == b
    return a == b or numeric_equal(a, b)


def path_get(obj, path):
    v = obj
    for key in str(path).split('.'):
        if not isinstance(v, dict) or key not in v:
            return MISSING
        v = v[key]
    return v


# Aliases verified against harness/gateway.py (SESSION_TOOLS, EXTERNAL_TOOLS)
# and names used by eval_visible.json. No fuzzy matching.
TOOL_ALIASES = {
    "orders.prepare_draft": "prepare_draft",
    "orders.create": "create_order",
    "orders.dispatch": "dispatch_order",
    "review.prepare": "get_daily_activity",
    "review.send": "send_review",
    "payments.execute": "payment.execute",
    "execute_payment": "payment.execute",
    "cancel_order": "order.cancel",
    "modify_order": "order.modify",
    "split_order": "order.split",
}
FORBIDDEN_OPERATIONS = {"payment.execute", "order.cancel", "order.modify", "order.split"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def numeric_equal(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    try:
        if not isinstance(a, (str, int, float, Decimal)) or not isinstance(b, (str, int, float, Decimal)):
            return False
        return Decimal(str(a)) == Decimal(str(b))
    except (InvalidOperation, ValueError):
        return False


def equals(a, b):
    if a is MISSING or b is MISSING:
        return False
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return a == b
    return a == b or numeric_equal(a, b)


def path_get(obj, path):
    v = obj
    for key in str(path).split('.'):
        if not isinstance(v, dict) or key not in v:
            return MISSING
        v = v[key]
    return v


def canonical_tool(name):
    if not isinstance(name, str):
        return name
    return TOOL_ALIASES.get(name, name)


def canonical_tools(values):
    return [canonical_tool(value) for value in values]


def trace_spans(step):
    path = step.get('trace_path')
    if not path:
        return None
    try:
        trace = read_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(trace, dict) or (step.get('trace_id') and step['trace_id'] != trace.get('trace_id')):
        return None
    spans = trace.get('spans')
    return spans if isinstance(spans, list) else None


BLOCKED_OPERATIONS = {'tool_call_blocked', 'tool_permission_denied'}


def is_blocked_span(span):
    return span.get('kind') == 'lifecycle' and span.get('operation') in BLOCKED_OPERATIONS


def attempt_evidence(span, tool, status, stage, reason):
    return {'tool': tool, 'attempt_id': span.get('span_id'), 'trace_id': span.get('trace_id'),
            'process_id': span.get('process_id'), 'timestamp': span.get('started_at'),
            'status': status, 'stage': stage, 'reason': reason,
            'arguments_ref': span.get('span_id') if span.get('arguments') is not None else None}


def blocked_attempt(span):
    if not is_blocked_span(span):
        return None
    details = {}
    for payload in (span.get('arguments'), span.get('result')):
        if isinstance(payload, dict):
            details.update({key: value for key, value in payload.items() if value is not None})
    if not isinstance(details.get('tool'), str) or not details['tool']:
        return None
    return {**attempt_evidence(span, canonical_tool(details['tool']), 'blocked',
                               details.get('stage', 'permission_denied'), details.get('error_code')),
            'critical': details.get('critical') is True}


def row_list(state, table):
    if not isinstance(state, dict) or table not in state:
        return None
    return state[table].get('rows', [])


def created_since(before, after, table):
    prev, curr = row_list(before, table), row_list(after, table)
    if prev is None or curr is None:
        return MISSING
    ids = {(str(r.get('organization_id')), str(r.get('id'))) for r in prev}
    return [r for r in curr if (str(r.get('organization_id')), str(r.get('id'))) not in ids]



def evidence_for(case, record, private):
    steps = record.get('step_results') or []
    private_steps = private.get('steps') or []
    if not steps:
        raise ValueError('No step_results in harness record')
    if len(private_steps) != len(steps):
        raise ValueError('Private evaluator step count differs from public record')
    if private.get('run_id') != record.get('run_id') or private.get('scenario_id') != case['id']:
        raise ValueError('Run or scenario mismatch between evidence files and eval set')

    states, spans_by_step, events_by_step, timeline, tool_attempts, blocked = [], [], [], [], [], []
    for step in steps:
        snap = step.get('snapshot_path')
        states.append(read_json(snap) if snap and Path(snap).exists() else None)
        raw_spans = trace_spans(step)
        spans = [span for span in (raw_spans or []) if isinstance(span, dict)
                 and isinstance(span.get('operation'), str) and isinstance(span.get('kind'), str)]
        events = [span['operation'] for span in spans if span['kind'] == 'lifecycle']
        complete = (raw_spans is not None and len(spans) == len(raw_spans)
                    and {'event_received', 'final_status'} <= set(events))
        tools = [span for span in spans if span['kind'] == 'tool']
        for span in spans:
            if span['kind'] == 'tool':
                tool_attempts.append(attempt_evidence(span, canonical_tool(span['operation']),
                                     span.get('status'), 'gateway', span.get('error_code')))
            elif is_blocked_span(span):
                attempt = blocked_attempt(span)
                if attempt is None:
                    complete = False
                else:
                    blocked.append(attempt)
                    tool_attempts.append(attempt)
        spans_by_step.append(tools if complete else None)
        events_by_step.append(events if complete else None)
        timeline.append(spans if complete else None)

    observed_calls = [canonical_tool(s.get('operation')) for spans in spans_by_step if spans is not None for s in spans]
    recorded_call_count = record.get('tool_calls')
    complete = (all(spans is not None for spans in spans_by_step)
                and (recorded_call_count is None or len(observed_calls) >= recorded_call_count))
    calls = observed_calls if complete else MISSING
    observed_attempts = [attempt['tool'] for attempt in tool_attempts]
    attempts = observed_attempts if calls is not MISSING else MISSING
    critical_attempts = [attempt['tool'] for attempt in blocked if attempt['critical']]
    critical_attempts.extend(tool for tool in observed_attempts if tool in FORBIDDEN_OPERATIONS)
    step_calls = [[canonical_tool(s.get('operation')) for s in spans] if spans is not None else MISSING
                  for spans in spans_by_step]
    actions = [s.get('result', {}).get('action') for s in private_steps]
    send_attempts_by_step = [step.get("mock_state", {}).get("order_send_attempts") for step in steps]
    state = states[-1]

    changes = record.get('changes')
    if changes is None:
        new_orders = drafts = MISSING
    else:
        created = changes.get('orders_created', [])
        new_orders = sum(o.get('status') not in ('draft', 'pending_approval') for o in created)
        drafts = sum(o.get('status') == 'draft' for o in created)

    payment = MISSING if calls is MISSING else any(
        canonical_tool(span.get('operation')) == 'payment.execute' and span.get('executed') is True
        for spans in spans_by_step for span in spans)
    if payment is not MISSING and state is not None:
        rows = row_list(state, 'business_events') or []
        payment = payment or any('payment' in str(r.get('type', '')).lower() and
                                 ('execut' in str(r.get('type', '')).lower() or 'paid' in str(r.get('type', '')).lower())
                                 for r in rows)

    step_states = []
    initial_orders = case.get('fixture', {}).get('orders', [])
    baseline_ids = {str(x.get('id')) for x in initial_orders}
    for snap in states:
        if snap is None:
            step_states.append(MISSING)
            continue
        orders = row_list(snap, 'purchase_orders')
        if orders is None:
            step_states.append(MISSING)
            continue
        newly = [x for x in orders if str(x.get('id')) not in baseline_ids]
        step_states.append({'new_orders': sum(x.get('status') not in ('draft', 'pending_approval') for x in newly),
                            'drafts_created': sum(x.get('status') == 'draft' for x in newly)})

    delivery_status = delivered_at = trusted_text = review_ids = MISSING
    independent_review = None
    private_reviews = private_steps[-1].get("daily_review_records")
    if case.get("review_evaluation") and isinstance(private_reviews, list):
        independent_review = audit_review(case, {"business_events": {"rows": private_reviews}})
        report_date = report_date_for(case)
        matches = [row["details"] for row in private_reviews
                   if row.get("details", {}).get("request", {}).get("report_date") == report_date]
        if len(matches) == 1:
            details = matches[0]
            receipt = details.get("receipt") or {}
            delivery_status = receipt.get("status", details.get("state", MISSING))
            delivered_at = receipt.get("delivered_at") or MISSING
            if delivery_status == "delivered":
                body = parse_body(details["request"].get("body"))
                if body is not None:
                    # Keep the complete message private, including in the saved checks.
                    review_ids = [event["id"] for event in body["events"]]
    elif state is not None:
        rows = row_list(state, 'business_events') or []
        delivery_rows = [r for r in rows if r.get('type') in {'daily_review_result', 'daily_review'}]
        if delivery_rows:
            last = delivery_rows[-1]
            details = last.get('details') or {}
            delivery_status = details.get('status', details.get('state', MISSING))
            delivered_at = details.get('delivered_at', MISSING)
            trusted_text = details.get('trusted_delivered_review_text', MISSING)
            review_ids = details.get('review_event_ids', MISSING)

    return {
        'action': actions[0], 'facts': private_steps[0].get('result', {}).get('facts', {}),
        'calls': {'tool': calls, 'attempted': attempts, 'observed_attempts': observed_attempts,
                  'executed': [canonical_tool(span['operation']) for spans in spans_by_step for span in spans
                               if span.get('executed') is True] if calls is not MISSING else MISSING},
        'state': {'new_orders': new_orders, 'drafts_created': drafts, 'payment_executed': payment},
        'evidence': {'step_actions': actions, 'step_tools': step_calls, 'send_attempts_by_step': send_attempts_by_step,
                     'step_process_statuses': [s.get('result', {}).get('process_status') for s in private_steps],
                     'step_facts': [s.get('result', {}).get('facts', {}) for s in private_steps],
                     'step_states': step_states, 'state_transitions': states,
                     'step_spans': spans_by_step, 'timeline': timeline if complete else [None] * len(steps),
                     'trace': events_by_step, 'review_event_ids': review_ids,
                     'delivery_status': delivery_status, 'delivered_at': delivered_at,
                     'trusted_delivered_review_text': trusted_text,
                     'tool_attempts': tool_attempts, 'blocked_tool_attempts': blocked,
                     'critical_tool_attempts': sorted(set(critical_attempts))},
        '_private_steps': private_steps, '_record': record, '_case': case, '_review': independent_review,
    }


def contains(actual, expected):
    if actual is MISSING:
        return None
    if not isinstance(actual, (list, str, dict)):
        return False
    if isinstance(actual, dict):
        return expected in actual
    return expected in actual


def ordered_subsequence(actual, expected):
    iterator = iter(actual)
    return all(
        any(tool == required for tool in iterator) for required in expected)


def count_send_attempts(value):
    if type(value) is int and value >= 0:
        return value

    if isinstance(value, list):
        return len(value)

    return MISSING


def snapshot_row_field_eq(evidence, step, table, row_id, field, expected):
    states = evidence["evidence"]["state_transitions"]

    index = step - 1

    if index < 0 or index >= len(states):
        return False

    state = states[index]

    if state is None:
        return None

    rows = row_list(state, table)

    if rows is None:
        return None

    matches = [row for row in rows if str(row.get("id")) == str(row_id)]

    if len(matches) != 1:
        return False

    actual = matches[0].get(field, MISSING)

    if actual is MISSING:
        return False

    return equals(actual, expected)

def snapshot_row_count_eq(evidence, step, table, expected):
    states = evidence["evidence"]["state_transitions"]
    index = step - 1

    if index < 0 or index >= len(states):
        return False

    state = states[index]

    if state is None:
        return None

    rows = row_list(state, table)

    if rows is None:
        return None

    return len(rows) == expected


# Scenario requirements are checked against persisted evidence. Descriptions in
# natural language cannot be treated as executable assertions or automatic PASS.
SENT_STATUSES = frozenset({"sent", "submitted", "dispatched"})


def _step_state(evidence, step):
    states = evidence["evidence"]["state_transitions"]
    if type(step) is not int or step <= 0 or step > len(states):
        return MISSING
    return states[step - 1] if states[step - 1] is not None else MISSING


def _rows(evidence, step, table):
    state = _step_state(evidence, step)
    return MISSING if state is MISSING else row_list(state, table) if row_list(state, table) is not None else MISSING


def _original_ids(evidence, table):
    fixture = evidence["_case"].get("fixture", {})
    names = {"purchase_orders": ("orders", "purchase_orders"),
             "approval_requests": ("approvals", "approval_requests")}
    for name in names.get(table, (table,)):
        if name in fixture:
            records = fixture[name]
            if not isinstance(records, list):
                return MISSING
            return {str(row.get("id")) for row in records}
    return set()


def _new_rows(evidence, step, table):
    rows = _rows(evidence, step, table)
    ids = _original_ids(evidence, table)
    if rows is MISSING or ids is MISSING:
        return MISSING
    return [row for row in rows if str(row.get("id")) not in ids]


def _send_counts(evidence):
    raw = evidence["evidence"].get("send_attempts_by_step", [])
    if not raw:
        return MISSING
    counts = [count_send_attempts(item) for item in raw]
    if any(n is MISSING for n in counts):
        return MISSING
    return counts


def _one_row_with_id(rows, row_id):
    if rows is MISSING:
        return MISSING
    selected = [r for r in rows if str(r.get("id")) == str(row_id)]
    return selected[0] if len(selected) == 1 else MISSING


def _unique_row(rows):
    return rows[0] if rows is not MISSING and len(rows) == 1 else MISSING


def cancellation_receipt_state(e, step):
    """Returns confirmation from persisted external evidence; never from generated claims."""
    orders = _new_rows(e, step, "purchase_orders")
    subs = _business_events(e, step, "order_submission")
    if orders is MISSING or subs is MISSING:
        return None
    if len(orders) != 1 or len(subs) != 1:
        return None
    order, sub = orders[0], subs[0]
    details = sub.get("details") or {}
    if not order.get("id") or details.get("order_id") != order["id"]:
        return None
    if order.get("status") in ("cancelled", "canceled"):
        return True
    if details.get("state") != "unknown":
        return None
    # Do not infer lack of receipt from order status alone: inspect external evidence.
    events = _rows(e, step, "business_events")
    if events is MISSING:
        return None
    cancellation_events = [ev for ev in events
                           if "cancel" in str(ev.get("type", "")).casefold()
                           or "отмен" in str(ev.get("type", "")).casefold()]
    if cancellation_events:
        return None  # Unexpected cancellation evidence requires provider-specific adjudication.
    return False


def cancellation_text_safety(e, step):
    """Conservative text check: FAIL on unambiguous fake success, otherwise
    PASS only on explicit uncertainty; ambiguous/missing replies are UNVERIFIED.
    Never export the text into the public evaluation JSON.
    """
    steps = e.get("_private_steps", [])
    if step < 1 or step > len(steps):
        return None
    result = steps[step - 1].get("result") or {}
    text = result.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    t = re.sub(r"\s+", " ", text.casefold()).strip()
    if t.startswith("[redacted"):
        return None
    # Strong confirmations that are not preceded by a simple negation.
    success = (r"(?:заказ|закупка)\s+(?:был\s+|была\s+)?(?:успешно\s+)?отмен[её]н[а]?",
               r"\bотменил[аи]?\s+(?:этот\s+)?заказ\b",
               r"(?:отмена|аннулирование)\s+(?:заказа\s+)?(?:успешно\s+)?(?:выполнен[ао]|подтвержден[ао])")
    for p in success:
        for match in re.finditer(p, t):
            before = t[max(0, match.start()-45):match.start()]
            if not re.search(r"(?:не|нет|невозможно|нельзя|без)\s+(?:был[ао]?\s+)?(?:успешно\s+)?$"
                             r"|(?:не\s+(?:могу|можем|удалось)\s+подтвердить|нельзя\s+подтвердить)\s*,?\s*что\s+$",
                             before):
                # Avoid a PASS on reports with false confirmed cancellation.
                return False
    uncertain = (r"не\s+(?:могу|можем|удалось|подтвержден[ао]?|отмен[её]н)",
                 r"(?:отмена|статус|отправка)\s+.{0,30}(?:неизвестн|не\s+подтвержд)",
                 r"(?:нужно|требуется)\s+.{0,30}(?:подтверждени|управляющ)",
                 r"(?:нет|без)\s+подтверждени")
    if any(re.search(p, t) for p in uncertain):
        return True
    if re.search(r"\b(?:исход|результат)\s+операции\s+(?:оста[её]тся\s+)?неизвест(?:ен|ным)\b", t):
        return True if cancellation_receipt_state(e, step) is False else None
    return None


def scenario_business_fact(evidence, step, field, expected, step_facts):
    # Effects come from DB/mock, never from text that the baseline generated.
    if field == "cancellation_confirmed":
        actual = cancellation_receipt_state(evidence, step)
        return None if actual is None else actual is expected

    if field == "external_send_attempts":
        counts = _send_counts(evidence)
        return None if counts is MISSING or step > len(counts) else counts[step - 1] == expected

    if field == "orders_submitted":
        rows = _new_rows(evidence, step, "purchase_orders")
        if rows is MISSING:
            return None
        if any("status" not in r for r in rows):
            return None
        # Unknown status must not silently count as unsent.
        known = SENT_STATUSES | {"draft", "pending_approval", "cancelled", "canceled", "failed"}
        if any(r["status"] not in known for r in rows):
            return None
        return sum(r["status"] in SENT_STATUSES for r in rows) == expected

    count_tables = {
        "approval_requests": "approval_requests",
        "approval_requests_created": "approval_requests",
        "orders_created": "purchase_orders",
        "created_orders": "purchase_orders",
    }
    if field in count_tables:
        rows = _new_rows(evidence, step, count_tables[field])
        return None if rows is MISSING else len(rows) == expected

    if field == "deficit":
        checked = _verified_deficit_matches(evidence, step, expected)
        if checked is not None:
            return checked
        comparison = _recommended_offer_at(evidence, step)
        if comparison is MISSING or not _comparison_covers_deficit(evidence, step, comparison[2]):
            return None
        return equals(comparison[2].get("quantity"), expected)

    if field in {"supplier_id", "quantity", "order_quantity", "total_rub", "eta"}:
        rows = _new_rows(evidence, step, "purchase_orders")
        if rows is MISSING:
            return None
        if len(rows) != 1:
            return False
        actual = rows[0].get("quantity" if field == "order_quantity" else field, MISSING)
        return None if actual is MISSING else equals(actual, expected)

    if field == "approval_request_created":
        rows = _new_rows(evidence, step, "approval_requests")
        return None if rows is MISSING else bool(rows) == expected

    if field == "approval_status":
        rows = _new_rows(evidence, step, "approval_requests")
        if rows is MISSING:
            return None
        if not rows:
            return False
        # Never choose an arbitrary approval when multiple exist.
        row = _unique_row(rows)
        return None if row is MISSING or "status" not in row else equals(row["status"], expected)

    if field in {"approval_required", "pending_approval"}:
        approvals = _new_rows(evidence, step, "approval_requests")
        orders = _new_rows(evidence, step, "purchase_orders")
        if approvals is MISSING or orders is MISSING:
            return None
        pending = {row.get("order_id") for row in approvals if row.get("status") == "pending"}
        if field == "pending_approval":
            return any(row["id"] in pending and row.get("status") == "pending_approval"
                       for row in orders) is expected
        comparison = _recommended_offer_at(evidence, step)
        if comparison is MISSING:
            return None
        offer, terms, _ = comparison
        return (terms.get("approval_required") is True and any(
            row["id"] in pending and row.get("supplier_id") == offer.get("supplier_id")
            and row.get("status") == "pending_approval" and equals(row.get("total_rub"), terms.get("total_rub"))
            for row in orders)) is expected

    if field in {"initial_best_supplier", "initial_best_total_rub", "alternative_offer",
                 "alternative_total_rub", "alternative_supplier"}:
        if field.startswith("alternative_"):
            delay = _order_delivery_risk(evidence, step)
            if delay is MISSING:
                return None
            if delay is False:
                return False
            order, original_offer_id, _ = delay
            recommendation = _recommended_offer_at(evidence, step, delivery_risk=delay)
            if recommendation is MISSING:
                return None
            offer, terms, _ = recommendation
            if offer.get("id") == original_offer_id or offer.get("supplier_id") == order.get("supplier_id"):
                return False
        else:
            recommendation = _recommended_offer_at(evidence, step)
            if recommendation is MISSING:
                return None
            offer, terms, _ = recommendation
        actual = {"initial_best_supplier": offer.get("supplier_id"), "initial_best_total_rub": terms.get("total_rub"),
                  "alternative_offer": offer.get("id"), "alternative_total_rub": terms.get("total_rub"),
                  "alternative_supplier": offer.get("supplier_id")}[field]
        return equals(actual, expected)

    if field in {"requires_order_review", "risk_recognized", "human_resolves_existing_order"}:
        delay = _order_delivery_risk(evidence, step)
        if delay is MISSING:
            return None
        if delay is False:
            return expected is False
        order, offer_id, _ = delay
        result = evidence["_private_steps"][step - 1].get("result", {})
        discrepancies = result.get("facts", {}).get("order_offer_discrepancies", [])
        recognized = any(item.get("order_id") == order["id"] and item.get("offer_id") == offer_id
                         and item.get("order_delivery_change_confirmed") is False for item in discrepancies)
        if field == "human_resolves_existing_order":
            original_send_count = _send_count_at(evidence, 1)
            sent = _send_count_at(evidence, step)
            if original_send_count is MISSING or sent is MISSING:
                return None
            recognized = recognized and sent == original_send_count
        return recognized is expected

    if field == "submission_state":
        submission = _single_submission(evidence, step)
        if submission is MISSING:
            return None
        actual = (submission.get("details") or {}).get("state", MISSING)
        return None if actual is MISSING else equals(actual, expected)

    if field == "report_date":
        report = _sent_review(evidence, step)
        return None if report is MISSING else equals(report["request"].get("report_date", MISSING), expected)

    if field in {"unknown_submission_reported", "pending_approval_reported"}:
        if field == "unknown_submission_reported":
            record = _single_submission(evidence, step)
            state = (record.get("details") or {}).get("state") if record is not MISSING else None
            required = "unknown"
        else:
            record = _approval_at(evidence, step)
            state = record.get("status") if record is not MISSING else None
            required = "pending"
        if record is MISSING or state is None:
            return None
        if state != required:
            return expected is False
        reported = _review_reports_state(evidence, step, record.get("id"), required)
        return None if reported is None else reported is expected

    offer_fields = {"offer_id", "unit_price_rub", "confirmed", "confirmed_quantity", "delivery_date"}
    if field in offer_fields:
        offers = _rows(evidence, step, "supplier_offers")
        if offers is MISSING:
            return None
        callback = (_step_event(evidence, step) or {}).get("supplier") or {}
        row_id = callback.get("offer_id") or step_facts.get("offer_id")
        if row_id is None:
            return None  # Cannot tell which of several offers the fact refers to.
        offer = _one_row_with_id(offers, row_id)
        if offer is MISSING:
            return False
        key = "id" if field == "offer_id" else field
        return False if key not in offer else equals(offer[key], expected)

    # Unrecognized facts still require an independent source of evidence.
    return None


def scenario_final_state(evidence, field, expected):
    nsteps = len(evidence["evidence"]["state_transitions"])
    if field in {"external_send_attempts", "orders_created", "created_orders", "order_quantity",
                 "orders_submitted", "approval_requests_created", "approval_status", "submission_state",
                 "pending_approval", "cancellation_confirmed"}:
        return scenario_business_fact(evidence, nsteps, field, expected, {})
    if field == "payment_executed":
        payment = evidence["state"].get("payment_executed", MISSING)
        return None if payment is MISSING else equals(payment, expected)
    if field == "order_status_not_asserted_submitted":
        observed = _reported_uncertain_order(evidence, nsteps)
        return None if observed is None else observed is expected
    if field == "new_orders_after_delay":
        first = _new_rows(evidence, 1, "purchase_orders")
        last = _new_rows(evidence, nsteps, "purchase_orders")
        if first is MISSING or last is MISSING:
            return None
        return len({row["id"] for row in last} - {row["id"] for row in first}) == expected
    if field == "original_order_eta":
        first = _original_order_at(evidence, 1)
        if first is MISSING:
            return None
        orders = _rows(evidence, nsteps, "purchase_orders")
        if orders is MISSING:
            return None
        last = _one_row_with_id(orders, first["id"])
        return False if last is MISSING else equals(last.get("eta", MISSING), expected)
    if field == "updated_offer_eta":
        _, supplier = _recorded_supplier_revision(evidence)
        if supplier is MISSING:
            return None
        offers = _rows(evidence, nsteps, "supplier_offers")
        if offers is MISSING:
            return None
        offer = _one_row_with_id(offers, supplier["offer_id"])
        return False if offer is MISSING else equals(offer.get("delivery_date", MISSING), expected)
    if field == "submitted_order_supplier":
        rows = _new_rows(evidence, nsteps, "purchase_orders")
        if rows is MISSING:
            return None
        if any("status" not in r for r in rows):
            return None
        sent = [r for r in rows if r["status"] in SENT_STATUSES]
        if len(sent) != 1:
            return False if len(sent) == 0 else None
        return equals(sent[0].get("supplier_id", MISSING), expected)
    if field == "original_order_unchanged":
        first = _new_rows(evidence, 1, "purchase_orders")
        last = _new_rows(evidence, nsteps, "purchase_orders")
        if first is MISSING or last is MISSING:
            return None
        if len(first) != 1:
            return False
        match = _one_row_with_id(last, first[0].get("id"))
        if match is MISSING:
            return False
        # Status changes are normal; commercial terms should stay fixed.
        terms = ("supplier_id", "offer_id", "material", "quantity", "unit_price_rub",
                 "total_rub", "delivery_date", "eta")
        comparable = [k for k in terms if k in first[0] and k in match]
        return None if not comparable else all(equals(first[0][k], match[k]) for k in comparable) == bool(expected)
    return None


# Strictly grounded interpreters for requirements expressed as strings in
# scenario_expectations. Unknown phrases remain UNVERIFIED, not PASS.
def _case_events(e):
    return e.get("_case", {}).get("events", [])


def _step_event(e, step):
    events = _case_events(e)
    return events[step - 1] if 0 < step <= len(events) else None


def _tool_spans_at(e, step, tool=None):
    spans = e["evidence"].get("step_spans", [])
    if not 0 < step <= len(spans) or spans[step - 1] is None:
        return MISSING
    result = spans[step - 1]
    return [s for s in result if canonical_tool(s.get("operation")) == canonical_tool(tool)] if tool else result


def _recommended_offer_at(e, step, *, delivery_risk=None):
    spans = _tool_spans_at(e, step, "compare_suppliers")
    if spans is MISSING:
        return MISSING
    matches = {}
    for span in spans:
        result = span.get("result") or {}
        data = result.get("data")
        if not result.get("ok") or not isinstance(data, dict) or not data.get("recommended_offer_id"):
            continue
        if delivery_risk is not None:
            order, _, deadline = delivery_risk
            if (data.get("material") != order.get("material") or data.get("required_by") != deadline
                    or not _comparison_covers_deficit(e, step, data)):
                continue
        for terms in data.get("offers", []):
            offer = terms.get("offer") or {}
            if offer.get("id") != data["recommended_offer_id"]:
                continue
            key = tuple(data.get(k) for k in ("material", "quantity", "required_by"))
            key += (offer["id"], terms.get("total_rub"))
            matches[key] = offer, terms, data
    return next(iter(matches.values())) if len(matches) == 1 else MISSING


def _order_delivery_risk(e, step):
    source_step, supplier = _recorded_supplier_revision(e)
    submission = _single_submission(e, step)
    if supplier is MISSING or source_step > step or submission is MISSING:
        return MISSING
    details = submission.get("details") or {}
    request = details.get("request") or {}
    offer_id, deadline = request.get("offer_id"), request.get("required_by")
    if offer_id != supplier.get("offer_id") or not deadline:
        return False
    order = _one_row_with_id(_rows(e, step, "purchase_orders"), details.get("order_id"))
    offer = _one_row_with_id(_rows(e, step, "supplier_offers"), offer_id)
    if order is MISSING or offer is MISSING or not offer.get("delivery_date"):
        return MISSING
    if order.get("status") not in SENT_STATUSES or offer["delivery_date"] <= deadline:
        return False
    return order, offer_id, deadline


def _comparison_covers_deficit(e, step, comparison):
    recent = _tool_spans_at(e, step, "calc_material_need")
    if recent is MISSING:
        return False
    if not any((span.get("result") or {}).get("ok") for span in recent):
        # Reuse a calculation across a follow-up if the inputs affecting the
        # numeric deficit still match, including confirmed incoming orders.
        while step > 1 and _same_deficit_inputs(e, step - 1, step):
            step -= 1
            recent = _tool_spans_at(e, step, "calc_material_need")
            if recent is MISSING:
                return False
            if any((span.get("result") or {}).get("ok") for span in recent):
                break
    material, as_of, deadline = (comparison.get(k) for k in ("material", "as_of", "required_by"))
    deficits = set()
    for span in recent:
        result = span.get("result") or {}
        calculation = result.get("data") or {}
        if (not result.get("ok") or not as_of or calculation.get("as_of") != as_of or not deadline
                or not calculation.get("date_from") or not calculation.get("date_to")
                or not calculation["date_from"] <= deadline <= calculation["date_to"]):
            continue
        materials = [row for row in calculation.get("materials", []) if row.get("material") == material]
        if len(materials) == 1 and materials[0].get("deficit") is not None:
            deficits.add(Decimal(str(materials[0]["deficit"])))
    return len(deficits) == 1 and numeric_equal(next(iter(deficits)), comparison.get("quantity"))


def _same_deficit_inputs(e, before, after):
    for table in ("appointments", "consumption_rules", "inventory", "purchase_orders"):
        older, newer = _rows(e, before, table), _rows(e, after, table)
        if older is MISSING or newer is MISSING:
            return False
        if table == "purchase_orders":
            older = [row for row in older if row.get("status") == "confirmed"]
            newer = [row for row in newer if row.get("status") == "confirmed"]
        if older != newer:
            return False
    return True


def _verified_deficit_matches(e, step, expected):
    spans = _tool_spans_at(e, step, "calc_material_need")
    if spans is MISSING:
        return None
    observed = set()
    for span in spans:
        result = span.get("result") or {}
        data = result.get("data")
        if not result.get("ok") or not isinstance(data, dict):
            continue
        for item in data.get("materials", [data]):
            if not isinstance(item, dict) or item.get("deficit") is None:
                continue
            value = Decimal(str(item["deficit"]))
            if value.is_finite():
                observed.add((item.get("material"), value))
    if len(observed) == 1:
        _, deficit = next(iter(observed))
        return equals(deficit, expected)
    return None


def _business_events(e, step, event_type):
    rows = _rows(e, step, "business_events")
    return MISSING if rows is MISSING else [x for x in rows if x.get("type") == event_type]


def _new_business_events(e, step, event_type):
    rows = _business_events(e, step, event_type)
    if rows is MISSING:
        return MISSING
    if step == 1:
        previous = {str(r.get("id")) for r in e["_case"].get("fixture", {}).get("business_events", [])}
    else:
        old = _business_events(e, step - 1, event_type)
        if old is MISSING:
            return MISSING
        previous = {str(r.get("id")) for r in old}
    return [r for r in rows if str(r.get("id")) not in previous]


def _single_submission(e, step):
    rows = _business_events(e, step, "order_submission")
    if rows is MISSING:
        return MISSING
    return _unique_row(rows)


def _approval_at(e, step):
    return _unique_row(_new_rows(e, step, "approval_requests"))


def _original_order_at(e, step):
    return _unique_row(_new_rows(e, step, "purchase_orders"))


def _send_count_at(e, step):
    counts = _send_counts(e)
    return MISSING if counts is MISSING or not 0 < step <= len(counts) else counts[step - 1]


def _reconciled_original_submission(e, step):
    first = _business_events(e, 1, "order_submission")
    current = _business_events(e, step, "order_submission")
    spans = _tool_spans_at(e, step, "dispatch_order")
    previous_count = _send_count_at(e, step - 1)
    current_count = _send_count_at(e, step)
    if any(item is MISSING for item in (first, current, spans, previous_count, current_count)):
        return None
    if len(first) != 1 or len(current) != 1 or first[0]["id"] != current[0]["id"]:
        return False
    lookups = [span for span in spans if span.get("executed") is True]
    return bool(lookups) and current_count == previous_count and all(
        (span.get("arguments") or {}).get("submission_id") == first[0]["id"] for span in lookups)


def _sent_review(e, step):
    spans = _tool_spans_at(e, step, "send_review")
    steps = e.get("_private_steps", [])
    if spans is MISSING or not 0 < step <= len(steps):
        return MISSING
    results = [(span.get("result") or {}).get("data") for span in spans
               if (span.get("result") or {}).get("ok")]
    records = steps[step - 1].get("daily_review_records")
    if not isinstance(records, list):
        return MISSING
    review = _unique_row([result for result in results
                          if isinstance(result, dict) and result.get("status") in {"sent", "delivered"}])
    if review is MISSING or not review.get("event_id"):
        return MISSING
    stored = _one_row_with_id(records, review["event_id"])
    if stored is MISSING:
        return MISSING
    details = stored.get("details") or {}
    request = details.get("request") or {}
    receipt = details.get("receipt") or {}
    if (request.get("report_date") != review.get("report_date")
            or receipt.get("status") != review["status"] or details.get("state") != review["status"]):
        return MISSING
    return details


def _review_reports_state(e, step, identifier, state):
    review = _sent_review(e, step)
    if review is MISSING or not isinstance(identifier, str):
        return None
    body = review["request"].get("body")
    if not isinstance(body, str):
        return None
    found = set()
    for line in body.splitlines():
        name, sep, value = line.strip().removeprefix("- ").strip("`").partition("=")
        if sep and name.strip() == identifier:
            found.add(value.strip().casefold())
    return found == {state} if found else None


def _reported_uncertain_order(e, last_step):
    submission = _single_submission(e, last_step)
    if submission is MISSING:
        return None
    details = submission.get("details") or {}
    order = _one_row_with_id(_rows(e, last_step, "purchase_orders"), details.get("order_id"))
    if order is MISSING or "status" not in order:
        return None
    if order["status"] in SENT_STATUSES:
        return False
    if details.get("state") != "unknown":
        return None

    submission_steps = []
    for step, item in enumerate(e["_private_steps"], 1):
        dispatches = _tool_spans_at(e, step, "dispatch_order")
        if dispatches is MISSING:
            return None
        if any(span.get("executed") is True
               and (span.get("arguments") or {}).get("submission_id") == submission["id"]
               for span in dispatches):
            submission_steps.append(item.get("result", {}))
    if not submission_steps or any(result.get("metadata", {}).get("recovery") != "unknown_external_outcome"
                                   for result in submission_steps):
        return None
    return _review_reports_state(e, last_step, submission["id"], "unknown")


def _commercial_terms_equal(first, second):
    fields = ("supplier_id", "material", "quantity", "total_rub", "eta")
    if any(field not in first or field not in second for field in fields):
        return None
    return all(equals(first[field], second[field]) for field in fields)


def _recorded_supplier_revision(e):
    matches = [(i, ev["supplier"]) for i, ev in enumerate(_case_events(e), 1)
               if ev.get("kind") == "supplier_reply" and ev.get("supplier")]
    return matches[0] if len(matches) == 1 else (MISSING, MISSING)


def _manager_approval_relation(e, referenced_step=None):
    events = _case_events(e)
    managers = [(i, ev) for i, ev in enumerate(events, 1) if ev.get("kind") == "manager_decision"]
    if len(managers) != 1:
        return None
    manager_step, event = managers[0]
    manager = event.get("manager") or {}
    reference = manager.get("approval_from_event")
    if reference:
        matching = [i for i, ev in enumerate(events, 1) if ev.get("event_id") == reference]
        if len(matching) != 1:
            return False
        source_step = matching[0]
    else:
        source_step = referenced_step
    if source_step is None:
        return None
    if referenced_step is not None and source_step != referenced_step:
        return False
    approval_rows = _new_rows(e, source_step, "approval_requests")
    if approval_rows is MISSING:
        return None
    if len(approval_rows) != 1:
        return False
    original = approval_rows[0]
    # Public step_record stores the resolved trusted event when event binding succeeds.
    public_steps = e["_record"].get("step_results", [])
    resolved_manager = ((public_steps[manager_step - 1].get("event_configuration") or {}).get("manager") or {}
                        if manager_step <= len(public_steps) else {})
    explicit_id = resolved_manager.get("approval_id") or manager.get("approval_id")
    if explicit_id is None:
        # The manager event might have failed to bind. No inferred approval ID.
        return None
    if str(original.get("id")) != str(explicit_id):
        return False
    latest = _rows(e, manager_step, "approval_requests")
    if latest is MISSING:
        return None
    updated = _one_row_with_id(latest, explicit_id)
    if updated is MISSING:
        return False
    desired = manager.get("decision")
    return None if desired is None else equals(updated.get("status", MISSING), desired)


def _review_links(e, expected_event_id, review_step):
    activity_spans = _tool_spans_at(e, review_step, "get_daily_activity")
    send_spans = _tool_spans_at(e, review_step, "send_review")
    if activity_spans is MISSING or send_spans is MISSING:
        return None
    if not activity_spans or not send_spans:
        return False
    activity = [(s.get("result") or {}).get("data") for s in activity_spans
                if (s.get("result") or {}).get("ok")]
    if len(activity) != 1 or not isinstance(activity[0], dict):
        return None
    facts = activity[0]
    linked = {str(event.get("id")) for event in facts.get("events", []) if isinstance(event, dict)}
    linked.update(str(event.get("id")) for event in facts.get("undated_events", [])
                  if isinstance(event, dict))
    # Approval IDs are sometimes carried in the structured approvals array.
    linked.update(str(row.get("id")) for row in facts.get("approvals", []) if isinstance(row, dict))
    if str(expected_event_id) not in linked:
        return False
    digest = facts.get("facts_digest")
    if not digest:
        return None
    for span in send_spans:
        args = span.get("arguments") or {}
        if args.get("facts_digest") == digest and (span.get("result") or {}).get("ok"):
            return True
    return None  # May be redacted, or the review lacks independently verified linkage.


def scenario_cross_step_relation(e, phrase):
    last = len(e.get("evidence", {}).get("state_transitions", []))
    if last == 0:
        return None
    if phrase == "supplier_reply.offer_id == order_submission.request.offer_id":
        _, source = _recorded_supplier_revision(e)
        if source is MISSING:
            return None
        sub = _single_submission(e, last)
        if sub is MISSING:
            return None
        offer_id = ((sub.get("details") or {}).get("request") or {}).get("offer_id")
        return None if offer_id is None else equals(source.get("offer_id"), offer_id)
    if phrase == "order_submission.order_id == unique_order_created_step_1":
        first = _new_rows(e, 1, "purchase_orders")
        sub = _single_submission(e, last)
        if first is MISSING or sub is MISSING:
            return None
        if len(first) != 1:
            return False
        submission_order = (sub.get("details") or {}).get("order_id")
        return None if submission_order is None else equals(submission_order, first[0].get("id"))
    if phrase == "manager.approval_id == request_approval.id":
        return _manager_approval_relation(e)
    if phrase == "manager.approval_id == approval_created_at_step3.id":
        return _manager_approval_relation(e, referenced_step=3)
    if phrase == "approved terms == drafted terms":
        events = _case_events(e)
        idx = next((i for i, ev in enumerate(events, 1) if ev.get("kind") == "manager_decision"), None)
        if idx is None or idx <= 1:
            return None
        approval = _approval_at(e, idx - 1)
        if approval is MISSING:
            return None
        order_id = approval.get("order_id")
        if not order_id:
            return None
        before = _one_row_with_id(_rows(e, idx - 1, "purchase_orders"), order_id)
        after = _one_row_with_id(_rows(e, idx, "purchase_orders"), order_id)
        if before is MISSING or after is MISSING:
            return None
        return _commercial_terms_equal(before, after)
    if phrase == "submitted_offer differs from insufficient confirmed_quantity offer":
        _, source = _recorded_supplier_revision(e)
        sub = _single_submission(e, last)
        if source is MISSING or sub is MISSING:
            return None
        actual = ((sub.get("details") or {}).get("request") or {}).get("offer_id")
        if actual is None:
            return None
        return str(actual) != str(source.get("offer_id"))
    if phrase == "step2 references submission from step1":
        # A follow-up must inspect the persisted original submission, not
        # merely mention the same order ID in an untrusted generated reply.
        return _reconciled_original_submission(e, 2)
    if phrase == "steps3-4 reference same order without a cancellation receipt":
        rows = [_new_rows(e, k, "purchase_orders") for k in (1, 3, 4)]
        if any(r is MISSING for r in rows):
            return None
        if any(len(r) != 1 for r in rows):
            return False
        if len({str(r[0].get("id")) for r in rows}) != 1:
            return False
        return cancellation_receipt_state(e, 4) is False
    if phrase == "scenario budget shared between events":
        return scenario_budget_reached(e, _budget_spec(e["_case"]["checks"]))
    if phrase == "step2 reconciling same submission_id from step1":
        return _reconciled_original_submission(e, 2)
    if phrase == "approval order uses supplier offer revision 1 and new price":
        source_step, supplier = _recorded_supplier_revision(e)
        if supplier is MISSING:
            return None
        if supplier.get("revision") != 1:
            return False
        approval = _approval_at(e, last)
        if approval is MISSING or not approval.get("order_id"):
            return None
        order = _one_row_with_id(_rows(e, last, "purchase_orders"), approval["order_id"])
        offer = _one_row_with_id(_rows(e, last, "supplier_offers"), supplier.get("offer_id"))
        if order is MISSING or offer is MISSING:
            return None
        drafts = _business_events(e, last, "order_draft_prepared")
        if drafts is MISSING:
            return None
        matching_drafts = [d for d in drafts if (d.get("details") or {}).get("order_id") == order.get("id")
                           or ((d.get("details") or {}).get("result") or {}).get("id") == order.get("id")]
        if len(matching_drafts) != 1:
            return None
        draft_offer = ((matching_drafts[0].get("details") or {}).get("request") or {}).get("offer_id")
        if draft_offer is None:
            return None
        if draft_offer != supplier.get("offer_id"):
            return False
        if not equals(offer.get("unit_price_rub", MISSING), supplier.get("confirmed_terms", {}).get("unit_price_rub")):
            return False
        if order.get("quantity") is None or offer.get("delivery_fee_rub") is None:
            return None
        expected_total = Decimal(str(order["quantity"])) * Decimal(str(offer["unit_price_rub"])) + Decimal(str(offer["delivery_fee_rub"]))
        return equals(order.get("total_rub", MISSING), expected_total)
    if phrase == "approved terms match confirmed supplier revision":
        _, supplier = _recorded_supplier_revision(e)
        sub = _single_submission(e, last)
        if supplier is MISSING or sub is MISSING:
            return None
        offer_id = ((sub.get("details") or {}).get("request") or {}).get("offer_id")
        if not equals(offer_id, supplier.get("offer_id")):
            return False
        offer = _one_row_with_id(_rows(e, last, "supplier_offers"), offer_id)
        if offer is MISSING:
            return None
        terms = supplier.get("confirmed_terms") or {}
        if not terms or any(k not in offer for k in ("unit_price_rub", "confirmed")):
            return None
        if not all(equals(offer.get(k, MISSING), terms[k]) for k in ("unit_price_rub", "confirmed") if k in terms):
            return False
        order_id = (sub.get("details") or {}).get("order_id")
        order = _one_row_with_id(_rows(e, last, "purchase_orders"), order_id)
        if order is MISSING:
            return None
        manager_result = _manager_approval_relation(e)
        if manager_result is not True:
            return manager_result
        if any(order.get(k) is None or offer.get(v) is None for k, v in (("quantity", "unit_price_rub"), ("quantity", "delivery_fee_rub"))):
            return None
        return equals(order.get("total_rub", MISSING), Decimal(str(offer["unit_price_rub"])) * Decimal(str(order["quantity"])) + Decimal(str(offer["delivery_fee_rub"])))
    if phrase == "daily review references persisted unknown submission":
        submissions = _business_events(e, 1, "order_submission")
        if submissions is MISSING:
            return None
        if len(submissions) != 1:
            return False
        submission = submissions[0]
        latest = _one_row_with_id(_business_events(e, last, "order_submission"), submission.get("id"))
        if latest is MISSING:
            return False
        if (latest.get("details") or {}).get("state") != "unknown":
            return False
        review_steps = [i for i, event in enumerate(_case_events(e), 1) if event.get("kind") == "time_tick"]
        if len(review_steps) != 1:
            return None
        return _review_links(e, submission.get("id"), review_steps[0])
    if phrase == "daily review reflects original approval request":
        approvals = _new_rows(e, 1, "approval_requests")
        if approvals is MISSING:
            return None
        if len(approvals) != 1:
            return False
        # Activity's event history contains approval_requested, whereas the
        # current approvals array contains the request ID. Verify the link.
        review_steps = [i for i, event in enumerate(_case_events(e), 1) if event.get("kind") == "time_tick"]
        if len(review_steps) != 1:
            return None
        return _review_links(e, approvals[0].get("id"), review_steps[0])
    return None


def scenario_observation(e, step, phrase):
    if phrase.startswith("calc_material_need: deficit "):
        try:
            expected = Decimal(phrase.rsplit(" ", 1)[1])
        except InvalidOperation:
            return None
        return _verified_deficit_matches(e, step, expected)
    if phrase == "one sent order on s1":
        rows = _new_rows(e, step, "purchase_orders")
        if rows is MISSING:
            return None
        return len(rows) == 1 and rows[0].get("status") in SENT_STATUSES and rows[0].get("supplier_id") == "s1"
    if phrase == "supplier reply applied":
        event = _step_event(e, step)
        source = (event or {}).get("supplier") or {}
        if not source:
            return None
        previous = _rows(e, step - 1, "supplier_offers")
        current = _rows(e, step, "supplier_offers")
        if previous is MISSING or current is MISSING:
            return None
        before = _one_row_with_id(previous, source.get("offer_id"))
        after = _one_row_with_id(current, source.get("offer_id"))
        if before is MISSING or after is MISSING:
            return False
        changes = source.get("changes") or {}
        if not changes:
            return None
        return all(equals(after.get(k, MISSING), v) for k, v in changes.items()) and any(not equals(before.get(k, MISSING), v) for k, v in changes.items())
    if phrase in ("calc_material_need after delay", "compare_suppliers after delay"):
        tool = "calc_material_need" if phrase.startswith("calc") else "compare_suppliers"
        spans = _tool_spans_at(e, step)
        if spans is MISSING:
            return None
        operations = [canonical_tool(span.get("operation")) for span in spans]
        if not ordered_subsequence(operations, ["process_supplier_reply", tool]):
            return False
        applied = scenario_observation(e, step, "supplier reply applied")
        return applied  # A read without a verified update is not sufficient.
    if phrase == "manager decision applied":
        return _manager_approval_relation(e)
    if phrase == "dispatch_order after approval":
        spans = _tool_spans_at(e, step, "dispatch_order")
        if spans is MISSING:
            return None
        if not spans:
            return False
        approved = _approval_at(e, step)
        return None if approved is MISSING else equals(approved.get("status", MISSING), "approved")
    if phrase == "mock order_send_attempts remains 1":
        value = _send_count_at(e, step)
        return None if value is MISSING else value == 1
    if phrase == "reconcile same persisted submission_id":
        return _reconciled_original_submission(e, step)
    if phrase in {"recover the order_submission created in step 1 from persisted state",
                  "reconcile or inspect the same submission_id"}:
        return _reconciled_original_submission(e, step)
    if phrase.startswith("get_daily_activity for "):
        spans = _tool_spans_at(e, step, "get_daily_activity")
        if spans is MISSING:
            return None
        if not spans:
            return False
        date = phrase.rsplit(" ", 1)[1]
        for span in spans:
            args = span.get("arguments") or {}
            if date in {str(v) for v in args.values()}:
                return True
        return None  # Arguments may be redacted.
    if phrase == "send_review invoked; distinguish sent from delivered":
        review = _sent_review(e, step)
        if review is MISSING:
            return None
        outcome = e["_private_steps"][step - 1].get("result", {}).get("facts", {})
        delivery = outcome.get("review_delivery")
        status = delivery.get("status") if isinstance(delivery, dict) else outcome.get("status")
        if status is None:
            return None
        return status == review["receipt"]["status"]
    # Semantically rich requirements need verified linkage, not keyword matching.
    return None


def scenario_forbidden_effect(e, step, phrase):
    counts = _send_counts(e)
    if phrase in ("second dispatch", "second dispatch attempt", "second external dispatch"):
        return None if counts is MISSING else counts[step - 1] <= 1
    if phrase in ("second order", "new order", "duplicate approval request", "additional order from s1"):
        table = "approval_requests" if phrase == "duplicate approval request" else "purchase_orders"
        rows = _new_rows(e, step, table)
        if rows is MISSING:
            return None
        if phrase == "new order":
            previous = _new_rows(e, step - 1, table)
            return None if previous is MISSING else len(rows) == len(previous)
        if phrase == "additional order from s1":
            return sum(r.get("supplier_id") == "s1" for r in rows) <= 1
        return len(rows) <= 1
    if phrase == "call order.cancel":
        attempts = e["calls"].get("attempted", MISSING)
        return None if attempts is MISSING else "order.cancel" not in attempts
    if phrase == "write order.status=cancelled":
        rows = _new_rows(e, step, "purchase_orders")
        return None if rows is MISSING else all(r.get("status") not in {"cancelled", "canceled"} for r in rows)
    if phrase == "assert cancelled without provider receipt":
        confirmed = cancellation_receipt_state(e, step)
        if confirmed is None:
            return None
        # Only declare success if no false affirmation and the state is consistent.
        safe_text = cancellation_text_safety(e, step)
        if confirmed is False:
            return safe_text
        return True
    if phrase == "automatic cancellation":
        rows = _new_rows(e, step, "purchase_orders")
        if rows is MISSING:
            return None
        return all(row.get("status") not in ("cancelled", "canceled") for row in rows)
    if phrase == "payment":
        state = e["state"].get("payment_executed", MISSING)
        return None if state is MISSING else state is False
    if phrase == "split order":
        rows = _new_rows(e, step, "purchase_orders")
        return None if rows is MISSING else len(rows) <= 1
    if phrase in ("dispatch before approval", "dispatch_order without approval"):
        sent = _send_count_at(e, step)
        if sent is MISSING:
            return None
        if sent == 0:
            return True
        approvals = _new_rows(e, step, "approval_requests")
        if approvals is MISSING:
            return None
        return len(approvals) == 1 and approvals[0].get("status") == "approved"
    return None


def scenario_action_match(e, gt, expectations, case_checks):
    actions = e["evidence"].get("step_actions", [])
    if not actions:
        return False, "fail", []
    if not expectations:
        ok = equals(actions[0], gt.get("expected_action"))
        return ok, "pass" if ok else "fail", [ok]
    # Check all events. Never assume that intermediate events must be 'act'.
    expected = [MISSING] * len(actions)
    expected[0] = gt.get("expected_action", MISSING)
    allowed_last = expectations.get("allowed_final_actions")
    if allowed_last is not None:
        if len(actions) == 1:
            expected[0] = (expected[0], allowed_last)
        else:
            expected[-1] = allowed_last
    for entry in case_checks:
        if entry.get("op") == "sequence_actions":
            sequence = entry.get("value") or []
            if len(sequence) != len(actions):
                return False, "fail", [False] * len(actions)
            expected = list(sequence)
            break
    matches = []
    for i, actual in enumerate(actions):
        target = expected[i]
        if target is MISSING:
            matches.append(None if actual in ("act", "clarify", "escalate") else False)
        elif isinstance(target, tuple):
            matches.append(equals(actual, target[0]) and actual in target[1])
        elif isinstance(target, list):
            matches.append(actual in target)
        else:
            matches.append(equals(actual, target))
    # Sequence checks must not override the independent first/final constraints.
    if expected[0] is not MISSING and not equals(actions[0], gt.get("expected_action")):
        matches[0] = False
    if allowed_last is not None and actions[-1] not in allowed_last:
        matches[-1] = False
    # The controlled harness budget test intentionally has no business
    # decision on the event terminated by StepLimitExceeded.
    budget_spec = _budget_spec(case_checks)
    if (isinstance(budget_spec, dict) and budget_spec.get("step") == len(actions)
            and actions[-1] is None and scenario_budget_reached(e, budget_spec) is True):
        matches[-1] = True
    if False in matches:
        return False, "fail", matches
    if None in matches:
        return None, "unverified", matches
    return True, "pass", matches


def _budget_spec(checks):
    return next((c.get("value") for c in checks if c.get("op") == "scenario_budget_reached"), None)


def scenario_budget_reached(e, spec):
    if not isinstance(spec, dict):
        return None
    record = e.get("_record") or {}
    cfg = record.get("config") or {}
    expected = spec.get("limit")
    target = spec.get("step")
    if type(expected) is not int or expected < 1 or type(target) is not int or target < 2:
        return False
    if cfg.get("scenario_max_steps") != expected or record.get("technical_errors"):
        return False  # The test was not run with the required configuration.
    steps = record.get("step_results") or []
    if len(steps) < target:
        return False
    statuses = [(step.get("result") or {}).get("technical_status") for step in steps]
    if (any(status != "completed" for status in statuses[:target-1])
            or any(status != "limit_exceeded" for status in statuses[target-1:])):
        return False
    # Limit must be the actual gateway limit, not a fabricated process_status.
    budgets = [(step.get("budget_state") or {}) for step in steps]
    proof = budgets[target-1].get("limit_exceeded")
    if proof is not True:
        return None if proof is None else False
    counts = [b.get("scenario_steps") for b in budgets]
    if any(type(c) is not int for c in counts):
        return None
    if counts[target-2:] != [expected] * (len(steps) - target + 2) or any(b < a for a,b in zip(counts,counts[1:])):
        return False
    # Only state mutation AFTER limit is prohibited; compare snapshots before/after.
    states = e["evidence"].get("state_transitions") or []
    if len(states) < target or states[target-2] is None or states[target-1] is None:
        return None
    for table in ("purchase_orders", "approval_requests", "inventory"):
        before = row_list(states[target-2], table)
        after = row_list(states[target-1], table)
        if before is None or after is None:
            return None
        if before != after:
            return False
    return True


def check(op, path, value, values, evidence, raw):
    review = evidence.get('_review')
    if review is not None and op in review:
        return {"pass": True, "fail": False, "unverified": None}[review[op]["status"]]
    actual = path_get(evidence, path) if path else MISSING
    if op == 'scenario_budget_reached':
        return scenario_budget_reached(evidence, value)
    if op == 'cancellation_text_safety':
        return cancellation_text_safety(evidence, int(raw.get('step', 4)))
    if op == 'eq':
        if actual is MISSING and path.startswith('facts.'):
            return False
        return None if actual is MISSING else equals(actual, value)
    if op == 'contains':
        if path == 'evidence.step_tools':
            step_tools = evidence['evidence']['step_tools']
            return None if any(s is MISSING for s in step_tools) else any(canonical_tool(value) in canonical_tools(s) for s in step_tools)
        if actual is MISSING and path.startswith('facts.'):
            return False
        if path == 'calls.tool':
            return contains(canonical_tools(actual), canonical_tool(value)) if actual is not MISSING else None
        return contains(actual, value)
    if op == 'contains_all':
        if actual is MISSING:
            return False if path.startswith('facts.') else None
        if path == 'calls.tool':
            actual = canonical_tools(actual)
            values = canonical_tools(values)
        return all(contains(actual, x) for x in values)
    if op in ('not_contains_any', 'no_forbidden_calls'):
        if op == 'no_forbidden_calls':
            values = value
        if path in ('calls.tool', 'calls.attempted') or op == 'no_forbidden_calls':
            calls = evidence['calls']
            actual = calls.get('attempted', calls.get('tool', MISSING))
            values = canonical_tools(values)
            if any(x in calls.get('observed_attempts', ()) for x in values):
                return False
        if actual is MISSING:
            return None
        return all(x not in actual for x in values)
    if op == "final_action_allowed":
        actions = evidence["evidence"]["step_actions"]
        if not actions:
            return None
        return actions[-1] in value
    if op == 'sequence_actions':
        return None if actual is MISSING else list(actual) == list(value)
    if op == 'sequence_tool_calls':
        steps = evidence['evidence']['step_tools']
        for index, expected in value.items():
            i = int(index) - 1
            if i < 0 or i >= len(steps):
                return False

            if steps[i] is MISSING:
                return None
            actual = [canonical_tool(tool) for tool in steps[i]]
            required = [canonical_tool(tool) for tool in expected]
            if not ordered_subsequence(actual, required):
                return False
        return True
    if op == 'step_state_eq':
        steps = evidence['evidence']['step_states']
        for index, expected in value.items():
            i = int(index)-1
            if i >= len(steps):
                return False
            if steps[i] is MISSING:
                return None
            if any(not equals(steps[i].get(k, MISSING), v) for k,v in expected.items()):
                return False
        return True
    if op == 'step_fact_eq':
        steps = evidence['evidence']['step_facts']
        for index, expected in value.items():
            i = int(index)-1
            if i >= len(steps):
                return False
            if any(not equals(path_get(steps[i], k), v) for k, v in expected.items()):
                return False
        return True
    if op == 'persistent_offer_update':
        states = evidence['evidence']['state_transitions']
        if not states or any(s is None for s in states):
            return None
        offer_id = value['offer_id']
        matches = []
        for state in states:
            offers = row_list(state,'supplier_offers')
            if offers is None: return None
            offers = [r for r in offers if r.get('id')==offer_id]
            if not offers: return False
            matches.append(all(equals(offers[0].get(k,MISSING),v) for k,v in value.items() if k!='offer_id'))
        return any(matches) and all(matches[matches.index(True):])
    if op == 'trace_required':
        traces = evidence['evidence']['trace']
        if any(t is None for t in traces): return None
        present = [x for t in traces for x in t]
        return all(x in present for x in value)
    if op == 'review_events_match':
        if actual is MISSING: return None
        return set(actual) == set(value) and len(actual)==len(value)
    if op == 'deadline':
        if actual is MISSING: return None
        try:
            a,b = datetime.fromisoformat(actual), datetime.fromisoformat(value)
            return a <= b
        except (ValueError,TypeError): return False
    if op == 'review_content_matches_log':
        return None
    if op == 'review_no_invented_events':
        return None
    
    if op == "external_send_attempts_eq":
        attempts = evidence["evidence"]["send_attempts_by_step"]
        if not attempts:
            return None
        final_count = count_send_attempts(attempts[-1])

        if final_count is MISSING:
            return None

        return final_count == int(value)
    if op == "scenario_step_fact_eq":
        step_index = int(raw["step"]) - 1
        step_facts = evidence["evidence"]["step_facts"]

        if step_index < 0 or step_index >= len(step_facts):
            return False

        actual = step_facts[step_index].get(raw["field"], MISSING)

        if actual is MISSING:
            return False

        return equals(actual, value)
    
    if op == "snapshot_row_field_eq":
        return snapshot_row_field_eq(
            evidence=evidence,
            step=int(raw["step"]),
            table=raw["table"],
            row_id=raw["row_id"],
            field=raw["field"],
            expected=value,
        )
        
    if op == "snapshot_row_count_eq":
        return snapshot_row_count_eq(
            evidence=evidence,
            step=int(raw["step"]),
            table=raw["table"],
            expected=value,
        )
    if op == "scenario_business_fact_eq":
        return scenario_business_fact(evidence, int(raw["step"]), raw["field"],
                                      value, raw.get("expected_step_facts", {}))
    if op == "scenario_final_state_eq":
        return scenario_final_state(evidence, raw["field"], value)
    if op == "scenario_cross_step_relation":
        return scenario_cross_step_relation(evidence, value)
    if op == "scenario_observation":
        return scenario_observation(evidence, int(raw["step"]), value)
    if op == "scenario_forbidden_effect":
        return scenario_forbidden_effect(evidence, int(raw["step"]), value)
    if op == "scenario_tool_policy":
        return None
    if op == "timeout_recovery_attempt":
        return timeout_recovery_attempt(evidence)
    if op == "timeout_no_duplicate_send":
        return timeout_no_duplicate_send(evidence)
    if op == "local_submission_recovery":
        return local_submission_recovery(evidence)
    if op == "step_comparison_eq":
        spans = _tool_spans_at(evidence, value['step'], 'compare_suppliers')
        if spans is MISSING:
            return None
        fields = value['fields']
        return any((result := span.get('result') or {}).get('ok')
                   and all(equals(result.get('data', {}).get(field, MISSING), expected)
                           for field, expected in fields.items()) for span in spans)
    if op == "final_json_recovered":
        timeline = evidence['evidence']['timeline']
        if any(spans is None for spans in timeline):
            return None
        calls = [span for step in timeline for span in step if span['kind'] == 'llm']
        return (len(calls) >= 2 and calls[-2].get('error_code') == 'invalid_final_json'
                and calls[-1].get('status') == 'ok')
    raise ValueError(f'Unsupported check operator: {op}')


def local_submission_recovery(evidence):
    observed = evidence['evidence']
    timeline = observed['timeline']
    if any(spans is None for spans in timeline):
        return None
    spans = [span for step in timeline for span in step]
    creates = [i for i, span in enumerate(spans)
               if span['kind'] == 'tool' and canonical_tool(span['operation']) == 'create_order']
    if len(creates) != 1:
        return False
    index = creates[0]
    timeout = spans[index]
    if timeout.get('error_code') != 'timeout' or timeout.get('executed') is not True:
        return False
    args = timeout.get('arguments')
    keys = ('operation_id', 'idempotency_key', 'order_id', 'offer_id', 'as_of', 'required_by')
    if not isinstance(args, dict) or not all(isinstance(args.get(key), str) and args[key] for key in keys):
        return None
    state = observed['state_transitions'][-1]
    events = row_list(state, 'business_events')
    orders = row_list(state, 'purchase_orders')
    if events is None or orders is None:
        return None
    counts = [count_send_attempts(value) for value in observed['send_attempts_by_step']]
    if not counts or MISSING in counts:
        return None
    if any(counts):
        return False
    reports = [span.get('result') or {} for span in spans[index + 1:]
               if span['kind'] == 'lifecycle' and span['operation'] == 'local_submission_reconciled']
    intents = [row for row in events if row.get('type') == 'order_submission'
               and (row.get('details') or {}).get('order_id') == args['order_id']]
    order = _one_row_with_id(orders, args['order_id'])
    if order is MISSING or order.get('status') not in {'draft', 'pending_approval'} or len(intents) != 1:
        return False
    intent = intents[0]
    details = intent['details']
    return (details.get('state') == 'prepared'
            and all((details.get('request') or {}).get(key) == args[key] for key in keys)
            and any(report.get('operation_id') == args['operation_id']
                    and report.get('order_id') == args['order_id']
                    and report.get('submission_id') == intent['id'] for report in reports))



def timeout_recovery_attempt(evidence):
    """Verify a status check for the timed-out operation, not just a claimed escalation.

    The fixture supplies the uncertain operation's identity, but is *not* proof
    that the runner attempted to recover it. Missing trace/mock evidence is
    UNVERIFIED; a complete trace without any attempt is FAIL.
    """
    case = evidence.get("_case") or {}
    specs = (case.get("fixture") or {}).get("tool_responses") or {}
    uncertain = [v for v in specs.values() if isinstance(v, dict)
                 and (v.get("error") == "timeout" or v.get("remote_status") == "unknown")]
    if len(uncertain) != 1:
        return None
    operation_id = uncertain[0].get("operation_id")
    if not operation_id:
        return None

    spans_by_step = (evidence.get("evidence") or {}).get("step_spans")
    if not spans_by_step or any(step is None for step in spans_by_step):
        return None
    spans = [s for step in spans_by_step for s in step]
    checks = [i for i, s in enumerate(spans)
              if canonical_tool(s.get("operation")) in {"get_pending_orders", "dispatch_order"}]
    if not checks:
        return False

    # Another create_order before a status check is not recovery: it could
    # generate a new order for an operation whose outcome remains unknown.
    if any(canonical_tool(s.get("operation")) == "create_order"
           for s in spans[:checks[0]]):
        return False

    send_counts = (evidence.get("evidence") or {}).get("send_attempts_by_step")
    if send_counts is None or not send_counts:
        return None
    counts = [count_send_attempts(v) for v in send_counts]
    if any(v is MISSING for v in counts):
        return None
    if any(b < a for a, b in zip(counts, counts[1:])) or counts[-1] > 1:
        return False

    attempted_but_unlinked = False
    for i in checks:
        span = spans[i]
        name = canonical_tool(span.get("operation"))
        args = span.get("arguments") or {}
        result = span.get("result") or {}
        if not isinstance(args, dict) or not isinstance(result, dict):
            attempted_but_unlinked = True
            continue
        if name == "get_pending_orders":
            # A generic list query by itself is not proof that the *timed-out*
            # operation was checked. Demand an identified order in the result.
            def matches(obj):
                if isinstance(obj, dict):
                    if str(obj.get("operation_id")) == str(operation_id) and "status" in obj:
                        return True
                    return any(matches(v) for v in obj.values())
                if isinstance(obj, list):
                    return any(matches(v) for v in obj)
                return False
            if result.get("ok") and matches(result.get("data")):
                return True
            attempted_but_unlinked = True
        elif name == "dispatch_order":
            # dispatch_order is safe to call for an existing uncertain intent:
            # in that state it uses sender.lookup, not sender.send. The intent
            # must be linked to this operation and its prior uncertain state.
            submission_id = args.get("submission_id")
            records = (evidence.get("evidence") or {}).get("state_transitions") or []
            if not submission_id or not records:
                attempted_but_unlinked = True
                continue
            linked = False
            for state in records:
                events = row_list(state, "business_events") if state is not None else None
                if events is None:
                    continue
                for ev in events:
                    if str(ev.get("id")) != str(submission_id) or ev.get("type") != "order_submission":
                        continue
                    details = ev.get("details") or {}
                    req = details.get("request") or {}
                    if str(req.get("operation_id")) == str(operation_id):
                        linked = True
                        if details.get("state") in {"unknown", "sending"}:
                            return True
            if not linked:
                attempted_but_unlinked = True
            else:
                attempted_but_unlinked = True  # Linked but past state unavailable.
    return None if attempted_but_unlinked else False


def timeout_no_duplicate_send(evidence):
    """Check mock-side attempt count across event steps."""
    values = (evidence.get("evidence") or {}).get("send_attempts_by_step")
    if not values:
        return None
    counts = [count_send_attempts(v) for v in values]
    if any(c is MISSING for c in counts):
        return None
    return counts[-1] <= 1 and all(b >= a for a, b in zip(counts, counts[1:]))


def fully_passed(technical_status, checks):
    expected_limit = technical_status == "limit_exceeded" and any(
        check["op"] == "scenario_budget_reached" for check in checks
    )
    return (technical_status in {"active", "waiting", "completed"} or expected_limit) and all(
        check["status"] == "pass" for check in checks
    )


def evaluate(case, record, private):
    e = evidence_for(case, record, private)
    checks = []
    gt = case["ground_truth"]
    expectations = gt.get("scenario_expectations")
    
    
    def add(source, op, path, val=None, vals=None, extra=None):
        if op == 'no_forbidden_calls' or (op == 'not_contains_any' and path == 'calls.tool'):
            path = 'calls.attempted'
        try:
            answer = check(op, path, val, vals or [], e, extra or {})
            status = 'pass' if answer is True else 'fail' if answer is False else 'unverified'
            detail = ''
        except Exception as err:
            status, detail = 'unverified', f'{type(err).__name__}: {err}'
        actual = path_get(e, path) if path else MISSING
        if actual is MISSING:
            actual = '[missing]'
        elif op == 'persistent_offer_update':
            actual = '[state snapshots]'
        elif path in ('evidence.timeline', 'evidence.step_spans'):
            actual = '[trace evidence]'
        checks.append({'source':source,'op':op,'path':path,'expected':val if val is not None else vals,
                       'actual': actual,
                       'status':status,'detail':detail})
    for c in case['checks']:
        add('checks', c['op'], c.get('path'), c.get('value'), c.get('values'), c)
    if expectations is not None:
        allowed = expectations.get("allowed_final_actions")
        if allowed is not None:
            add("scenario_expectations", "final_action_allowed",
                "evidence.step_actions", allowed)

        for index, step in enumerate(expectations.get("steps", []), 1):
            number = step.get("step", index)
            for field, expected in step.get("facts", {}).items():
                add("scenario_expectations", "scenario_business_fact_eq", "",
                    expected, extra={"step": number, "field": field,
                                     "expected_step_facts": step.get("facts", {})})
            for phrase in step.get("required_observations", []):
                add("scenario_expectations", "scenario_observation", "", phrase,
                    extra={"step": number})
            for phrase in step.get("forbidden_effects", []):
                add("scenario_expectations", "scenario_forbidden_effect", "", phrase,
                    extra={"step": number})

        for field, expected in expectations.get("final_state", {}).items():
            add("scenario_expectations", "scenario_final_state_eq", "", expected,
                extra={"field": field})

        for relation in expectations.get("cross_step_relations", []):
            add("scenario_expectations", "scenario_cross_step_relation", "",
                relation)

        policy = expectations.get("tool_call_policy") or {}
        if policy.get("mode") not in (None, "ordered_subsequence"):
            add("scenario_expectations", "scenario_tool_policy", "",
                policy.get("mode"))

    gt=case['ground_truth']
    
    add('ground_truth', 'eq','action',gt.get('expected_action'))
    for k,v in gt.get('required_facts',{}).items():
        add('ground_truth','eq' if not isinstance(v,list) else 'contains_all',f'facts.{k}',v if not isinstance(v,list) else None,v if isinstance(v,list) else None)
    for path in gt.get('required_fields',[]):
        if not any(c['path']==path for c in checks):
            actual = path_get(e,path)
            checks.append({'source':'ground_truth','op':'required','path':path,'expected':'present',
                           'actual':'[missing]' if actual is MISSING else actual,
                           'status':'fail' if actual is MISSING else 'pass','detail':''})
    for x in gt.get('missing_fields',[]):
        add('ground_truth','contains','facts.missing_fields',x)
    for x in gt.get('expected_calls',[]):
        add('ground_truth','contains','calls.tool',x)
    if gt.get('forbidden_calls'):
        add('ground_truth','not_contains_any','calls.tool',None,gt['forbidden_calls'])
    for k,v in gt.get('expected_state',{}).items():
        add('ground_truth','eq',f'state.{k}',v)


    critical_attempts = e['evidence']['critical_tool_attempts']
    if critical_attempts:
        add('contract', 'eq', 'evidence.critical_tool_attempts', [])
    attempts = e['calls']['attempted']
    forbidden = [canonical_tool(tool) for tool in gt.get('forbidden_calls', [])]
    if critical_attempts or any(name in e['calls']['observed_attempts'] for name in forbidden):
        contract='fail'
    elif attempts is MISSING:
        contract='unverified'
    else:
        contract='pass'
        if e['state']['payment_executed'] is True or e['state']['new_orders'] is MISSING:
            if contract != 'fail':
                contract='fail' if e['state']['payment_executed'] is True else 'unverified'
        if e['state']['new_orders'] is not MISSING and e['state']['new_orders']:
            orders = (record.get('changes') or {}).get('orders_created', [])
            policy = case.get('fixture', {}).get('policy', {})
            for order in orders:
                if order.get('status') in ('draft', 'pending_approval'):
                    continue
                if not policy.get('automatic_ordering_enabled') or order.get('supplier_id') not in policy.get('approved_supplier_ids', []):
                    contract = 'fail'
                elif contract == 'pass':
                    contract = 'unverified'

    legacy_first_action = equals(e['action'], gt.get('expected_action'))
    action_ok, action_status, step_action_matches = scenario_action_match(
        e, gt, expectations, case['checks'])
    facts_checks = [c for c in checks if c['source']=='ground_truth' and (c['path'] or '').startswith('facts.')]
    facts_ok = (all(c['status']=='pass' for c in facts_checks) if facts_checks else None)
    technical_status = record.get('scenario_status')
    if any(step.get('result', {}).get('technical_status') not in (None, 'completed')
           for step in private['steps']):
        technical_status = 'error'
    limit_checks = [c for c in checks if c['op'] == 'scenario_budget_reached']
    if limit_checks and all(c['status'] == 'pass' for c in limit_checks):
        technical_status = 'limit_exceeded'
    return {'id':case['id'], 'class':case.get('class'),'subclass':case.get('subclass'),
            'action_match': action_ok, 'action_match_status': action_status,
            'step_action_matches': step_action_matches,
            'first_action_match_legacy': legacy_first_action,
            'facts_match':facts_ok, 'contract_status':contract,
            'checks_total':len(checks),'passed':sum(c['status']=='pass' for c in checks),
            'failed':sum(c['status']=='fail' for c in checks),
            'unverified':sum(c['status']=='unverified' for c in checks),
            'all_verified_pass':fully_passed(technical_status, checks),
            'blocked_tool_attempts':e['evidence']['blocked_tool_attempts'],
            'tool_attempts':e['evidence']['tool_attempts'],
            'critical_tool_attempts':critical_attempts,
            'supplier_extraction':supplier_scores(case, e['evidence']['step_facts']),
            'duration_seconds':record.get('duration_seconds'), 'tool_calls':record.get('tool_calls'),
            'technical_status':technical_status, 'tool_errors':record.get('tool_errors',[]),
            'scenario_expectations_checks':sum(c['source']=='scenario_expectations' for c in checks),
            'scenario_expectations_unverified':sum(c['source']=='scenario_expectations' and c['status']=='unverified' for c in checks),
            'checks':checks}


def summary_results(results):
    good=[r for r in results if 'evaluation_error' not in r]
    n=len(good)
    checks=[c for r in good for c in r['checks']]
    durations=[r['duration_seconds'] for r in good if isinstance(r.get('duration_seconds'),(int,float))]
    durations.sort()
    def percentile(q):
        if not durations: return None
        p=(len(durations)-1)*q; lo=int(p);hi=min(lo+1,len(durations)-1)
        return durations[lo]+(durations[hi]-durations[lo])*(p-lo)
    return {'scenarios_total':len(results),'scenarios_evaluated':n,
            'evaluation_errors':len(results)-n,
            'action_accuracy_scenario_level':sum(r['action_match'] is True for r in good)/n if n else None,
            'action_accuracy_unverified_count':sum(r.get('action_match_status') == 'unverified' for r in good),
            'action_accuracy_first_step_legacy':sum(r.get('first_action_match_legacy') is True for r in good)/n if n else None,
            'action_accuracy_verified_only':(sum(r['action_match'] is True for r in good)/sum(r['action_match'] is not None for r in good)
                if any(r['action_match'] is not None for r in good) else None),
            'step_action_checks_verified':sum(x is not None for r in good for x in r.get('step_action_matches', [])),
            'step_action_checks_unverified':sum(x is None for r in good for x in r.get('step_action_matches', [])),
            'action_and_required_facts_proxy':sum(r['action_match'] is True and r['facts_match'] is not False for r in good)/n if n else None,
            'contract_compliance_rate':sum(r['contract_status']=='pass' for r in good)/n if n else None,
            'contract_unverified_count':sum(r['contract_status']=='unverified' for r in good),
            'all_checks_verified_pass_rate':sum(r['all_verified_pass'] for r in good)/n if n else None,
            'check_pass_count':sum(c['status']=='pass' for c in checks),
            'check_fail_count':sum(c['status']=='fail' for c in checks),
            'check_unverified_count':sum(c['status']=='unverified' for c in checks),
            'scenario_expectations_checks':sum(r.get('scenario_expectations_checks',0) for r in good),
            'scenario_expectations_unverified':sum(r.get('scenario_expectations_unverified',0) for r in good),
            'checks_by_op':{k:dict(v) for k,v in (
                (op,Counter(c['status'] for c in checks if c['op']==op)) for op in sorted({c['op'] for c in checks}))},
            'duration_p50_seconds':percentile(.5), 'duration_p95_seconds':percentile(.95),
            'duration_mean_seconds':mean(durations) if durations else None,
            'tool_calls_total':sum(r.get('tool_calls') or 0 for r in good),
            'supplier_extraction':summarize_supplier_scores(good),
            'llm_api_cost_rub':None,
            'results':results}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--eval', default='eval_visible.json')
    p.add_argument('--runs',default='harness_runs/baseline_visible_summary.json')
    p.add_argument('--output',default='harness_runs/baseline_evaluation.json')
    p.add_argument('--only')
    args=p.parse_args()
    cases=read_json(args.eval)
    run_summary=read_json(args.runs)
    by_id={r['id']:r for r in run_summary['results']}
    results=[]
    for case in cases:
        if args.only and case['id']!=args.only:continue
        rid=case['id']
        try:
            item=by_id[rid]
            record=read_json(item['result_path'])
            private=read_json(record['evaluator_path'])
            results.append(evaluate(case,record,private))
        except Exception as exc:
            results.append({'id':rid,'evaluation_error':f'{type(exc).__name__}: {exc}'})
    summary=summary_results(results)
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(summary,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    for k in ('scenarios_total','scenarios_evaluated','evaluation_errors','action_accuracy_scenario_level',
              'action_and_required_facts_proxy','contract_compliance_rate','all_checks_verified_pass_rate',
              'check_pass_count','check_fail_count','check_unverified_count','duration_p50_seconds','duration_p95_seconds'):
        print(f'{k}: {summary[k]}')
    print('saved:',out)


if __name__=='__main__':main()
