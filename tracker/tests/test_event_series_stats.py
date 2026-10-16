"""``series_stats``: сколько вхождений осталось и прошло относительно даты."""
from datetime import date, time

import pytest

from tracker.models import Event, EventCancellation, User
from tracker.tests.test_event_cancel_api import _series

URL = '/event_schedule/'


def _stats(client, event, day):
    r = client.get(f'{URL}{event.id}/series_stats/', {'date': day})
    assert r.status_code == 200, r.data
    return r.data


@pytest.mark.django_db
class TestSeriesStats:
    def test_end_count(self, auth_client, user, pet):
        event = _series(user, pet, end_count=10)  # 5..14 октября
        data = _stats(auth_client, event, '2026-10-08')
        assert data == {
            'remaining': 7, 'past': 3, 'series_end': '2026-10-14', 'is_open_ended': False, 'cancelled_dates': [],
        }

    def test_first_date_and_last_date(self, auth_client, user, pet):
        event = _series(user, pet, end_count=10)
        assert _stats(auth_client, event, '2026-10-05')['remaining'] == 10
        assert _stats(auth_client, event, '2026-10-05')['past'] == 0
        assert _stats(auth_client, event, '2026-10-14')['remaining'] == 1

    def test_date_after_end(self, auth_client, user, pet):
        event = _series(user, pet, end_count=3)
        data = _stats(auth_client, event, '2026-11-01')
        assert data['remaining'] == 0
        assert data['past'] == 3

    def test_open_ended(self, auth_client, user, pet):
        event = _series(user, pet)
        data = _stats(auth_client, event, '2026-10-08')
        assert data['remaining'] is None
        assert data['is_open_ended'] is True
        assert data['series_end'] is None
        assert data['past'] == 3

    def test_cancelled_not_counted(self, auth_client, user, pet):
        event = _series(user, pet, end_count=10)
        for day in (date(2026, 10, 6), date(2026, 10, 9), date(2026, 10, 10)):
            EventCancellation.objects.create(event=event, occurrence_date=day)
        data = _stats(auth_client, event, '2026-10-08')
        assert data['remaining'] == 5
        assert data['past'] == 2
        assert data['cancelled_dates'] == ['2026-10-09', '2026-10-10']

    def test_slots_count_separately(self, auth_client, user, pet):
        event = _series(user, pet, times=['08:00', '14:00'], end_count=4)  # 5..8 октября
        EventCancellation.objects.create(event=event, occurrence_date=date(2026, 10, 7), occurrence_time=time(14, 0))
        EventCancellation.objects.create(event=event, occurrence_date=date(2026, 10, 8), occurrence_time=None)
        data = _stats(auth_client, event, '2026-10-06')
        assert data['remaining'] == 2 * 3 - 2
        assert data['past'] == 2

    def test_single_event(self, auth_client, user, pet):
        event = Event.objects.create(user=user, pet=pet, title='Визит', start_date=date(2026, 10, 6))
        assert _stats(auth_client, event, '2026-10-06') == {
            'remaining': 1, 'past': 0, 'series_end': '2026-10-06', 'is_open_ended': False,
            'cancelled_dates': [],
        }
        EventCancellation.objects.create(event=event, occurrence_date=date(2026, 10, 6))
        assert _stats(auth_client, event, '2026-10-06')['remaining'] == 0

    def test_weekly_end_date(self, auth_client, user, pet):
        event = _series(user, pet, frequency='weekly')
        event.recurrence.week_days = [1]
        event.recurrence.end_date = event.recurrence.until = date(2026, 11, 2)
        event.recurrence.save()
        data = _stats(auth_client, event, '2026-10-12')
        assert data['remaining'] == 4  # 12, 19, 26 октября и 2 ноября
        assert data['past'] == 1

    def test_requires_date_and_owner(self, auth_client, user, pet):
        event = _series(user, pet)
        assert auth_client.get(f'{URL}{event.id}/series_stats/').status_code == 400
        other = User.objects.create_user(phone_number='79005554433')
        foreign = Event.objects.create(user=other, pet=pet, title='Чужое', start_date=date(2026, 10, 6))
        assert auth_client.get(f'{URL}{foreign.id}/series_stats/', {'date': '2026-10-06'}).status_code == 404

    def test_query_count_is_fixed(self, auth_client, user, pet, django_assert_max_num_queries):
        event = _series(user, pet, times=['08:00', '14:00'], end_count=300)
        with django_assert_max_num_queries(6):
            _stats(auth_client, event, '2026-10-20')
