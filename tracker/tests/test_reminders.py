"""Напоминание о событии: смещение до вхождения (``reminders``), API и рассылка пушей."""
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from unittest.mock import patch

import pytest

from tracker import reminders as rem
from tracker.models import (
    Event, EventCancellation, EventCompletion, EventNotificationLog, EventNotificationType,
    EventOccurrenceOverride, FCMDevice, Notification, RecurrenceRule,
)
from tracker.tasks import send_event_notifications
from tracker.tests.test_event_cancel_api import _series

URL = '/event_schedule/'
D = date(2026, 10, 20)


def _utc(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=dt_timezone.utc)


def _send(at):
    with patch('tracker.tasks.firebase_service'), patch('django.utils.timezone.now', return_value=at):
        send_event_notifications()


def _event(user, pet, *, reminders=None, at=time(15, 0), day=D, **extra):
    return Event.objects.create(
        user=user, pet=pet, title='Визит', start_date=day, time=at, reminders=reminders, **extra,
    )


def _logs(event):
    return list(
        EventNotificationLog.objects.filter(event=event)
        .order_by('occurrence_date').values_list('occurrence_date', 'notification_type')
    )


class TestReminderRules:
    def test_validate(self):
        assert rem.validate_reminders(None, all_day=False) is None
        assert rem.validate_reminders([], all_day=False) == []
        assert rem.validate_reminders([0], all_day=False) == [0]
        assert rem.validate_reminders([43200], all_day=False) == [43200]
        assert rem.validate_reminders([2880], all_day=True) == [2880]

    @pytest.mark.parametrize('value', [[-1], [43201], [1, 2], 'x', [1.5], [True], ['10']])
    def test_validate_rejects(self, value):
        with pytest.raises(rem.ReminderError):
            rem.validate_reminders(value, all_day=False)

    def test_all_day_needs_whole_days(self):
        with pytest.raises(rem.ReminderError):
            rem.validate_reminders([60], all_day=True)

    def test_moment(self):
        assert rem.reminder_moment(D, time(15, 0), 10) == datetime(2026, 10, 20, 14, 50)
        assert rem.reminder_moment(D, None, 1440) == datetime(2026, 10, 19, 9, 0)
        assert rem.reminder_moment(D, time(0, 5), 10) == datetime(2026, 10, 19, 23, 55)

    @pytest.mark.parametrize('minutes, at, text', [
        (1, None, 'через 1 минуту'),
        (10, None, 'через 10 минут'),
        (22, None, 'через 22 минуты'),
        (60, None, 'через 1 час'),
        (180, None, 'через 3 часа'),
        (1440, time(15, 0), 'завтра в 15:00'),
        (1440, None, 'завтра'),
        (2880, time(15, 0), 'через 2 дня в 15:00'),
        (5 * 1440, None, 'через 5 дней'),
        (10080, time(9, 5), 'через 1 неделю в 09:05'),
        (3 * 10080, None, 'через 3 недели'),
        (90, None, 'через 90 минут'),
    ])
    def test_lead_text(self, minutes, at, text):
        assert rem.lead_text(minutes, at) == text


