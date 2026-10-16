"""Редактирование: ``edit`` (one/future), разделение серии и сброс отмен при смене расписания."""
from datetime import date, time

import pytest

from tracker.models import (
    Event, EventCancellation, EventCompletion, EventNotificationLog, EventOccurrenceOverride, User,
)
from tracker.tests.test_event_cancel_api import _series

URL = '/event_schedule/'
D = date(2026, 10, 8)


def _edit(client, event, scope, form, day='2026-10-08', **extra):
    return client.post(f'{URL}{event.id}/edit/', {'scope': scope, 'date': day, 'event': form, **extra}, format='json')


def _period(client, day_from, day_to):
    return client.get('/event_schedule/period/', {'date_from': day_from, 'date_to': day_to}).data


@pytest.mark.django_db
class TestEditOne:
    def test_override_fields(self, auth_client, user, pet):
        event = _series(user, pet)
        r = _edit(auth_client, event, 'one', {'title': 'Другое', 'description': 'Заметка', 'type': 'custom', 'time': '09:30'})
        assert r.status_code == 200, r.data
        assert (r.data['event']['title'], r.data['event']['time'], r.data['event']['type']) == ('Другое', '09:30:00', 'custom')
        row = EventOccurrenceOverride.objects.get()
        assert (row.occurrence_date, row.time, row.occurrence_time) == (D, time(9, 30), None)
        # серия не изменилась, соседние даты тоже
        data = _period(auth_client, '2026-10-07', '2026-10-09')
        assert [i['title'] for d in sorted(data) for i in data[d]] == ['Таблетки', 'Другое', 'Таблетки']
        assert Event.objects.count() == 1

    def test_second_edit_keeps_other_fields(self, auth_client, user, pet):
        event = _series(user, pet)
        _edit(auth_client, event, 'one', {'title': 'А'})
        _edit(auth_client, event, 'one', {'description': 'Б'})
        row = EventOccurrenceOverride.objects.get()
        assert (row.title, row.description) == ('А', 'Б')

    def test_utc_time_is_stored_local(self, auth_client, user, pet):
        event = _series(user, pet, offset=180)
        _edit(auth_client, event, 'one', {'time': '09:00'})
        assert EventOccurrenceOverride.objects.get().time == time(12, 0)

    def test_forbidden_fields(self, auth_client, user, pet):
        event = _series(user, pet)
        r = _edit(auth_client, event, 'one', {'title': 'X', 'start_date': '2026-10-09'})
        assert r.status_code == 400
        assert not EventOccurrenceOverride.objects.exists()

    def test_invalid_values(self, auth_client, user, pet):
        event = _series(user, pet)
        assert _edit(auth_client, event, 'one', {'type': 'nope'}).status_code == 400
        assert _edit(auth_client, event, 'one', {'title': ''}).status_code == 400

    def test_cancelled_conflicts(self, auth_client, user, pet):
        event = _series(user, pet)
        EventCancellation.objects.create(event=event, occurrence_date=D)
        assert _edit(auth_client, event, 'one', {'title': 'X'}).status_code == 409

    def test_validation_errors(self, auth_client, user, pet):
        event = _series(user, pet)
        assert auth_client.post(f'{URL}{event.id}/edit/', {'date': '2026-10-08', 'event': {}}, format='json').status_code == 400
        assert _edit(auth_client, event, 'all', {}).status_code == 400
        assert auth_client.post(f'{URL}{event.id}/edit/', {'scope': 'one', 'date': '2026-10-08'}, format='json').status_code == 400
        assert _edit(auth_client, event, 'one', {'title': 'X'}, day='2025-01-01').status_code == 400

    def test_foreign_event_is_404(self, auth_client, pet):
        other = User.objects.create_user(phone_number='79005554433')
        event = Event.objects.create(user=other, pet=pet, title='Чужое', start_date=D)
        assert _edit(auth_client, event, 'one', {'title': 'X'}).status_code == 404

    def test_multi_slot_override_and_done_by_edited_time(self, auth_client, user, pet):
        event = _series(user, pet, times=['08:00', '14:00'])
        r = _edit(auth_client, event, 'one', {'time': '15:00'}, time='14:00')
        assert r.status_code == 200, r.data
        assert r.data['event']['time'] == '15:00:00'
        assert EventOccurrenceOverride.objects.get().occurrence_time == time(14, 0)
        data = _period(auth_client, '2026-10-08', '2026-10-08')['2026-10-08']
        assert [i['time'] for i in data] == ['08:00:00', '15:00:00']

    def test_single_event(self, auth_client, user, pet):
        event = Event.objects.create(user=user, pet=pet, title='Визит', start_date=D, time=time(16, 0))
        assert _edit(auth_client, event, 'one', {'title': 'Приём'}).status_code == 200
        assert _period(auth_client, '2026-10-08', '2026-10-08')['2026-10-08'][0]['title'] == 'Приём'


