"""Создание/правка повторяющихся событий через API: валидация, until, end_type."""
import datetime

import pytest
from django.urls import reverse

from tracker.models import Event, RecurrenceRule

URL = '/event_schedule/'
HEADERS = {'HTTP_X_TIME_CONTRACT': 'utc'}


def _payload(pet, recurrence, start='2026-10-05', **extra):
    data = {
        'pet': pet.id, 'title': 'Лекарство', 'start_date': start, 'timezone_offset': 180,
        'is_recurring': True, 'type': 'custom', 'recurrence': recurrence,
    }
    data.update(extra)
    return data


def _post(client, pet, recurrence, **kw):
    return client.post(URL, _payload(pet, recurrence, **kw), format='json', **HEADERS)


@pytest.mark.django_db
class TestCreate:
    def test_yearly(self, auth_client, pet):
        r = _post(auth_client, pet, {'frequency': 'yearly', 'year_dates': [{'month': 2, 'day': 29}, {'month': 3, 'day': 1}]})
        assert r.status_code == 201, r.data
        assert r.data['recurrence']['end_type'] == 'never'
        rule = RecurrenceRule.objects.get()
        assert rule.until is None

    def test_end_count_stores_until_and_end_type(self, auth_client, pet):
        r = _post(auth_client, pet, {'frequency': 'weekly', 'week_days': [1, 3], 'end_type': 'count', 'end_count': 3})
        assert r.status_code == 201, r.data
        assert r.data['recurrence']['end_type'] == 'count'
        rule = RecurrenceRule.objects.get()
        # 5.10 пн, 7.10 ср, 12.10 пн
        assert rule.until == datetime.date(2026, 10, 12)

    def test_end_date_sets_until(self, auth_client, pet):
        r = _post(auth_client, pet, {'frequency': 'daily', 'end_date': '2026-10-20'})
        assert r.status_code == 201
        assert r.data['recurrence']['end_type'] == 'date'
        assert RecurrenceRule.objects.get().until == datetime.date(2026, 10, 20)

    def test_foreign_fields_cleared(self, auth_client, pet):
        r = _post(auth_client, pet, {'frequency': 'daily', 'week_days': [1], 'month_days': [3]})
        assert r.status_code == 201
        rule = RecurrenceRule.objects.get()
        assert rule.week_days is None and rule.month_days is None

    @pytest.mark.parametrize('rule,field', [
        ({'frequency': 'weekly'}, 'week_days'),
        ({'frequency': 'monthly', 'month_days': [0]}, 'month_days'),
        ({'frequency': 'yearly', 'year_dates': [{'month': 2, 'day': 30}]}, 'year_dates'),
        ({'frequency': 'daily', 'interval': 31}, 'interval'),
        ({'frequency': 'yearly', 'interval': 11, 'year_dates': [{'month': 1, 'day': 1}]}, 'interval'),
        ({'frequency': 'daily', 'end_count': 1}, 'end_count'),
        ({'frequency': 'daily', 'end_count': 1000}, 'end_count'),
        ({'frequency': 'daily', 'end_date': '2026-10-10', 'end_count': 5}, 'end_date'),
        ({'frequency': 'daily', 'end_type': 'date'}, 'end_date'),
        ({'frequency': 'daily', 'end_type': 'count'}, 'end_count'),
    ])
    def test_invalid(self, auth_client, pet, rule, field):
        r = _post(auth_client, pet, rule)
        assert r.status_code == 400
        assert field in r.data

    def test_end_before_start(self, auth_client, pet):
        r = _post(auth_client, pet, {'frequency': 'daily', 'end_date': '2026-10-01'})
        assert r.status_code == 400
        assert r.data['end_date'][0].code == 'end_before_start'

    def test_end_before_first(self, auth_client, pet):
        # старт во вторник, повторение по средам; окончание в тот же вторник — раньше первого повторения
        r = _post(auth_client, pet, {'frequency': 'weekly', 'week_days': [3], 'end_date': '2026-10-06'}, start='2026-10-06')
        assert r.status_code == 400
        assert r.data['end_date'][0].code == 'end_before_first'
        assert '07.10.2026' in str(r.data['end_date'][0])