@pytest.mark.django_db
class TestReminderApi:
    def _body(self, pet, **extra):
        return {
            'pet': pet.id, 'title': 'Визит', 'start_date': '2026-10-20', 'time': '15:00',
            'timezone_offset': 0, 'is_recurring': False, **extra,
        }

    def test_default_is_null(self, auth_client, pet):
        r = auth_client.post(URL, self._body(pet), format='json')
        assert r.status_code == 201, r.data
        assert r.data['reminders'] is None

    @pytest.mark.parametrize('value', [[], [0], [1440], [43200]])
    def test_create_and_read(self, auth_client, pet, value):
        r = auth_client.post(URL, self._body(pet, reminders=value), format='json')
        assert r.status_code == 201, r.data
        assert r.data['reminders'] == value
        assert Event.objects.get().reminders == value
        got = auth_client.get(f'{URL}{r.data["id"]}/')
        assert got.data['reminders'] == value

    @pytest.mark.parametrize('value', [[-1], [43201], [1, 2], ['a'], [1.5]])
    def test_create_rejects_bad_values(self, auth_client, pet, value):
        r = auth_client.post(URL, self._body(pet, reminders=value), format='json')
        assert r.status_code == 400
        assert 'reminders' in r.data

    def test_all_day_requires_whole_days(self, auth_client, pet):
        body = self._body(pet, time=None)
        assert auth_client.post(URL, {**body, 'reminders': [60]}, format='json').status_code == 400
        assert auth_client.post(URL, {**body, 'reminders': [1440]}, format='json').status_code == 201

    def test_patch_updates_and_clears(self, auth_client, user, pet):
        event = _event(user, pet, reminders=[10])
        r = auth_client.patch(f'{URL}{event.id}/', {'reminders': [60]}, format='json')
        assert r.status_code == 200 and r.data['reminders'] == [60]
        r = auth_client.patch(f'{URL}{event.id}/', {'reminders': None}, format='json')
        assert r.status_code == 200 and r.data['reminders'] is None
        # поле не передано — значение не меняется
        auth_client.patch(f'{URL}{event.id}/', {'reminders': [5]}, format='json')
        auth_client.patch(f'{URL}{event.id}/', {'title': 'Другое'}, format='json')
        event.refresh_from_db()
        assert event.reminders == [5]

    def test_patch_time_removal_conflicts_with_minutes(self, auth_client, user, pet):
        event = _event(user, pet, reminders=[60])
        r = auth_client.patch(f'{URL}{event.id}/', {'time': None}, format='json')
        assert r.status_code == 400
        r = auth_client.patch(f'{URL}{event.id}/', {'time': None, 'reminders': [1440]}, format='json')
        assert r.status_code == 200

    def test_period_and_upcoming_show_reminders(self, auth_client, user, pet):
        event = _event(user, pet, reminders=[30])
        EventOccurrenceOverride.objects.create(event=event, occurrence_date=D, reminders=[1440])
        data = auth_client.get(URL + 'period/', {'date_from': '2026-10-20', 'date_to': '2026-10-20'}).data
        assert data['2026-10-20'][0]['reminders'] == [1440]

    def test_period_without_override_uses_event_value(self, auth_client, user, pet):
        rule = _series(user, pet)
        rule.reminders = [10]
        rule.save()
        EventOccurrenceOverride.objects.create(event=rule, occurrence_date=date(2026, 10, 8), reminders=[])
        data = auth_client.get(URL + 'period/', {'date_from': '2026-10-07', 'date_to': '2026-10-09'}).data
        assert [i['reminders'] for d in sorted(data) for i in data[d]] == [[10], [], [10]]


def _edit(client, event, scope, form, day='2026-10-08'):
    return client.post(f'{URL}{event.id}/edit/', {'scope': scope, 'date': day, 'event': form}, format='json')


@pytest.mark.django_db
class TestReminderEdit:
    def test_one_sets_override(self, auth_client, user, pet):
        event = _series(user, pet)
        r = _edit(auth_client, event, 'one', {'reminders': [30]})
        assert r.status_code == 200, r.data
        assert r.data['event']['reminders'] == [30]
        assert EventOccurrenceOverride.objects.get().reminders == [30]
        event.refresh_from_db()
        assert event.reminders is None

    def test_one_empty_list_means_none_and_null_resets(self, auth_client, user, pet):
        event = _series(user, pet)
        _edit(auth_client, event, 'one', {'reminders': []})
        assert EventOccurrenceOverride.objects.get().reminders == []
        _edit(auth_client, event, 'one', {'reminders': None})
        assert EventOccurrenceOverride.objects.get().reminders is None

    def test_one_validates(self, auth_client, user, pet):
        event = _series(user, pet)
        assert _edit(auth_client, event, 'one', {'reminders': [-5]}).status_code == 400
        assert _edit(auth_client, event, 'one', {'reminders': [1, 2]}).status_code == 400

    def test_one_all_day_requires_days(self, auth_client, user, pet):
        event = _series(user, pet)
        event.time = None
        event.save()
        assert _edit(auth_client, event, 'one', {'reminders': [90]}).status_code == 400
        assert _edit(auth_client, event, 'one', {'reminders': [1440]}).status_code == 200

    def test_future_split_carries_and_changes(self, auth_client, user, pet):
        event = _series(user, pet)
        event.reminders = [10]
        event.save()
        r = _edit(auth_client, event, 'future', {'title': 'Новое'})
        assert r.status_code == 200, r.data
        assert r.data['created_event']['reminders'] == [10]
        created = Event.objects.exclude(pk=event.pk).get()
        assert created.reminders == [10]

        r = _edit(auth_client, created, 'future', {'reminders': [60]}, day=str(created.start_date))
        assert r.status_code == 200
        created.refresh_from_db()
        assert created.reminders == [60]

    def test_future_split_applies_new_value(self, auth_client, user, pet):
        event = _series(user, pet)
        event.reminders = [10]
        event.save()
        r = _edit(auth_client, event, 'future', {'reminders': [1440]})
        assert r.data['created_event']['reminders'] == [1440]
        event.refresh_from_db()
        assert event.reminders == [10]


