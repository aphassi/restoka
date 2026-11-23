#!/usr/bin/env python3
"""Restoka KT3: audit daily review delivery and contents from trusted harness snapshots.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4
import json
from zoneinfo import ZoneInfo


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def rows(state, table):
    if not isinstance(state, dict):
        return None
    entry = state.get(table)
    return entry.get('rows') if isinstance(entry, dict) else None


def result(status, reason):
    assert status in ('pass', 'fail', 'unverified')
    return {'status': status, 'reason': reason}


def parse_body(body):
    """Accept only structured JSON with full event records, not IDs in free text.

    Format: {"report_date":"YYYY-MM-DD", "events":[{"id":...,"type":...,"date":...},...]}
    """
    if not isinstance(body, str):
        return None
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or set(data) != {'report_date', 'events'} or not isinstance(data.get('events'), list):
        return None
    if not all(isinstance(e, dict) and set(e) == {'id', 'type', 'date'}
               and isinstance(e.get('id'), str)
               and isinstance(e.get('type'), str) and isinstance(e.get('date'), str)
               for e in data['events']):
        return None
    return data


def report_date_for(case):
    request = case.get("input")
    if isinstance(request, dict) and isinstance(request.get("report_date"), str):
        return request["report_date"]
    return case["fixture"]["now"][:10]


def audit_review(case, state):
    fixture = case['fixture']
    report_date = report_date_for(case)
    expected_log = [e for e in fixture.get('action_log', []) if e.get('date') == report_date]
    expected_by_id = {e['id']: e for e in expected_log}
    expected_ids = set(expected_by_id)
    required_ids = set(case.get('ground_truth', {}).get('required_facts', {}).get('event_ids', []))
    all_events = rows(state, 'business_events')
    checks = {}
    ops = ['review_events_match', 'deadline', 'review_content_matches_log', 'review_no_invented_events']
    if all_events is None:
        return {k: result('unverified', 'No trusted business_events snapshot') for k in ops}

    reviews = [r for r in all_events if r.get('type') == 'daily_review'
               and (r.get('details') or {}).get('request', {}).get('report_date') == report_date]
    if not reviews:
        return {k: result('fail', 'No daily_review operation persisted for expected report date') for k in ops}
    if len(reviews) > 1:
        return {k: result('fail', 'Multiple daily_review operations persisted for same date') for k in ops}

    record = reviews[0]
    details = record.get('details') or {}
    request = details.get('request') or {}
    receipt = details.get('receipt') or {}
    state_status = details.get('state')
    receipt_status = receipt.get('status')
    body = request.get('body')
    parsed = parse_body(body)

    if state_status in ('failed', 'rejected'):
        delivery = result('fail', f'Delivery terminally {state_status}')
    elif state_status != 'delivered' or receipt_status != 'delivered':
        delivery = result('unverified', f'Delivery not confirmed (state={state_status}, receipt={receipt_status})')
    else:
        delivered = receipt.get('delivered_at')
        sent = receipt.get('sent_at')
        try:
            delivery_dt = datetime.fromisoformat(delivered)
            sent_dt = datetime.fromisoformat(sent)
            tz = ZoneInfo(fixture['timezone'])
            deadline = datetime.combine(date.fromisoformat(report_date) + timedelta(days=1), time(0,10), tz)
            if any(t.tzinfo is None or t.utcoffset() is None for t in (delivery_dt, sent_dt)):
                raise ValueError('timestamps not aware')
            if sent_dt > delivery_dt:
                delivery = result('fail', 'Delivery timestamp precedes sending timestamp')
            else:
                delivery = result('pass' if delivery_dt <= deadline else 'fail',
                                  f'Delivered at {delivered}; deadline {deadline.isoformat()}')
        except (TypeError, ValueError, KeyError) as exc:
            delivery = result('unverified', f'Invalid/missing trusted delivery timestamp: {exc}')
    checks['deadline'] = delivery

    if parsed is None:
        status = 'fail' if state_status == 'delivered' else 'unverified'
        for op in ('review_events_match', 'review_content_matches_log', 'review_no_invented_events'):
            checks[op] = result(status, 'Delivered body is not a verifiable structured event report'
                                if status == 'fail' else 'Report body unavailable or not structured; delivery not confirmed')
        return checks

    if state_status != 'delivered' or receipt_status != 'delivered':
        status = 'fail' if state_status in ('failed', 'rejected') else 'unverified'
        for op in ('review_events_match', 'review_content_matches_log', 'review_no_invented_events'):
            checks[op] = result(status, 'No confirmed delivery of structured report text')
        return checks

    entries = parsed['events']
    ids = [e['id'] for e in entries]
    if parsed.get('report_date') != report_date:
        for op in ('review_events_match', 'review_content_matches_log', 'review_no_invented_events'):
            checks[op] = result('fail', 'Report date does not match expected date')
        return checks
    duplicate = len(ids) != len(set(ids))
    actual_ids = set(ids)
    checks['review_events_match'] = result(
        'pass' if not duplicate and actual_ids == expected_ids and required_ids.issubset(actual_ids) else 'fail',
        f'Expected IDs {sorted(expected_ids)}, reported {ids}')
    invented = actual_ids - expected_ids
    checks['review_no_invented_events'] = result(
        'pass' if not duplicate and not invented else 'fail',
        f'Unexpected IDs {sorted(invented)}; duplicates={duplicate}')
    mismatches = []
    for event in entries:
        known = expected_by_id.get(event['id'])
        if known is None:
            continue
        for key in ('type', 'date'):
            if event.get(key) != known.get(key):
                mismatches.append(f"{event['id']}.{key}")
        for key in known.keys() - {'id','type','date'}:
            if event.get(key) != known[key]:
                mismatches.append(f"{event['id']}.{key}")
    checks['review_content_matches_log'] = result(
        'pass' if not duplicate and actual_ids == expected_ids and not mismatches else 'fail',
        f'Field mismatches {mismatches}; expected {len(expected_ids)} events, got {len(ids)}')
    return checks


def audit_saved(eval_path, runs_path, output, only=None):
    cases = read(eval_path)
    index = {r['id']: r for r in read(runs_path)['results']}
    results = []
    for case in cases:
        if case.get('id') != only and only:
            continue
        ops = {c['op'] for c in case.get('checks', [])}
        if 'review_events_match' not in ops:
            continue
        try:
            run = index[case['id']]
            record = read(run['result_path'])
            state = record.get('database_state')
            checks = audit_review(case, state)
            results.append({'id': case['id'], 'checks': checks})
        except Exception as exc:
            results.append({'id': case['id'], 'checks': {op: result('unverified', str(exc))
                            for op in ('review_events_match','deadline','review_content_matches_log','review_no_invented_events')}})
    counts = Counter(check['status'] for item in results for check in item['checks'].values())
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'scenarios': len(results), 'statuses': dict(counts), 'results': results,
               'note': 'Independent audit of stored evidence, not a rewrite of baseline evaluation'}
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Review scenarios: {len(results)}')
    print('Check statuses:', dict(counts))
    for item in results:
        print(item['id'], ', '.join(f'{k}={v["status"]}' for k,v in item['checks'].items()))
    print('Saved:', output_path)
    return payload


def test_integration(eval_path):
    from sqlalchemy.schema import CreateSchema, DropSchema
    from database.database import make_engine, make_session_factory, session_scope
    from database.load_fixture import load_fixture
    from database.models import Base
    from tools.review import get_daily_activity, send_review
    from tools.external import MockMessageChannel
    cases = read(eval_path)
    case = next(c for c in cases if c['id']=='R-027')
    fixture = case['fixture']
    now = datetime.fromisoformat('2026-11-02T00:02:00+03:00')
    scenarios = [
        ('delivered_on_time', 'delivered', '2026-11-02T00:05:00+03:00', 'pass'),
        ('delivered_late', 'delivered', '2026-11-02T00:12:00+03:00', 'fail'),
        ('sent_unconfirmed', 'sent', None, 'unverified'),
        ('delivery_failed', 'failed', None, 'fail'),
        ('report_contains_invented_id', 'delivered', '2026-11-02T00:05:00+03:00', 'fail'),
    ]
    for name, status, delivered_at, expected in scenarios:
        engine = make_engine()
        schema = 'restoka_review_test_' + uuid4().hex
        created = False
        try:
            with engine.begin() as conn:
                conn.execute(CreateSchema(schema))
            created = True
            scoped = engine.execution_options(schema_translate_map={None:schema})
            Base.metadata.create_all(scoped)
            factory = make_session_factory(scoped)
            with session_scope(factory) as session:
                runtime = load_fixture(session, fixture)
            with session_scope(factory) as session:
                facts = get_daily_activity(session, runtime.organization_id, report_date=date(2026,11,1))
                assert facts.ok, facts.error
                digest = facts.data.facts_digest
            events = [dict(id=e['id'], type=e['type'], date=e['date']) for e in fixture['action_log']]
            if name == 'report_contains_invented_id':
                events.append({'id':'invented-review-event','type':'order_prepared','date':'2026-11-01'})
            body = json.dumps({'report_date':'2026-11-01','events':events},ensure_ascii=False)
            channel = MockMessageChannel(status=status, delivered_at=datetime.fromisoformat(delivered_at) if delivered_at else None)
            result_data = send_review(factory,runtime.organization_id,report_date=date(2026,11,1),
                                      text=body,operation_id='test-review-'+name,now=now,channel=channel,
                                      facts_digest=digest)
            if name == 'delivery_failed':
                assert not result_data.ok, ("Ожидалась ошибка доставки, но отправка прошла успешно")
                assert result_data.error.code.value == 'external_service_error'
            else:
                assert result_data.ok, result_data.error
        
            if status == 'delivered' and delivered_at:
                observation = datetime.fromisoformat(delivered_at)
                result_data = send_review(factory,runtime.organization_id,report_date=date(2026,11,1),
                                          text=body,operation_id='test-review-'+name,now=observation,channel=channel,
                                          facts_digest=digest)
                assert result_data.ok, result_data.error
            from harness.evidence import snapshot
            snapshot_state = snapshot(scoped)
            checks = audit_review(case,snapshot_state)
            got = checks['deadline']['status'] if name != 'report_contains_invented_id' else checks['review_no_invented_events']['status']
            assert got == expected, (name, expected, got, checks)
            if name == 'delivered_on_time':
                assert all(v['status']=='pass' for v in checks.values()), checks
            print('PASS',name, got, 'channel_attempts=',channel.send_attempts)
        finally:
            if created:
                with engine.begin() as conn:
                    conn.execute(DropSchema(schema,cascade=True))
            engine.dispose()
    print('RESULT: 5/5 passed')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--eval',default='eval_visible.json')
    parser.add_argument('--runs',default='harness_runs/baseline_visible_summary.json')
    parser.add_argument('--output',default='harness_runs/daily_review_audit.json')
    parser.add_argument('--only')
    parser.add_argument('--integration-test', action='store_true')
    args=parser.parse_args()
    if args.integration_test:
        test_integration(args.eval)
    else:
        audit_saved(args.eval,args.runs,args.output,args.only)

if __name__=='__main__':
    main()
