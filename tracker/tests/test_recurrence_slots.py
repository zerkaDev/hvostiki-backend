"""Несколько времён в день (поток C): запись, выдача по слотам, отметки, уведомления."""
import datetime
from datetime import date, time
from unittest.mock import patch

import pytest
from django.utils import timezone

from tracker.models import Event, EventCompletion, EventNotificationLog, FCMDevice
from tracker.tasks import send_event_notifications

URL = '/event_schedule/'
PERIOD = '/event_schedule/period/'


def _post(client, pet, times, time_='05:00', rule=None, **extra):
    recurrence = {'frequency': 'daily', 'times': times}
    recurrence.update(rule or {})
    payload = {
        'pet': pet.id, 'title': 'Лекарство', 'start_date': '2026-10-05', 'time': time_,
        'timezone_offset': 180, 'is_recurring': True, 'type': 'custom', 'recurrence': recurrence,
    }
    payload.update(extra)
    return client.post(URL, payload, format='json')


@pytest.mark.django_db
class TestWrite:
    def test_times_converted_sorted_and_deduped(self, auth_client, pet):
        # UTC 11:00, 05:00, 11:00 при offset +3 → локальные 14:00, 08:00
        r = _post(auth_client, pet, ['11:00', '05:00', '11:00'], time_='11:00')
        assert r.status_code == 201, r.data
        event = Event.objects.get()
        assert event.recurrence.times == ['08:00', '14:00']
        assert event.time == time(8, 0)  # первый слот
        assert r.data['recurrence']['times'] == ['05:00', '11:00']  # на проводе — UTC
        assert r.data['time'] == '05:00:00'

    def test_single_slot_collapses_to_event_time(self, auth_client, pet):
        r = _post(auth_client, pet, ['11:00'], time_='05:00')
        assert r.status_code == 201, r.data
        event = Event.objects.get()
        assert event.recurrence.times is None
        assert event.time == time(14, 0)

    def test_non_daily_clears_times(self, auth_client, pet):
        r = _post(auth_client, pet, ['05:00', '11:00'], rule={'frequency': 'weekly', 'week_days': [1]})
        assert r.status_code == 201, r.data
        assert Event.objects.get().recurrence.times is None

    @pytest.mark.parametrize('times,time_,code', [
        (['05:00', '06:00'], None, 'times_without_time'),
        (['01:00', '02:00', '03:00', '04:00', '05:00', '06:00', '07:00'], '01:00', 'invalid_times'),
    ])
    def test_errors(self, auth_client, pet, times, time_, code):
        r = _post(auth_client, pet, times, time_=time_)
        assert r.status_code == 400
        assert r.data['times'][0].code == code

    def test_patch_time_conflicting_with_times(self, auth_client, pet):
        eid = _post(auth_client, pet, ['05:00', '11:00']).data['id']
        r = auth_client.patch(f'{URL}{eid}/', {'time': '07:00'}, format='json')
        assert r.status_code == 400
        assert r.data['time'][0].code == 'time_conflicts_with_times'

    def test_patch_times_replaces_and_switching_frequency_clears(self, auth_client, pet):
        eid = _post(auth_client, pet, ['05:00', '11:00']).data['id']
        r = auth_client.patch(f'{URL}{eid}/', {'recurrence': {'times': ['06:00', '12:00', '18:00']}}, format='json')
        assert r.status_code == 200, r.data
        assert Event.objects.get().recurrence.times == ['09:00', '15:00', '21:00']
        r = auth_client.patch(f'{URL}{eid}/', {'recurrence': {'frequency': 'weekly', 'week_days': [2]}}, format='json')
        assert r.status_code == 200, r.data
        assert Event.objects.get().recurrence.times is None


@pytest.mark.django_db
class TestPeriod:
    def test_one_record_per_slot_sorted_with_done_by_slot(self, auth_client, pet):
        eid = _post(auth_client, pet, ['05:00', '11:00', '17:00']).data['id']
        auth_client.post(f'{URL}{eid}/mark_done/', {'date': '2026-10-05', 'time': '11:00'}, format='json')
        r = auth_client.get(PERIOD, {'date_from': '2026-10-05', 'date_to': '2026-10-06'})
        day = r.data['2026-10-05']
        assert [e['time'] for e in day] == ['05:00:00', '11:00:00', '17:00:00']
        assert [e['done'] for e in day] == [False, True, False]
        assert all(e['id'] == eid for e in day)
        assert all(e['done'] is False for e in r.data['2026-10-06'])
        assert day[0]['recurrence']['times'] == ['05:00', '11:00', '17:00']

    def test_occurrence_limit_counts_slots(self, auth_client, pet, monkeypatch):
        from tracker import views
        monkeypatch.setattr(views, 'MAX_OCCURRENCES_PER_RESPONSE', 5)
        _post(auth_client, pet, ['05:00', '11:00', '17:00'])
        r = auth_client.get(PERIOD, {'date_from': '2026-10-05', 'date_to': '2026-10-06'})
        assert r.status_code == 400

    def test_midnight_crossing_sorted_by_local_moment(self, auth_client, pet):
        # локально 00:30 и 23:30 (offset +3) → UTC 21:30 и 20:30; порядок в дне — по локальному времени
        _post(auth_client, pet, ['21:30', '20:30'], time_='21:30')
        r = auth_client.get(PERIOD, {'date_from': '2026-10-05', 'date_to': '2026-10-05'})
        assert [e['time'] for e in r.data['2026-10-05']] == ['21:30:00', '20:30:00']

    def test_queries_do_not_grow_with_slots(self, auth_client, pet, django_assert_max_num_queries):
        _post(auth_client, pet, ['05:00', '11:00', '17:00'])
        with django_assert_max_num_queries(8):
            r = auth_client.get(PERIOD, {'date_from': '2026-10-05', 'date_to': '2027-02-05'})
        assert r.status_code == 200 and sum(len(v) for v in r.data.values()) == 124 * 3