@pytest.mark.django_db
class TestReminderNotifications:
    def test_before_event(self, user, pet):
        event = _event(user, pet, reminders=[10])
        _send(_utc(D, 14, 40))
        assert _logs(event) == []
        _send(_utc(D, 14, 50))
        assert _logs(event) == [(D, EventNotificationType.REMINDER)]
        _send(_utc(D, 15, 0))
        # «в момент события» по умолчанию не добавляется
        assert _logs(event) == [(D, EventNotificationType.REMINDER)]

    def test_text_goes_to_inbox(self, user, pet):
        event = _event(user, pet, reminders=[1440], description='Взять паспорт')
        _send(_utc(D - timedelta(days=1), 15, 0))
        note = Notification.objects.get()
        assert (note.title, note.body) == ('Визит', 'Buddy: завтра в 15:00')
        assert note.notification_type == 'reminder' and note.occurrence_date == D and note.event == event

    def test_at_event_time_is_standard(self, user, pet):
        event = _event(user, pet, reminders=[0])
        _send(_utc(D, 15, 0))
        assert _logs(event) == [(D, EventNotificationType.STANDARD)]
        assert Notification.objects.get().body == 'Buddy: пора выполнить'

    def test_none_never_notifies(self, user, pet):
        event = _event(user, pet, reminders=[])
        for at in (_utc(D - timedelta(days=1), 15), _utc(D, 14, 50), _utc(D, 15)):
            _send(at)
        assert _logs(event) == []

    def test_null_keeps_old_behaviour(self, user, pet):
        event = _event(user, pet, reminders=None)
        _send(_utc(D, 14, 50))
        assert _logs(event) == []
        _send(_utc(D, 15, 0))
        assert _logs(event) == [(D, EventNotificationType.STANDARD)]

    @pytest.mark.parametrize('minutes, at', [
        (60, _utc(D, 14, 0)),
        (2880, _utc(D - timedelta(days=2), 15, 0)),
        (10080, _utc(D - timedelta(days=7), 15, 0)),
        (43200, _utc(D - timedelta(days=30), 15, 0)),
    ])
    def test_offsets(self, user, pet, minutes, at):
        event = _event(user, pet, reminders=[minutes])
        _send(at)
        assert _logs(event) == [(D, EventNotificationType.REMINDER)]

    def test_far_future_event_is_considered(self, user, pet):
        event = _event(user, pet, reminders=[43200], day=D + timedelta(days=30))
        _send(_utc(D, 15, 0))
        assert _logs(event) == [(D + timedelta(days=30), EventNotificationType.REMINDER)]

    def test_window_and_dedupe(self, user, pet):
        event = _event(user, pet, reminders=[10])
        _send(_utc(D, 14, 53))
        assert _logs(event) == []
        _send(_utc(D, 14, 51))
        _send(_utc(D, 14, 52))
        assert len(_logs(event)) == 1
        assert Notification.objects.count() == 1

    def test_timezone_offset(self, user, pet):
        # локальное 15:00 при UTC+3 — 12:00 UTC; за 10 минут — 11:50 UTC
        event = _event(user, pet, reminders=[10], timezone_offset=180)
        _send(_utc(D, 11, 50))
        assert _logs(event) == [(D, EventNotificationType.REMINDER)]

    def test_midnight_crossing(self, user, pet):
        event = _event(user, pet, reminders=[10], at=time(0, 5))
        _send(_utc(D - timedelta(days=1), 23, 55))
        assert _logs(event) == [(D, EventNotificationType.REMINDER)]

    def test_recurring_every_occurrence(self, user, pet):
        event = _series(user, pet)
        event.reminders = [60]
        event.save()
        _send(_utc(date(2026, 10, 8), 7, 0))
        _send(_utc(date(2026, 10, 9), 7, 0))
        assert [d for d, _ in _logs(event)] == [date(2026, 10, 8), date(2026, 10, 9)]

    def test_cancelled_and_done_are_skipped(self, user, pet):
        event = _series(user, pet)
        event.reminders = [60]
        event.save()
        EventCancellation.objects.create(event=event, occurrence_date=date(2026, 10, 8))
        EventCompletion.objects.create(event=event, occurrence_date=date(2026, 10, 9))
        _send(_utc(date(2026, 10, 8), 7, 0))
        _send(_utc(date(2026, 10, 9), 7, 0))
        assert _logs(event) == []

    def test_moved_event_moves_reminder(self, user, pet):
        event = _event(user, pet, reminders=[15], at=time(9, 0))
        EventOccurrenceOverride.objects.create(event=event, occurrence_date=D, time=time(11, 0))
        _send(_utc(D, 8, 45))
        assert _logs(event) == []
        _send(_utc(D, 10, 45))
        assert _logs(event) == [(D, EventNotificationType.REMINDER)]

    def test_multi_slot_each_slot(self, user, pet):
        rule = RecurrenceRule.objects.create(frequency='daily', times=['08:00', '20:00'])
        event = Event.objects.create(
            user=user, pet=pet, title='Таблетки', start_date=D, time=time(8, 0),
            is_recurring=True, recurrence=rule, reminders=[30],
        )
        _send(_utc(D, 19, 30))
        assert EventNotificationLog.objects.get(event=event).occurrence_time == time(20, 0)

    def test_disabled_category_is_skipped(self, user, pet):
        from tracker.models import NotificationSettings
        NotificationSettings.objects.create(user=user, disabled_categories=['vet_visits'])
        event = _event(user, pet, reminders=[10], type='vetVisit')
        _send(_utc(D, 14, 50))
        assert _logs(event) == []

    def test_user_without_devices_still_gets_inbox_record(self, user, pet):
        assert not FCMDevice.objects.exists()
        _event(user, pet, reminders=[10])
        _send(_utc(D, 14, 50))
        assert Notification.objects.count() == 1

    def test_event_already_past_when_created(self, user, pet):
        # событие в 14:30, напоминание за час, «сейчас» 14:00: момент (13:30) прошёл — пуша не будет
        event = _event(user, pet, reminders=[60], at=time(14, 30))
        _send(_utc(D, 14, 0))
        assert _logs(event) == []


