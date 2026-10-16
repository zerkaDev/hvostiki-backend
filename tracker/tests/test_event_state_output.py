"""Выдача состояния вхождения: отмена, ``done_at``, правка вхождения; ``upcoming`` и пуши."""
import datetime
from datetime import date, time
from unittest.mock import patch

import pytest
from django.utils import timezone

from tracker.models import (
    Event, EventCancellation, EventCompletion, EventNotificationLog, EventOccurrenceOverride, FCMDevice,
    RecurrenceRule,
)
from tracker.tasks import send_event_notifications

PERIOD = '/event_schedule/period/'
DAY = date(2026, 10, 6)


def _series(user, pet, *, times=None, event_time=time(8, 0), offset=0):
    """Ежедневная серия с 5 октября; ``times`` — локальные слоты (несколько времён в день)."""
    return Event.objects.create(
        user=user, pet=pet, title='Таблетки', description='Одна капсула', start_date=date(2026, 10, 5),
        time=event_time, timezone_offset=offset, is_recurring=True, type='dailyPills',
        recurrence=RecurrenceRule.objects.create(frequency='daily', times=times),
    )


def _period(client, day_from=DAY, day_to=DAY, **params):
    r = client.get(PERIOD, {'date_from': day_from.isoformat(), 'date_to': day_to.isoformat(), **params})
    assert r.status_code == 200, r.data
    return r.data


@pytest.mark.django_db
class TestPeriodState:
    def test_new_fields_default(self, auth_client, user, pet):
        _series(user, pet)
        item = _period(auth_client)['2026-10-06'][0]
        assert item['cancelled'] is False
        assert item['done'] is False
        assert item['done_at'] is None

    def test_cancelled_occurrence_stays_in_list(self, auth_client, user, pet):
        event = _series(user, pet)
        EventCancellation.objects.create(event=event, occurrence_date=DAY)

        data = _period(auth_client, date(2026, 10, 5), date(2026, 10, 7))

        assert [item['cancelled'] for d in sorted(data) for item in data[d]] == [False, True, False]

    def test_single_event_cancellation(self, auth_client, user, pet):
        event = Event.objects.create(user=user, pet=pet, title='Визит', start_date=DAY, time=time(16, 0))
        EventCancellation.objects.create(event=event, occurrence_date=DAY)

        assert _period(auth_client)['2026-10-06'][0]['cancelled'] is True

    def test_done_at_is_returned_for_completed_occurrence(self, auth_client, user, pet):
        event = _series(user, pet)
        EventCompletion.objects.create(event=event, occurrence_date=DAY)

        item = _period(auth_client)['2026-10-06'][0]

        assert item['done'] is True
        assert item['done_at'] is not None and item['done_at'].endswith('Z')

    def test_cancellation_of_one_slot_only(self, auth_client, user, pet):
        event = _series(user, pet, times=['08:00', '14:00'])
        EventCancellation.objects.create(event=event, occurrence_date=DAY, occurrence_time=time(14, 0))

        items = _period(auth_client)['2026-10-06']

        assert [(i['time'], i['cancelled']) for i in items] == [('08:00:00', False), ('14:00:00', True)]

    def test_legacy_null_slot_cancellation_applies_to_first_slot(self, auth_client, user, pet):
        event = _series(user, pet, times=['08:00', '14:00'])
        EventCancellation.objects.create(event=event, occurrence_date=DAY)

        items = _period(auth_client)['2026-10-06']

        assert [i['cancelled'] for i in items] == [True, False]

    def test_queries_do_not_grow_with_occurrences(self, auth_client, user, pet, django_assert_max_num_queries):
        event = _series(user, pet, times=['08:00', '14:00'])
        for offset in range(10):
            EventCancellation.objects.create(
                event=event, occurrence_date=date(2026, 10, 5) + datetime.timedelta(days=offset),
                occurrence_time=time(8, 0),
            )

        with django_assert_max_num_queries(12):
            _period(auth_client, date(2026, 10, 5), date(2026, 10, 25))


