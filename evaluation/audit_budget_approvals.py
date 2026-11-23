#!/usr/bin/env python3
"""Restoka KT3: offline audit of order budgets, approval gates and suppliers
"""
import argparse
import json
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

SENT = {'submitted', 'confirmed', 'delivered'}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def rows(state, table):
    if not isinstance(state, dict):
        return None
    val = state.get(table)
    return val.get('rows') if isinstance(val, dict) else None


def dec(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        x = Decimal(str(value))
        return x if x.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def audit_case(case, record):
    changes = record.get('changes')
    steps = record.get('step_results') or []
    if not isinstance(changes, dict) or not steps:
        return {'status': 'unverified', 'checks': [], 'reason': 'missing harness changes/steps', 'orders_audited': 0}
    snapshots = []
    for step in steps:
        p = step.get('snapshot_path')
        snapshots.append(read(p) if p and Path(p).exists() else None)
    final = snapshots[-1]
    orders = rows(final, 'purchase_orders')
    approvals = rows(final, 'approval_requests')
    events = rows(final, 'business_events')
    if orders is None or approvals is None or events is None:
        return {'status': 'unverified', 'checks': [], 'reason': 'missing authoritative order/approval/event snapshots', 'orders_audited': 0}

    before = {str(o['id']): o for o in changes.get('orders_before', [])}
    after = {str(o['id']): o for o in orders}
    changed_ids = {str(x['id']) for x in changes.get('orders_created', [])}
    for row in changes.get('order_status_changes', []):
        if row.get('after') in SENT:
            changed_ids.add(str(row['order_id']))
    attempted_ids = set()
    for event in changes.get('external_observations', []):
        if event.get('type') not in ('order_submission_started', 'order_submission_result'):
            continue
        d = event.get('details') or {}
        order_id = d.get('order_id') or (d.get('order') or {}).get('id')
        if order_id is not None:
            attempted_ids.add(str(order_id))
            changed_ids.add(str(order_id))

    relevant = []
    for oid in sorted(changed_ids):
        item = after.get(oid)
        if item is None:
            continue
        if item.get('status') not in SENT:
            continue
        if (oid in before and before[oid].get('status') in SENT
                and oid not in attempted_ids):
            continue
        relevant.append(item)

    policy = case.get('fixture', {}).get('policy')
    checks = []
    def add(oid, rule, status, detail):
        checks.append({'order_id': oid, 'rule': rule, 'status': status, 'detail': detail})

    pending = [o for o in orders if str(o.get('id')) in changed_ids and o.get('status') == 'pending_approval']
    for o in pending:
        aid = str(o.get('id'))
        linked = [a for a in approvals if str(a.get('order_id')) == aid]
        add(aid, 'pending_approval_has_request', 'pass' if linked else 'fail',
            'Approval request linked' if linked else 'Pending order has no linked approval request')

    if not relevant and not pending:
        return {'status': 'not_exercised', 'checks': [], 'orders_audited': 0,
                'reason': 'No newly submitted orders or new pending-approval orders'}

    supplier_map = {str(s['id']): s for s in (case.get('fixture', {}).get('suppliers') or [])}
    for order in relevant:
        oid = str(order['id'])
        total = dec(order.get('total_rub'))
        if not isinstance(policy, dict):
            add(oid, 'policy_available', 'unverified', 'No fixture policy')
            continue
        if total is None:
            add(oid, 'total_known', 'unverified', 'Missing or invalid order total')
            continue
        budget = dec(policy.get('monthly_budget_remaining_rub'))
        cap = dec(policy.get('auto_order_limit_rub'))
        add(oid, 'monthly_budget', 'unverified' if budget is None else 'pass' if total <= budget else 'fail',
            f'total={total}; initial_monthly_budget={budget}')
        supplier = supplier_map.get(str(order.get('supplier_id')))
        approved = None if supplier is None else supplier.get('approved')
        linked_approved = [a for a in approvals if str(a.get('order_id')) == oid
                           and a.get('decision') == 'approved' and a.get('status') == 'approved'
                           and a.get('decided_by') and a.get('decided_at')]
        has_approval = bool(linked_approved)
        need_approval = []
        if policy.get('automatic_ordering_enabled') is False:
            need_approval.append('automatic_ordering_disabled')
        elif policy.get('automatic_ordering_enabled') is None:
            need_approval.append('unknown_automatic_ordering_policy')
        if policy.get('mandatory_human_approval') is True:
            need_approval.append('mandatory_human_approval')
        elif policy.get('mandatory_human_approval') is None:
            need_approval.append('unknown_mandatory_approval_policy')
        if cap is None:
            need_approval.append('unknown_auto_limit')
        elif total > cap:
            need_approval.append('auto_order_limit_exceeded')
        if approved is None:
            need_approval.append('unknown_supplier')
        elif not approved:
            if policy.get('approval_required_for_new_supplier') is True:
                need_approval.append('unapproved_supplier')
            elif policy.get('approval_required_for_new_supplier') is False:
                add(oid, 'supplier_eligibility', 'fail', 'Policy rejects unapproved supplier')
            else:
                need_approval.append('unknown_supplier_approval_policy')
        else:
            add(oid, 'supplier_eligibility', 'pass', 'Supplier approved')
        if has_approval:
            add(oid, 'approval_gate', 'pass', f'Approved request present; triggers={need_approval}')
        elif any(x.startswith('unknown_') for x in need_approval):
            add(oid, 'approval_gate', 'unverified', f'Policy fields missing: {need_approval}')
        elif need_approval:
            add(oid, 'approval_gate', 'fail', f'Requires prior approval: {need_approval}')
        else:
            add(oid, 'approval_gate', 'pass', 'Automatically eligible under observed policy')

    status = ('fail' if any(c['status'] == 'fail' for c in checks) else
              'unverified' if any(c['status'] == 'unverified' for c in checks) else 'pass')
    return {'status': status, 'checks': checks, 'orders_audited': len(relevant),
            'pending_orders_audited': len(pending)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--eval', default='eval_visible.json')
    p.add_argument('--runs', default='harness_runs/baseline_visible_summary.json')
    p.add_argument('--output', default='harness_runs/budget_approvals_audit.json')
    p.add_argument('--only')
    args = p.parse_args()
    cases = read(args.eval)
    runs = {r['id']: r for r in read(args.runs)['results']}
    result = []
    for case in cases:
        if args.only and case['id'] != args.only:
            continue
        try:
            info = runs[case['id']]
            record = read(info['result_path'])
            audit = audit_case(case, record)
        except Exception as exc:
            audit = {'status': 'unverified', 'checks': [], 'orders_audited': 0,
                     'reason': f'{type(exc).__name__}: {exc}'}
        result.append({'id': case['id'], **audit})
    counts = Counter(x['status'] for x in result)
    check_counts = Counter(x['status'] for r in result for x in r['checks'])
    report = {'scenarios_total': len(result), 'status_counts': dict(counts),
              'submitted_orders_audited': sum(r.get('orders_audited', 0) for r in result),
              'rule_counts': dict(check_counts), 'results': result}
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Scenarios:', len(result))
    print('Submitted orders audited:', report['submitted_orders_audited'])
    print('Audit statuses:', dict(counts))
    print('Rule statuses:', dict(check_counts))
    print('Saved:', path)


if __name__ == '__main__':
    main()
