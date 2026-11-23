"""Score structured facts extracted from explicitly annotated supplier messages."""

from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation


FIELDS = frozenset({
    'material', 'quantity', 'unit', 'unit_price_rub', 'currency', 'delivery_fee_rub',
    'delivery_date', 'delivery_confirmed', 'remaining_confirmed', 'available',
})
NUMBER_FIELDS = {'quantity', 'unit_price_rub', 'delivery_fee_rub'}
FLAG_FIELDS = {'delivery_confirmed', 'remaining_confirmed', 'available'}
ALIASES = {
    'unit': {'шт': 'pcs', 'штук': 'pcs', 'штука': 'pcs', 'штуки': 'pcs', 'ед': 'pcs',
             'единица': 'pcs', 'единицу': 'pcs', 'unit': 'pcs', 'units': 'pcs',
             'piece': 'pcs', 'pieces': 'pcs', 'pc': 'pcs', 'pcs': 'pcs', 'кг': 'kg'},
    'currency': {'руб': 'rub', 'рубль': 'rub', 'рублей': 'rub', '₽': 'rub', 'rur': 'rub'},
}


def normalize(field, value):
    if field in NUMBER_FIELDS and isinstance(value, (str, int, float)) and type(value) is not bool:
        try:
            number = Decimal(''.join(str(value).split()).replace(',', '.'))
            return ('number', number) if number.is_finite() else ('invalid',)
        except InvalidOperation:
            return ('invalid',)
    if field in FLAG_FIELDS:
        return ('bool', value) if type(value) is bool else ('invalid',)
    if not isinstance(value, str):
        return ('invalid',)
    if field == 'delivery_date':
        try:
            return ('date', date.fromisoformat(value.strip()))
        except ValueError:
            return ('invalid',)
    text = ' '.join(value.casefold().split())
    if field in ALIASES:
        text = ALIASES[field].get(text.rstrip('.'), text.rstrip('.'))
    return ('text', text) if text else ('invalid',)


def metrics(counts):
    tp, fp, fn = (counts[key] for key in ('tp', 'fp', 'fn'))
    return {'tp': tp, 'fp': fp, 'fn': fn,
            'gold_count': tp + fn, 'predicted_count': tp + fp,
            'precision': tp / (tp + fp) if tp + fp else None,
            'recall': tp / (tp + fn) if tp + fn else None}


def supplier_scores(case, step_facts):
    annotations = case.get('supplier_evaluation', [])
    if not annotations:
        return None
    by_field = {field: Counter() for field in sorted(FIELDS)}
    rows = []
    for annotation in annotations:
        step = annotation['step']
        gold = annotation['facts']
        expected = {field: normalize(field, value) for field, value in gold.items()}
        if not expected.keys() <= FIELDS or ('invalid',) in expected.values():
            raise ValueError(f'Invalid supplier gold for {case["id"]} step {step}')
        facts = step_facts[step - 1] if 1 <= step <= len(step_facts) else {}
        observed = {field: facts[field] for field in FIELDS if facts.get(field) is not None}
        predicted = {field: normalize(field, value) for field, value in observed.items()}
        matched = {field for field in expected if predicted.get(field) == expected[field]}
        for field in expected.keys() | predicted.keys():
            by_field[field].update(tp=int(field in matched),
                                   fp=int(field in predicted and field not in matched),
                                   fn=int(field in expected and field not in matched))
        rows.append({'step': step, 'matched_fields': sorted(matched),
                     'incorrect_fields': sorted((expected.keys() & predicted.keys()) - matched),
                     'missing_fields': sorted(expected.keys() - predicted.keys()),
                     'unexpected_fields': sorted(predicted.keys() - expected.keys())})
    return {**metrics(sum(by_field.values(), Counter())), 'turns': rows,
            'by_field': {field: metrics(counts) for field, counts in by_field.items()}}


def summarize_supplier_scores(results):
    scores = [row['supplier_extraction'] for row in results if row.get('supplier_extraction') is not None]
    totals = Counter()
    by_field = {field: Counter() for field in sorted(FIELDS)}
    for score in scores:
        totals.update({key: score[key] for key in ('tp', 'fp', 'fn')})
        for field, counts in score['by_field'].items():
            by_field[field].update({key: counts[key] for key in ('tp', 'fp', 'fn')})
    return {**metrics(totals), 'scenarios': len(scores),
            'turns': sum(len(score['turns']) for score in scores),
            'by_field': {field: metrics(counts) for field, counts in by_field.items()}}
