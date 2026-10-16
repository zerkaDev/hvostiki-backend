"""Отмена вхождений: ``cancel`` (one/future), ``restore``, конфликты с отметкой выполнения."""
from datetime import date, time

import pytest

from tracker.models import (
    Event, EventCancellation, EventCompletion, EventOccurrenceOverride, RecurrenceRule, User,
)

URL = '/event_schedule/'
D = date(2026, 10, 6)


def _series(user, pet, *, times=None, end_count=None, frequency='daily', offset=0):
    from tracker.recurrence import Rule, compute_until
    rule = RecurrenceRule.objects.create(frequency=frequency, times=times, end_count=end_count)
    start = date(2026, 10, 5)
    rule.until = compute_until(Rule.from_model(rule), start)
    rule.save()
    return Event.objects.create(
        user=user, pet=pet, title='Таблетки', start_date=start, time=time(8, 0),
        timezone_offset=offset, is_recurring=True, type='dailyPills', recurrence=rule,
    )


def _post(client, event, action, **body):
    return client.post(f'{URL}{event.id}/{action}/', body, format='json')


@pytest.mark.django_db
class TestCancelOne:
    def test_cancel_single_event(self, auth_client, user, pet):
        event = Event.objects.create(user=user, pet=pet, title='Визит', start_date=D, time=time(16, 0))
        r = _post(auth_client, event, 'cancel', date='2026-10-06')
        assert r.status_code == 200, r.data
        assert r.data['cancelled'] is True
        assert r.data['start_date'] == '2026-10-06'
        assert EventCancellation.objects.filter(event=event).count() == 1

    def test_idempotent(self, auth_client, user, pet):
        event = _series(user, pet)
        assert _post(auth_client, event, 'cancel', date='2026-10-06').status_code == 200
        assert _post(auth_client, event, 'cancel', date='2026-10-06').status_code == 200
        assert EventCancellation.objects.filter(event=event).count() == 1

    def test_series_keeps_other_days(self, auth_client, user, pet):
        event = _series(user, pet)
        _post(auth_client, event, 'cancel', date='2026-10-06')
        data = auth_client.get('/event_schedule/period/', {'date_from': '2026-10-05', 'date_to': '2026-10-07'}).data
        assert [i['cancelled'] for d in sorted(data) for i in data[d]] == [False, True, False]

    def test_not_an_occurrence_is_400(self, auth_client, user, pet):
        event = _series(user, pet, frequency='weekly')  # 5 октября 2026 — понедельник
        assert _post(auth_client, event, 'cancel', date='2026-10-06').status_code == 400

    def test_bad_scope_and_missing_date(self, auth_client, user, pet):
        event = _series(user, pet)
        assert _post(auth_client, event, 'cancel', date='2026-10-06', scope='all').status_code == 400
        assert _post(auth_client, event, 'cancel').status_code == 400

    def test_done_occurrence_conflicts(self, auth_client, user, pet):
        event = _series(user, pet)
        EventCompletion.objects.create(event=event, occurrence_date=D)
        assert _post(auth_client, event, 'cancel', date='2026-10-06').status_code == 409
        assert not EventCancellation.objects.exists()

    def test_foreign_event_is_404(self, auth_client, pet):
        other = User.objects.create_user(phone_number='79005554433')
        event = Event.objects.create(user=other, pet=pet, title='Чужое', start_date=D)
        assert _post(auth_client, event, 'cancel', date='2026-10-06').status_code == 404
        assert _post(auth_client, event, 'restore', date='2026-10-06').status_code == 404


@pytest.mark.django_db
class TestMarkDoneOnCancelled:
    def test_mark_done_conflicts(self, auth_client, user, pet):
        event = _series(user, pet)
        _post(auth_client, event, 'cancel', date='2026-10-06')
        assert _post(auth_client, event, 'mark_done', date='2026-10-06').status_code == 409
        assert not EventCompletion.objects.exists()

    def test_after_restore_mark_done_works(self, auth_client, user, pet):
        event = _series(user, pet)
        _post(auth_client, event, 'cancel', date='2026-10-06')
        _post(auth_client, event, 'restore', date='2026-10-06')
        assert _post(auth_client, event, 'mark_done', date='2026-10-06').status_code == 200

    def test_other_day_not_affected(self, auth_client, user, pet):
        event = _series(user, pet)
        _post(auth_client, event, 'cancel', date='2026-10-06')
        assert _post(auth_client, event, 'mark_done', date='2026-10-07').status_code == 200