@pytest.mark.django_db
class TestAllDayReminder:
    def test_day_before_at_nine(self, user, pet):
        event = _event(user, pet, reminders=[1440], at=None)
        _send(_utc(D - timedelta(days=1), 9, 0))
        assert _logs(event) == [(D, EventNotificationType.REMINDER)]
        assert Notification.objects.get().body == 'Buddy: завтра'

    def test_same_day_at_nine_not_eight(self, user, pet):
        event = _event(user, pet, reminders=[0], at=None)
        _send(_utc(D, 8, 0))
        assert _logs(event) == []
        _send(_utc(D, 9, 0))
        assert _logs(event) == [(D, EventNotificationType.FINAL)]

    def test_old_all_day_rules_unchanged(self, user, pet):
        event = _event(user, pet, reminders=None, at=None)
        _send(_utc(D, 8, 0))
        _send(_utc(D - timedelta(days=1), 21, 0))
        assert sorted(t for _, t in _logs(event)) == ['final', 'reminder']

    def test_none_silences_all_day(self, user, pet):
        event = _event(user, pet, reminders=[], at=None)
        _send(_utc(D, 8, 0))
        _send(_utc(D - timedelta(days=1), 21, 0))
        _send(_utc(D, 9, 0))
        assert _logs(event) == []


@pytest.mark.django_db
class TestOccurrenceOverrideReminder:
    def test_override_replaces_series_value(self, user, pet):
        event = _event(user, pet, reminders=[10])
        EventOccurrenceOverride.objects.create(event=event, occurrence_date=D, reminders=[60])
        _send(_utc(D, 14, 50))
        assert _logs(event) == []
        _send(_utc(D, 14, 0))
        assert _logs(event) == [(D, EventNotificationType.REMINDER)]

    def test_override_none_silences_one_date(self, user, pet):
        event = _event(user, pet, reminders=[10])
        EventOccurrenceOverride.objects.create(event=event, occurrence_date=D, reminders=[])
        _send(_utc(D, 14, 50))
        assert _logs(event) == []

    def test_override_on_old_event_adds_reminder_for_that_date_only(self, user, pet):
        event = _series(user, pet)  # reminders = None: «как раньше»
        day = date(2026, 10, 8)
        EventOccurrenceOverride.objects.create(event=event, occurrence_date=day, reminders=[30])
        _send(_utc(day, 7, 30))
        assert _logs(event) == [(day, EventNotificationType.REMINDER)]
        # в момент события по правилу «как раньше» эта дата больше не уведомляет
        _send(_utc(day, 8, 0))
        assert len(_logs(event)) == 1
        # соседний день — по-старому, в момент события
        _send(_utc(date(2026, 10, 9), 8, 0))
        assert (date(2026, 10, 9), EventNotificationType.STANDARD) in _logs(event)

    def test_override_far_future_date(self, user, pet):
        event = _series(user, pet)
        day = date(2026, 10, 8) + timedelta(days=20)
        EventOccurrenceOverride.objects.create(event=event, occurrence_date=day, reminders=[20 * 1440])
        _send(_utc(date(2026, 10, 8), 8, 0))
        assert (day, EventNotificationType.REMINDER) in _logs(event)
