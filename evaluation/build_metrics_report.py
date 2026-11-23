#!/usr/bin/env python3
"""Restoka KT3: merge offline baseline evaluation and supplementary audits.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import json
from pathlib import Path

if __package__:
    from .evaluate_baseline_visible import fully_passed
else:
    from evaluate_baseline_visible import fully_passed

REVIEW_OPS = frozenset({
    'deadline', 'review_events_match', 'review_content_matches_log',
    'review_no_invented_events',
})
STATUSES = {'pass', 'fail', 'unverified'}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def ratio(a, b):
    return a / b if b else None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--evaluation', default='harness_runs/baseline_evaluation.json')
    p.add_argument('--budget', default='harness_runs/budget_approvals_audit.json')
    p.add_argument('--reviews', default='harness_runs/daily_review_audit.json')
    p.add_argument('--output', default='harness_runs/kt3_final_metrics.json')
    args = p.parse_args()

    baseline = read(args.evaluation)
    budget = read(args.budget)
    reviews = read(args.reviews)

    results = deepcopy(baseline['results'])
    if any('evaluation_error' in r for r in results):
        raise ValueError('Baseline evaluation has evaluation_error entries; resolve them before merging')
    result_by_id = {r['id']: r for r in results}
    if len(result_by_id) != len(results):
        raise ValueError('Duplicate scenario IDs in baseline evaluation')
    if len(results) != baseline['scenarios_evaluated']:
        raise ValueError('Scenario count mismatch in baseline evaluation')

    changed = []
    replaced_keys = set()
    for review in reviews['results']:
        rid = review['id']
        if rid not in result_by_id:
            raise ValueError(f'Review scenario {rid} absent from baseline evaluation')
        for op, replacement in review['checks'].items():
            if op not in REVIEW_OPS:
                raise ValueError(f'Unexpected review op: {op}')
            status = replacement['status']
            if status not in STATUSES:
                raise ValueError(f'Invalid audit status: {status}')
            candidates = [c for c in result_by_id[rid]['checks'] if c['op'] == op]
            if len(candidates) != 1:
                raise ValueError(f'Expected one {op} check for {rid}, got {len(candidates)}')
            c = candidates[0]
            if c['status'] != 'unverified':
                if c['status'] != status:
                    raise ValueError(
                        f'Conflicting verified check: {rid}/{op}, '
                        f'evaluator={c["status"]}, audit={status}'
                    )
                # Уже подтверждено, аудит согласен.
                # Ничего не перезаписываем.
                continue

            c['status'] = status
            c['detail'] = 'daily_review_audit: ' + replacement.get('detail', '')
            replaced_keys.add((rid, op))
            changed.append({
                'id': rid,
                'op': op,
                'old': 'unverified',
                'new': status,
                'detail': replacement.get('detail', '')
            })

    budget_rows = budget['results']
    if {r['id'] for r in budget_rows} != set(result_by_id):
        raise ValueError('Budget audit and baseline evaluation cover different scenario IDs')
    budget_status = Counter(r['status'] for r in budget_rows)
    budget_rules = Counter(c['status'] for r in budget_rows for c in r['checks'])
    if sum(budget_status.values()) != len(budget_rows):
        raise AssertionError('Budget accounting mismatch')

    all_checks = [c for r in results for c in r['checks']]
    aggregate = Counter(c['status'] for c in all_checks)
    by_op = defaultdict(Counter)
    for c in all_checks:
        by_op[c['op']][c['status']] += 1
    for r in results:
        c = Counter(check['status'] for check in r['checks'])
        r['passed'], r['failed'], r['unverified'] = c['pass'], c['fail'], c['unverified']
        r['all_verified_pass'] = fully_passed(r.get('technical_status'), r['checks'])

    n = len(results)
    all_pass = sum(r['all_verified_pass'] for r in results)
    outcome = {
        'method': 'Offline merge; review checks replace existing unverified statuses, budget audit separately reported',
        'scenarios_total': n,
        'review_checks_replaced': len(changed),
        'checks_total': len(all_checks),
        'check_pass_count': aggregate['pass'],
        'check_fail_count': aggregate['fail'],
        'check_unverified_count': aggregate['unverified'],
        'checks_by_op': {op: dict(counts) for op, counts in sorted(by_op.items())},
        'all_checks_passed_scenarios': all_pass,
        'all_checks_verified_pass_rate': ratio(all_pass, n),
        'action_accuracy_scenario_level': baseline['action_accuracy_scenario_level'],
        'action_and_required_facts_proxy': baseline['action_and_required_facts_proxy'],
        'duration_p50_seconds': baseline['duration_p50_seconds'],
        'duration_p95_seconds': baseline['duration_p95_seconds'],
        'duration_mean_seconds': baseline['duration_mean_seconds'],
        'tool_calls_total': baseline['tool_calls_total'],
        'supplier_extraction': baseline.get('supplier_extraction'),
        'llm_api_cost_rub': baseline['llm_api_cost_rub'],
        'contract_invariants_proxy_original': baseline['contract_compliance_rate'],
        'contract_compliance_rate_full': None,
        'contract_note': 'Full Contract Compliance Rate not derived: no submitted orders in baseline run; independent integration tests are not a baseline success-rate denominator.',
        'budget_audit': {
            'scenarios_total': len(budget_rows),
            'status_counts': dict(budget_status),
            'rule_counts': dict(budget_rules),
            'submitted_orders_audited': budget['submitted_orders_audited'],
        },
        'review_audit': {'scenarios': reviews['scenarios'],
                         'status_counts': dict(Counter(x['new'] for x in changed))},
        'integration_tests': {
            'purchase_policy': {'passed': 5, 'total': 5},
            'idempotency_events': {'passed': 5, 'total': 5},
            'order_lifecycle': {'passed': 5, 'total': 5},
            'note': 'Counts supplied from independently observed terminal logs; not rerun by this script.'
        },
        'baseline_results': results,
        'review_replacements': changed,
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(outcome, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print('Scenarios:', n)
    print('Replaced review checks:', len(changed))
    print('Checks: PASS={pass_} FAIL={fail} UNVERIFIED={unverified}'.format(
        pass_=aggregate['pass'], fail=aggregate['fail'], unverified=aggregate['unverified']))
    print('Scenarios passing all checks:', f'{all_pass}/{n} ({ratio(all_pass,n):.1%})')
    print('Budget audit:', dict(budget_status), '(separate; not added to eval checks)')
    print('Full Contract Compliance Rate: not established')
    print('Saved:', out)


if __name__ == '__main__':
    main()
