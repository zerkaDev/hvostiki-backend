"""Прогон общих тест-векторов повторений (тот же файл использует мобильное приложение)."""
import json
from datetime import date
from pathlib import Path

import pytest

from tracker.recurrence import (
    Rule, RuleError, check_end_against_start, compute_until, first_occurrence, normalize_rule,
    occurrences, rule_from_normalized,
)

VECTORS = json.loads((Path(__file__).parent / 'data' / 'recurrence_vectors.json').read_text())['cases']


def _d(value):
    return date.fromisoformat(value) if value else None


@pytest.mark.parametrize('case', VECTORS, ids=[c['id'] for c in VECTORS])
def test_vector(case):
    start = _d(case['start_date'])
    raw = dict(case['rule'])
    raw['end_date'] = _d(raw.get('end_date'))
    raw.setdefault('end_count', None)
    expect = case['expect']

    try:
        rule = rule_from_normalized(normalize_rule(raw))
        check_end_against_start(rule, start)
    except RuleError as exc:
        assert 'error' in expect, f'неожиданная ошибка {exc.code}'
        assert exc.code == expect['error']['code']
        if 'first' in expect['error']:
            assert exc.extra['first'].isoformat() == expect['error']['first']
        return

    assert 'error' not in expect, f'ожидалась ошибка {expect["error"]}'
    until = compute_until(rule, start)
    assert (until.isoformat() if until else None) == expect['until']
    assert first_occurrence(rule, start).isoformat() == expect['first']
    w_from, w_to = _d(case['window'][0]), _d(case['window'][1])
    got = occurrences(rule, start, max(w_from, start), w_to, until)
    assert [d.isoformat() for d in got] == expect['dates']