@pytest.mark.django_db
class TestRestore:
    def test_restore(self, auth_client, user, pet):
        event = _series(user, pet)
        _post(auth_client, event, 'cancel', date='2026-10-06')
        r = _post(auth_client, event, 'restore', date='2026-10-06')
        assert r.status_code == 200
        assert r.data['cancelled'] is False
        assert not EventCancellation.objects.exists()

    def test_restore_not_cancelled_is_ok(self, auth_client, user, pet):
        event = _series(user, pet)
        assert _post(auth_client, event, 'restore', date='2026-10-06').status_code == 200

    def test_restore_after_future_does_not_extend_series(self, auth_client, user, pet):
        event = _series(user, pet)
        _post(auth_client, event, 'cancel', date='2026-10-06', scope='future')
        _post(auth_client, event, 'restore', date='2026-10-06')
        event.recurrence.refresh_from_db()
        assert event.recurrence.end_date == D
        data = auth_client.get('/event_schedule/period/', {'date_from': '2026-10-05', 'date_to': '2026-10-09'}).data
        assert sorted(data) == ['2026-10-05', '2026-10-06']
        assert data['2026-10-06'][0]['cancelled'] is False


@pytest.mark.django_db
class TestCancelFuture:
    def test_stops_series(self, auth_client, user, pet):
        event = _series(user, pet)
        r = _post(auth_client, event, 'cancel', date='2026-10-07', scope='future')
        assert r.status_code == 200, r.data
        assert r.data['cancelled'] is True
        event.recurrence.refresh_from_db()
        assert event.recurrence.end_date == date(2026, 10, 7)
        assert event.recurrence.until == date(2026, 10, 7)
        assert event.recurrence.end_count is None
        data = auth_client.get('/event_schedule/period/', {'date_from': '2026-10-05', 'date_to': '2026-10-12'}).data
        assert [(d, data[d][0]['cancelled']) for d in sorted(data)] == [
            ('2026-10-05', False), ('2026-10-06', False), ('2026-10-07', True),
        ]

    def test_end_count_becomes_end_date(self, auth_client, user, pet):
        event = _series(user, pet, end_count=10)
        _post(auth_client, event, 'cancel', date='2026-10-08', scope='future')
        event.recurrence.refresh_from_db()
        assert event.recurrence.end_count is None
        assert event.recurrence.end_date == date(2026, 10, 8)

    def test_from_first_date(self, auth_client, user, pet):
        event = _series(user, pet)
        assert _post(auth_client, event, 'cancel', date='2026-10-05', scope='future').status_code == 200
        data = auth_client.get('/event_schedule/period/', {'date_from': '2026-10-05', 'date_to': '2026-10-12'}).data
        assert list(data) == ['2026-10-05']
        assert data['2026-10-05'][0]['cancelled'] is True

    def test_from_last_date_of_series(self, auth_client, user, pet):
        event = _series(user, pet, end_count=3)  # 5, 6, 7 октября
        assert _post(auth_client, event, 'cancel', date='2026-10-07', scope='future').status_code == 200
        event.recurrence.refresh_from_db()
        assert event.recurrence.end_date == date(2026, 10, 7)

    def test_state_after_date_is_dropped(self, auth_client, user, pet):
        event = _series(user, pet)
        later = date(2026, 10, 9)
        EventCancellation.objects.create(event=event, occurrence_date=later)
        EventCompletion.objects.create(event=event, occurrence_date=later)
        EventOccurrenceOverride.objects.create(event=event, occurrence_date=later, title='X')
        EventCompletion.objects.create(event=event, occurrence_date=date(2026, 10, 5))

        _post(auth_client, event, 'cancel', date='2026-10-07', scope='future')

        assert not EventCancellation.objects.filter(occurrence_date=later).exists()
        assert not EventCompletion.objects.filter(occurrence_date=later).exists()
        assert not EventOccurrenceOverride.objects.filter(occurrence_date=later).exists()
        assert EventCompletion.objects.filter(occurrence_date=date(2026, 10, 5)).exists()

    def test_future_from_future_date(self, auth_client, user, pet):
        event = _series(user, pet)
        assert _post(auth_client, event, 'cancel', date='2027-01-10', scope='future').status_code == 200
        event.recurrence.refresh_from_db()
        assert event.recurrence.end_date == date(2027, 1, 10)

    def test_single_event_future_equals_one(self, auth_client, user, pet):
        event = Event.objects.create(user=user, pet=pet, title='Визит', start_date=D, time=time(16, 0))
        r = _post(auth_client, event, 'cancel', date='2026-10-06', scope='future')
        assert r.status_code == 200
        assert r.data['cancelled'] is True

    def test_done_on_cancel_date_conflicts(self, auth_client, user, pet):
        event = _series(user, pet)
        EventCompletion.objects.create(event=event, occurrence_date=D)
        assert _post(auth_client, event, 'cancel', date='2026-10-06', scope='future').status_code == 409
        event.recurrence.refresh_from_db()
        assert event.recurrence.end_date is None

    def test_until_is_recomputed(self, auth_client, user, pet):
        event = _series(user, pet)
        _post(auth_client, event, 'cancel', date='2026-10-06', scope='future')
        event.recurrence.refresh_from_db()
        assert event.recurrence.until == D