@pytest.mark.django_db
class TestEditFuture:
    def test_split(self, auth_client, user, pet):
        event = _series(user, pet)
        r = _edit(auth_client, event, 'future', {'title': 'Новое', 'time': '09:00'})
        assert r.status_code == 200, r.data
        new = Event.objects.exclude(pk=event.pk).get()
        assert r.data['created_event']['id'] == str(new.pk)
        assert new.title == 'Новое'
        assert new.start_date == D
        assert new.time == time(9, 0)
        assert new.recurrence.frequency == 'daily'
        event.recurrence.refresh_from_db()
        assert event.recurrence.end_date == date(2026, 10, 7)
        assert event.recurrence.end_count is None

        data = _period(auth_client, '2026-10-06', '2026-10-09')
        assert [(d, data[d][0]['title']) for d in sorted(data)] == [
            ('2026-10-06', 'Таблетки'), ('2026-10-07', 'Таблетки'), ('2026-10-08', 'Новое'), ('2026-10-09', 'Новое'),
        ]
        assert all(len(items) == 1 for items in data.values())

    def test_carries_unchanged_fields(self, auth_client, user, pet):
        event = _series(user, pet)
        event.description = 'Одна капсула'
        event.save()
        _edit(auth_client, event, 'future', {'title': 'Новое'})
        new = Event.objects.exclude(pk=event.pk).get()
        assert (new.description, new.time, new.type, new.pet_id) == ('Одна капсула', time(8, 0), 'dailyPills', pet.pk)

    def test_moves_completions_cancellations_overrides(self, auth_client, user, pet):
        event = _series(user, pet)
        EventCompletion.objects.create(event=event, occurrence_date=date(2026, 10, 7))
        EventCompletion.objects.create(event=event, occurrence_date=D)
        EventCompletion.objects.create(event=event, occurrence_date=date(2026, 10, 9))
        EventCancellation.objects.create(event=event, occurrence_date=date(2026, 10, 10))
        EventOccurrenceOverride.objects.create(event=event, occurrence_date=date(2026, 10, 11), title='Правка')

        _edit(auth_client, event, 'future', {'time': '10:00'})
        new = Event.objects.exclude(pk=event.pk).get()

        assert list(EventCompletion.objects.filter(event=event).values_list('occurrence_date', flat=True)) == [date(2026, 10, 7)]
        assert sorted(EventCompletion.objects.filter(event=new).values_list('occurrence_date', flat=True)) == [
            date(2026, 10, 8), date(2026, 10, 9),
        ]
        assert EventCancellation.objects.get().event_id == new.pk
        assert EventOccurrenceOverride.objects.get().event_id == new.pk
        data = _period(auth_client, '2026-10-08', '2026-10-11')
        assert [i['done'] for d in sorted(data) for i in data[d]] == [True, True, False, False]
        assert [i['cancelled'] for d in sorted(data) for i in data[d]] == [False, False, True, False]
        assert data['2026-10-11'][0]['title'] == 'Правка'

    def test_new_schedule_drops_state_of_missing_dates(self, auth_client, user, pet):
        event = _series(user, pet)
        EventCancellation.objects.create(event=event, occurrence_date=date(2026, 10, 9))   # пятница
        EventCancellation.objects.create(event=event, occurrence_date=date(2026, 10, 12))  # понедельник
        _edit(auth_client, event, 'future', {
            'recurrence': {'frequency': 'weekly', 'interval': 1, 'week_days': [1]},  # по понедельникам
        })
        new = Event.objects.exclude(pk=event.pk).get()
        assert list(EventCancellation.objects.values_list('event_id', 'occurrence_date')) == [(new.pk, date(2026, 10, 12))]

    def test_end_count_remainder(self, auth_client, user, pet):
        event = _series(user, pet, end_count=10)  # 5..14 октября
        _edit(auth_client, event, 'future', {'title': 'Новое'})
        new = Event.objects.exclude(pk=event.pk).get()
        assert new.recurrence.end_count == 7  # 8..14
        assert new.recurrence.until == date(2026, 10, 14)
        event.recurrence.refresh_from_db()
        assert (event.recurrence.end_date, event.recurrence.end_count) == (date(2026, 10, 7), None)

    def test_form_recurrence_replaces_rule(self, auth_client, user, pet):
        event = _series(user, pet, end_count=10)
        _edit(auth_client, event, 'future', {'recurrence': {'frequency': 'daily', 'interval': 2}})
        new = Event.objects.exclude(pk=event.pk).get()
        assert (new.recurrence.interval, new.recurrence.end_count, new.recurrence.end_date) == (2, None, None)

    def test_first_date_is_plain_update(self, auth_client, user, pet):
        event = _series(user, pet)
        EventCancellation.objects.create(event=event, occurrence_date=date(2026, 10, 9))
        r = _edit(auth_client, event, 'future', {'title': 'Новое'}, day='2026-10-05')
        assert r.status_code == 200, r.data
        assert Event.objects.count() == 1
        event.refresh_from_db()
        assert event.title == 'Новое'
        assert 'created_event' not in r.data
        assert EventCancellation.objects.count() == 1  # дата осталась в расписании

    def test_single_event_is_plain_update(self, auth_client, user, pet):
        event = Event.objects.create(user=user, pet=pet, title='Визит', start_date=D, time=time(16, 0))
        assert _edit(auth_client, event, 'future', {'title': 'Приём'}).status_code == 200
        event.refresh_from_db()
        assert event.title == 'Приём'
        assert Event.objects.count() == 1

    def test_invalid_form_changes_nothing(self, auth_client, user, pet):
        event = _series(user, pet)
        r = _edit(auth_client, event, 'future', {'recurrence': {'frequency': 'weekly', 'week_days': []}})
        assert r.status_code == 400
        assert Event.objects.count() == 1
        event.recurrence.refresh_from_db()
        assert event.recurrence.end_date is None

    def test_notification_log_moves(self, auth_client, user, pet):
        event = _series(user, pet)
        EventNotificationLog.objects.create(event=event, occurrence_date=D)
        _edit(auth_client, event, 'future', {'time': '10:00'})
        new = Event.objects.exclude(pk=event.pk).get()
        assert EventNotificationLog.objects.get().event_id == new.pk

    def test_multi_slot_completion_follows_slot_order(self, auth_client, user, pet):
        event = _series(user, pet, times=['08:00', '14:00'])
        EventCompletion.objects.create(event=event, occurrence_date=D, occurrence_time=time(14, 0))
        _edit(auth_client, event, 'future', {'recurrence': {'frequency': 'daily', 'times': ['09:00', '15:00']}}, time='14:00')
        new = Event.objects.exclude(pk=event.pk).get()
        assert EventCompletion.objects.get().event_id == new.pk
        assert EventCompletion.objects.get().occurrence_time == time(15, 0)

    def test_multi_slot_count_mismatch_drops_slot_state(self, auth_client, user, pet):
        event = _series(user, pet, times=['08:00', '14:00'])
        EventCompletion.objects.create(event=event, occurrence_date=D, occurrence_time=time(14, 0))
        EventCancellation.objects.create(event=event, occurrence_date=D, occurrence_time=time(8, 0))
        _edit(auth_client, event, 'future', {'recurrence': {'frequency': 'daily'}, 'time': '09:00'}, time='14:00')
        assert not EventCompletion.objects.exists()
        assert not EventCancellation.objects.exists()

    def test_pet_can_change(self, auth_client, user, pet, breed):
        from tracker.models import Pet, PetType
        other_pet = Pet.objects.create(owner=user, name='Мурка', pet_type=PetType.DOG, breed=breed, weight=5, color='x')
        event = _series(user, pet)
        _edit(auth_client, event, 'future', {'pet': other_pet.pk})
        assert Event.objects.exclude(pk=event.pk).get().pet_id == other_pet.pk