@pytest.mark.django_db
class TestOverrides:
    def test_override_replaces_fields_for_that_date_only(self, auth_client, user, pet):
        event = _series(user, pet)
        EventOccurrenceOverride.objects.create(
            event=event, occurrence_date=DAY, title='Витамины', description='Двойная доза', type='vetVisit',
            time=time(8, 30),
        )

        data = _period(auth_client, date(2026, 10, 5), date(2026, 10, 7))

        changed = data['2026-10-06'][0]
        assert (changed['title'], changed['description'], changed['type'], changed['time']) == (
            'Витамины', 'Двойная доза', 'vetVisit', '08:30:00',
        )
        for other in ('2026-10-05', '2026-10-07'):
            item = data[other][0]
            assert (item['title'], item['description'], item['type'], item['time']) == (
                'Таблетки', 'Одна капсула', 'dailyPills', '08:00:00',
            )

    def test_partial_override_keeps_other_fields(self, auth_client, user, pet):
        event = _series(user, pet)
        EventOccurrenceOverride.objects.create(event=event, occurrence_date=DAY, title='Только название')

        item = _period(auth_client)['2026-10-06'][0]

        assert (item['title'], item['description'], item['time']) == ('Только название', 'Одна капсула', '08:00:00')

    def test_override_time_is_converted_to_utc(self, auth_client, user, pet):
        event = _series(user, pet, offset=180, event_time=time(11, 0))
        EventOccurrenceOverride.objects.create(event=event, occurrence_date=DAY, time=time(12, 30))

        assert _period(auth_client)['2026-10-06'][0]['time'] == '09:30:00'

    def test_override_time_changes_order_inside_day(self, auth_client, user, pet):
        first = _series(user, pet, event_time=time(8, 0))
        Event.objects.create(user=user, pet=pet, title='Прогулка', start_date=DAY, time=time(9, 0))
        EventOccurrenceOverride.objects.create(event=first, occurrence_date=DAY, time=time(10, 0))

        titles = [i['title'] for i in _period(auth_client)['2026-10-06']]

        assert titles == ['Прогулка', 'Таблетки']

    def test_override_of_one_slot_is_matched_by_original_slot(self, auth_client, user, pet):
        event = _series(user, pet, times=['08:00', '14:00'])
        EventOccurrenceOverride.objects.create(
            event=event, occurrence_date=DAY, occurrence_time=time(14, 0), time=time(15, 0), title='Позже',
        )
        EventCompletion.objects.create(event=event, occurrence_date=DAY, occurrence_time=time(14, 0))

        items = _period(auth_client)['2026-10-06']

        assert [(i['title'], i['time'], i['done']) for i in items] == [
            ('Таблетки', '08:00:00', False), ('Позже', '15:00:00', True),
        ]

    def test_override_is_not_applied_to_event_detail(self, auth_client, user, pet):
        event = _series(user, pet)
        EventOccurrenceOverride.objects.create(event=event, occurrence_date=DAY, title='Только дата')

        assert auth_client.get(f'/event_schedule/{event.id}/').data['title'] == 'Таблетки'


@pytest.mark.django_db
class TestUpcoming:
    def _upcoming(self, client, pet):
        r = client.get(f'/pets/{pet.id}/upcoming/', {'date_from': '2026-10-05', 'days': 3})
        assert r.status_code == 200, r.data
        return r.data

    def test_cancelled_occurrences_are_hidden(self, auth_client, user, pet):
        event = _series(user, pet)
        EventCancellation.objects.create(event=event, occurrence_date=DAY)

        assert sorted(self._upcoming(auth_client, pet)) == ['2026-10-05', '2026-10-07', '2026-10-08']

    def test_override_is_applied(self, auth_client, user, pet):
        event = _series(user, pet)
        EventOccurrenceOverride.objects.create(event=event, occurrence_date=DAY, title='Другое название')

        assert self._upcoming(auth_client, pet)['2026-10-06'][0]['title'] == 'Другое название'


@pytest.mark.django_db
class TestNotifications:
    def _run(self, hour, minute=0, day=DAY):
        now = timezone.make_aware(datetime.datetime(day.year, day.month, day.day, hour, minute))
        with patch('django.utils.timezone.now', return_value=now), \
                patch('tracker.tasks.firebase_service.send_push_notification') as send:
            send_event_notifications()
        return send

    def test_cancelled_occurrence_does_not_notify(self, user, pet):
        FCMDevice.objects.create(user=user, fcm_token='t')
        event = _series(user, pet)
        EventCancellation.objects.create(event=event, occurrence_date=DAY)

        assert self._run(8).call_count == 0
        assert not EventNotificationLog.objects.exists()

    def test_other_days_still_notify(self, user, pet):
        FCMDevice.objects.create(user=user, fcm_token='t')
        event = _series(user, pet)
        EventCancellation.objects.create(event=event, occurrence_date=DAY)

        assert self._run(8, day=date(2026, 10, 7)).call_count == 1

    def test_cancelled_slot_does_not_silence_other_slots(self, user, pet):
        FCMDevice.objects.create(user=user, fcm_token='t')
        event = _series(user, pet, times=['08:00', '14:00'])
        EventCancellation.objects.create(event=event, occurrence_date=DAY, occurrence_time=time(8, 0))

        assert self._run(8).call_count == 0
        assert self._run(14).call_count == 1

    def test_override_moves_notification_time_and_text(self, user, pet):
        FCMDevice.objects.create(user=user, fcm_token='t')
        event = _series(user, pet)
        EventOccurrenceOverride.objects.create(
            event=event, occurrence_date=DAY, time=time(9, 30), title='Позже', type='vetVisit',
        )

        assert self._run(8).call_count == 0
        send = self._run(9, 30)

        assert send.call_count == 1
        kwargs = send.call_args.kwargs
        assert kwargs['title'] == 'Позже'
        assert kwargs['data']['event_type'] == 'vetVisit'

    def test_override_applies_only_to_its_date(self, user, pet):
        FCMDevice.objects.create(user=user, fcm_token='t')
        event = _series(user, pet)
        EventOccurrenceOverride.objects.create(event=event, occurrence_date=DAY, time=time(9, 30), title='Позже')

        send = self._run(8, day=date(2026, 10, 7))

        assert send.call_count == 1
        assert send.call_args.kwargs['title'] == 'Таблетки'