@pytest.mark.django_db
class TestMultiSlot:
    TIMES = ['08:00', '14:00', '20:00']

    def test_cancel_one_slot(self, auth_client, user, pet):
        event = _series(user, pet, times=self.TIMES)
        r = _post(auth_client, event, 'cancel', date='2026-10-06', time='14:00')
        assert r.status_code == 200, r.data
        assert r.data['time'] == '14:00:00'
        data = auth_client.get('/event_schedule/period/', {'date_from': '2026-10-06', 'date_to': '2026-10-06'}).data
        assert [(i['time'], i['cancelled']) for i in data['2026-10-06']] == [
            ('08:00:00', False), ('14:00:00', True), ('20:00:00', False),
        ]

    def test_time_required(self, auth_client, user, pet):
        event = _series(user, pet, times=self.TIMES)
        assert _post(auth_client, event, 'cancel', date='2026-10-06').status_code == 400

    def test_first_slot_sees_legacy_null_row(self, auth_client, user, pet):
        event = _series(user, pet, times=self.TIMES)
        EventCancellation.objects.create(event=event, occurrence_date=D, occurrence_time=None)
        r = _post(auth_client, event, 'cancel', date='2026-10-06', time='08:00')
        assert r.status_code == 200
        assert EventCancellation.objects.count() == 1

    def test_future_cancels_all_slots_of_day(self, auth_client, user, pet):
        event = _series(user, pet, times=self.TIMES)
        _post(auth_client, event, 'cancel', date='2026-10-06', time='14:00', scope='future')
        data = auth_client.get('/event_schedule/period/', {'date_from': '2026-10-05', 'date_to': '2026-10-09'}).data
        assert sorted(data) == ['2026-10-05', '2026-10-06']
        assert [i['cancelled'] for i in data['2026-10-06']] == [True, True, True]
        assert [i['cancelled'] for i in data['2026-10-05']] == [False, False, False]

    def test_future_keeps_done_slot_of_day(self, auth_client, user, pet):
        event = _series(user, pet, times=self.TIMES)
        EventCompletion.objects.create(event=event, occurrence_date=D, occurrence_time=time(8, 0))
        _post(auth_client, event, 'cancel', date='2026-10-06', time='14:00', scope='future')
        data = auth_client.get('/event_schedule/period/', {'date_from': '2026-10-06', 'date_to': '2026-10-06'}).data
        assert [(i['done'], i['cancelled']) for i in data['2026-10-06']] == [
            (True, False), (False, True), (False, True),
        ]

    def test_mark_done_cancelled_slot_conflicts_other_slot_ok(self, auth_client, user, pet):
        event = _series(user, pet, times=self.TIMES)
        _post(auth_client, event, 'cancel', date='2026-10-06', time='14:00')
        assert _post(auth_client, event, 'mark_done', date='2026-10-06', time='14:00').status_code == 409
        assert _post(auth_client, event, 'mark_done', date='2026-10-06', time='20:00').status_code == 200

    def test_done_slot_cannot_be_cancelled(self, auth_client, user, pet):
        event = _series(user, pet, times=self.TIMES)
        _post(auth_client, event, 'mark_done', date='2026-10-06', time='14:00')
        assert _post(auth_client, event, 'cancel', date='2026-10-06', time='14:00').status_code == 409
        assert _post(auth_client, event, 'cancel', date='2026-10-06', time='20:00').status_code == 200

    def test_utc_time_in_request(self, auth_client, user, pet):
        # события в UTC+3: слот 14:00 локально = 11:00 UTC на проводе
        event = _series(user, pet, times=self.TIMES, offset=180)
        r = _post(auth_client, event, 'cancel', date='2026-10-06', time='11:00')
        assert r.status_code == 200, r.data
        assert EventCancellation.objects.get().occurrence_time == time(14, 0)

    def test_edited_time_identifies_slot(self, auth_client, user, pet):
        """Слот, сдвинутый правкой даты, опознаётся по эффективному времени (и по исходному тоже)."""
        event = _series(user, pet, times=self.TIMES)
        EventOccurrenceOverride.objects.create(
            event=event, occurrence_date=D, occurrence_time=time(14, 0), time=time(15, 30),
        )
        r = _post(auth_client, event, 'cancel', date='2026-10-06', time='15:30')
        assert r.status_code == 200, r.data
        assert r.data['time'] == '15:30:00'
        assert EventCancellation.objects.get().occurrence_time == time(14, 0)
        assert _post(auth_client, event, 'mark_done', date='2026-10-06', time='15:30').status_code == 409
        assert _post(auth_client, event, 'restore', date='2026-10-06', time='15:30').status_code == 200
        assert not EventCancellation.objects.exists()
        assert _post(auth_client, event, 'mark_done', date='2026-10-06', time='15:30').status_code == 200
        assert EventCompletion.objects.get().occurrence_time == time(14, 0)
        assert _post(auth_client, event, 'cancel', date='2026-10-06', time='14:00').status_code == 409