@pytest.mark.django_db
class TestMarkDone:
    def _event(self, client, pet):
        return _post(client, pet, ['05:00', '11:00']).data['id']

    def test_time_required_for_multi_slot(self, auth_client, pet):
        eid = self._event(auth_client, pet)
        r = auth_client.post(f'{URL}{eid}/mark_done/', {'date': '2026-10-05'}, format='json')
        assert r.status_code == 400 and 'detail' in r.data

    def test_unknown_slot(self, auth_client, pet):
        eid = self._event(auth_client, pet)
        r = auth_client.post(f'{URL}{eid}/mark_done/', {'date': '2026-10-05', 'time': '07:00'}, format='json')
        assert r.status_code == 400

    def test_done_and_undone_one_slot_keeps_other(self, auth_client, pet):
        eid = self._event(auth_client, pet)
        for t in ('05:00', '11:00'):
            assert auth_client.post(f'{URL}{eid}/mark_done/', {'date': '2026-10-05', 'time': t}, format='json').status_code == 200
        auth_client.post(f'{URL}{eid}/mark_undone/', {'date': '2026-10-05', 'time': '05:00'}, format='json')
        assert list(EventCompletion.objects.values_list('occurrence_time', flat=True)) == [time(14, 0)]

    def test_idempotent(self, auth_client, pet):
        eid = self._event(auth_client, pet)
        for _ in range(2):
            auth_client.post(f'{URL}{eid}/mark_done/', {'date': '2026-10-05', 'time': '05:00'}, format='json')
        assert EventCompletion.objects.count() == 1

    def test_legacy_completion_counts_for_first_slot(self, auth_client, pet):
        eid = self._event(auth_client, pet)
        EventCompletion.objects.create(event_id=eid, occurrence_date=date(2026, 10, 5))  # старая отметка, NULL
        r = auth_client.get(PERIOD, {'date_from': '2026-10-05', 'date_to': '2026-10-05'})
        assert [e['done'] for e in r.data['2026-10-05']] == [True, False]

    def test_single_slot_unchanged(self, auth_client, pet, user):
        eid = _post(auth_client, pet, None).data['id']
        r = auth_client.post(f'{URL}{eid}/mark_done/', {'date': '2026-10-05'}, format='json')
        assert r.status_code == 200
        assert EventCompletion.objects.get().occurrence_time is None


@pytest.mark.django_db
class TestNotifications:
    def _setup(self, user, pet):
        FCMDevice.objects.create(user=user, fcm_token='t')
        return Event.objects.create(
            user=user, pet=pet, title='Таблетки', start_date=date(2026, 10, 5), time=time(8, 0),
            timezone_offset=0, is_recurring=True,
            recurrence=__import__('tracker.models', fromlist=['RecurrenceRule']).RecurrenceRule.objects.create(
                frequency='daily', times=['08:00', '14:00', '20:00'],
            ),
        )

    def _run(self, hour, minute=0):
        now = timezone.make_aware(datetime.datetime(2026, 10, 6, hour, minute))
        with patch('django.utils.timezone.now', return_value=now), \
                patch('tracker.tasks.firebase_service.send_push_notification') as send:
            send_event_notifications()
        return send

    def test_each_slot_notifies_with_its_time(self, user, pet):
        event = self._setup(user, pet)
        send = self._run(14)
        assert send.call_count == 1
        assert send.call_args.kwargs['data']['time'] == '14:00'
        log = EventNotificationLog.objects.get()
        assert log.event == event and log.occurrence_time == time(14, 0)

    def test_second_run_does_not_duplicate(self, user, pet):
        self._setup(user, pet)
        self._run(14)
        assert self._run(14, 1).call_count == 0

    def test_done_slot_does_not_notify_but_others_do(self, user, pet):
        event = self._setup(user, pet)
        EventCompletion.objects.create(event=event, occurrence_date=date(2026, 10, 6), occurrence_time=time(8, 0))
        assert self._run(8).call_count == 0
        assert self._run(20).call_count == 1

    def test_legacy_completion_silences_first_slot_only(self, user, pet):
        event = self._setup(user, pet)
        EventCompletion.objects.create(event=event, occurrence_date=date(2026, 10, 6))
        assert self._run(8).call_count == 0
        assert self._run(14).call_count == 1