@pytest.mark.django_db
class TestUpdate:
    def _create(self, client, pet, rule):
        r = _post(client, pet, rule)
        assert r.status_code == 201, r.data
        return r.data['id']

    def test_patch_switch_frequency_clears_old_fields(self, auth_client, pet):
        eid = self._create(auth_client, pet, {'frequency': 'weekly', 'week_days': [1]})
        r = auth_client.patch(f'/event_schedule/{eid}/', {
            'recurrence': {'frequency': 'monthly', 'month_days': [-1]},
        }, format='json', **HEADERS)
        assert r.status_code == 200, r.data
        rule = Event.objects.get(pk=eid).recurrence
        assert rule.week_days is None and rule.month_days == [-1]

    def test_patch_end_count_replaces_end_date(self, auth_client, pet):
        eid = self._create(auth_client, pet, {'frequency': 'daily', 'end_date': '2026-10-20'})
        r = auth_client.patch(f'/event_schedule/{eid}/', {'recurrence': {'end_count': 4}}, format='json', **HEADERS)
        assert r.status_code == 200, r.data
        rule = Event.objects.get(pk=eid).recurrence
        assert rule.end_date is None and rule.until == datetime.date(2026, 10, 8)

    def test_patch_start_date_recomputes_until(self, auth_client, pet):
        eid = self._create(auth_client, pet, {'frequency': 'daily', 'end_count': 3})
        r = auth_client.patch(f'/event_schedule/{eid}/', {'start_date': '2026-11-01'}, format='json', **HEADERS)
        assert r.status_code == 200, r.data
        assert Event.objects.get(pk=eid).recurrence.until == datetime.date(2026, 11, 3)

    def test_patch_start_date_after_end_date_rejected(self, auth_client, pet):
        eid = self._create(auth_client, pet, {'frequency': 'daily', 'end_date': '2026-10-20'})
        r = auth_client.patch(f'/event_schedule/{eid}/', {'start_date': '2026-11-01'}, format='json', **HEADERS)
        assert r.status_code == 400
        assert Event.objects.get(pk=eid).start_date == datetime.date(2026, 10, 5)

    def test_patch_clear_end(self, auth_client, pet):
        eid = self._create(auth_client, pet, {'frequency': 'daily', 'end_count': 3})
        r = auth_client.patch(f'/event_schedule/{eid}/', {'recurrence': {'end_type': 'never'}}, format='json', **HEADERS)
        assert r.status_code == 200, r.data
        rule = Event.objects.get(pk=eid).recurrence
        assert rule.end_count is None and rule.until is None


@pytest.mark.django_db
class TestPeriodLimits:
    PERIOD = '/event_schedule/period/'

    def test_date_from_after_date_to(self, auth_client):
        r = auth_client.get(self.PERIOD, {'date_from': '2026-10-10', 'date_to': '2026-10-01'}, **HEADERS)
        assert r.status_code == 400

    def test_window_too_long(self, auth_client):
        r = auth_client.get(self.PERIOD, {'date_from': '2026-01-01', 'date_to': '2027-03-01'}, **HEADERS)
        assert r.status_code == 400

    def test_window_400_days_ok(self, auth_client):
        r = auth_client.get(self.PERIOD, {'date_from': '2026-01-01', 'date_to': '2027-02-05'}, **HEADERS)
        assert r.status_code == 200

    def test_occurrence_limit(self, auth_client, pet, monkeypatch):
        from tracker import views
        monkeypatch.setattr(views, 'MAX_OCCURRENCES_PER_RESPONSE', 5)
        assert _post(auth_client, pet, {'frequency': 'daily'}).status_code == 201
        r = auth_client.get(self.PERIOD, {'date_from': '2026-10-05', 'date_to': '2026-10-20'}, **HEADERS)
        assert r.status_code == 400 and 'detail' in r.data

    def test_queries_do_not_grow_with_occurrences(self, auth_client, pet, django_assert_max_num_queries):
        assert _post(auth_client, pet, {'frequency': 'daily'}).status_code == 201
        with django_assert_max_num_queries(8):
            r = auth_client.get(self.PERIOD, {'date_from': '2026-10-05', 'date_to': '2027-02-05'}, **HEADERS)
        assert r.status_code == 200 and len(r.data) == 124

    def test_done_flag_from_prefetch(self, auth_client, pet):
        eid = _post(auth_client, pet, {'frequency': 'daily'}).data['id']
        auth_client.post(f'/event_schedule/{eid}/mark_done/', {'date': '2026-10-06'}, format='json', **HEADERS)
        r = auth_client.get(self.PERIOD, {'date_from': '2026-10-05', 'date_to': '2026-10-07'}, **HEADERS)
        assert [r.data[d][0]['done'] for d in sorted(r.data)] == [False, True, False]

    def test_expired_rule_excluded(self, auth_client, pet):
        _post(auth_client, pet, {'frequency': 'daily', 'end_date': '2026-10-08'})
        r = auth_client.get(self.PERIOD, {'date_from': '2026-10-09', 'date_to': '2026-10-20'}, **HEADERS)
        assert r.status_code == 200 and r.data == {}