@pytest.mark.django_db
class TestScheduleChangeResetsState:
    def _patch(self, client, event, body):
        r = client.patch(f'{URL}{event.id}/', body, format='json')
        assert r.status_code == 200, r.data

    def test_removed_dates_are_dropped_kept_dates_stay(self, auth_client, user, pet):
        event = _series(user, pet)
        for day in (date(2026, 10, 7), date(2026, 10, 9), date(2026, 10, 12)):
            EventCancellation.objects.create(event=event, occurrence_date=day)
            EventOccurrenceOverride.objects.create(event=event, occurrence_date=day, title='X')
        # теперь только понедельники: 5 и 12 октября
        self._patch(auth_client, event, {'recurrence': {'frequency': 'weekly', 'interval': 1, 'week_days': [1]}})
        assert list(EventCancellation.objects.values_list('occurrence_date', flat=True)) == [date(2026, 10, 12)]
        assert list(EventOccurrenceOverride.objects.values_list('occurrence_date', flat=True)) == [date(2026, 10, 12)]

    def test_start_date_forward_trims(self, auth_client, user, pet):
        event = _series(user, pet)
        EventCancellation.objects.create(event=event, occurrence_date=date(2026, 10, 6))
        EventCancellation.objects.create(event=event, occurrence_date=date(2026, 10, 10))
        self._patch(auth_client, event, {'start_date': '2026-10-08'})
        assert list(EventCancellation.objects.values_list('occurrence_date', flat=True)) == [date(2026, 10, 10)]

    def test_end_date_trims_tail(self, auth_client, user, pet):
        event = _series(user, pet)
        EventCancellation.objects.create(event=event, occurrence_date=date(2026, 10, 20))
        self._patch(auth_client, event, {'recurrence': {'frequency': 'daily', 'end_date': '2026-10-15'}})
        assert not EventCancellation.objects.exists()

    def test_title_change_keeps_state(self, auth_client, user, pet):
        event = _series(user, pet)
        EventCancellation.objects.create(event=event, occurrence_date=D)
        EventOccurrenceOverride.objects.create(event=event, occurrence_date=D, title='X')
        self._patch(auth_client, event, {'title': 'Новое'})
        assert EventCancellation.objects.count() == 1
        assert EventOccurrenceOverride.objects.count() == 1

    def test_slots_change_drops_stale_slot_rows(self, auth_client, user, pet):
        event = _series(user, pet, times=['08:00', '14:00'])
        EventCancellation.objects.create(event=event, occurrence_date=D, occurrence_time=time(14, 0))
        EventCancellation.objects.create(event=event, occurrence_date=D, occurrence_time=time(8, 0))
        self._patch(auth_client, event, {'recurrence': {'frequency': 'daily', 'times': ['08:00', '20:00']}})
        assert list(EventCancellation.objects.values_list('occurrence_time', flat=True)) == [time(8, 0)]
