"""Время события: в API — UTC, в БД — локальное (``UTC + timezone_offset``)."""
import datetime
import importlib

import pytest
from django.apps import apps as django_apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch

from tracker.event_time import time_to_stored, time_to_wire
from tracker.models import Event, EventNotificationLog, EventTypeChoices
from tracker.tasks import send_event_notifications


def _create_payload(pet, time, **extra):
    payload = {
        'pet': pet.id,
        'title': 'Walk',
        'start_date': str(datetime.date(2026, 10, 5)),
        'time': time,
        'timezone_offset': 180,
        'is_recurring': False,
        'type': EventTypeChoices.WALKING,
    }
    payload.update(extra)
    return payload


def _make_event(user, pet, local_time, offset=180, start_date=datetime.date(2026, 10, 5), **extra):
    """Событие, как оно лежит в БД: время локальное."""
    return Event.objects.create(
        user=user, pet=pet, title='E', start_date=start_date,
        time=local_time, timezone_offset=offset, type=EventTypeChoices.CUSTOM, **extra,
    )


class TestHelpers:
    def test_conversions(self):
        local = datetime.time(17, 5)
        utc = datetime.time(14, 5)
        assert time_to_stored(utc, 180) == local
        assert time_to_wire(local, 180) == utc
        assert time_to_stored(None, 180) is None
        assert time_to_wire(None, 180) is None

    @pytest.mark.parametrize('offset', [-840, -300, 0, 180, 330, 840])
    def test_roundtrip_is_identity(self, offset):
        value = datetime.time(1, 0)
        assert time_to_wire(time_to_stored(value, offset), offset) == value


@pytest.mark.django_db
class TestCreateAndUpdate:
    def test_create_stores_local_time_and_returns_utc(self, auth_client, pet):
        response = auth_client.post(reverse('event_schedule-list'), _create_payload(pet, '14:05'), format='json')

        assert response.status_code == 201
        assert response.data['time'] == '14:05:00'
        # Инвариант: в БД локальное время
        assert Event.objects.get(id=response.data['id']).time == datetime.time(17, 5)

    def test_create_without_time_is_unaffected(self, auth_client, pet):
        response = auth_client.post(reverse('event_schedule-list'), _create_payload(pet, None), format='json')
        assert response.status_code == 201
        assert response.data['time'] is None

    def test_patch_time_stores_local_time(self, auth_client, user, pet):
        event = _make_event(user, pet, datetime.time(17, 5))

        response = auth_client.patch(
            reverse('event_schedule-detail', kwargs={'pk': event.id}), {'time': '15:00'}, format='json',
        )

        assert response.status_code == 200
        event.refresh_from_db()
        assert event.time == datetime.time(18, 0)

    def test_patch_without_time_keeps_stored_time(self, auth_client, user, pet):
        event = _make_event(user, pet, datetime.time(17, 5))

        response = auth_client.patch(
            reverse('event_schedule-detail', kwargs={'pk': event.id}), {'title': 'Renamed'}, format='json',
        )

        assert response.status_code == 200
        event.refresh_from_db()
        assert event.time == datetime.time(17, 5)

    def test_put_converts_time(self, auth_client, user, pet):
        event = _make_event(user, pet, datetime.time(17, 5))

        response = auth_client.put(
            reverse('event_schedule-detail', kwargs={'pk': event.id}),
            _create_payload(pet, '10:00', title='Put'), format='json',
        )

        assert response.status_code == 200
        event.refresh_from_db()
        assert event.time == datetime.time(13, 0)  # 10:00 UTC + 3 ч

    def test_patch_offset_with_time_uses_new_offset(self, auth_client, user, pet):
        event = _make_event(user, pet, datetime.time(17, 5), offset=180)

        response = auth_client.patch(
            reverse('event_schedule-detail', kwargs={'pk': event.id}),
            {'time': '12:00', 'timezone_offset': 300}, format='json',
        )

        assert response.status_code == 200
        event.refresh_from_db()
        assert event.time == datetime.time(17, 0)  # 12:00 UTC + 5 ч


@pytest.mark.django_db
class TestReading:
    def _period(self, client, event):
        day = event.start_date
        response = client.get(f"{reverse('event_schedule-period')}?date_from={day}&date_to={day}")
        assert response.status_code == 200
        return response

    def test_period_returns_utc(self, auth_client, user, pet):
        event = _make_event(user, pet, datetime.time(17, 5))
        assert self._period(auth_client, event).data[str(event.start_date)][0]['time'] == '14:05:00'

    def test_upcoming_returns_utc(self, auth_client, user, pet):
        event = _make_event(user, pet, datetime.time(17, 5))
        url = reverse('pet-upcoming', kwargs={'pk': pet.id})
        response = auth_client.get(f'{url}?date_from={event.start_date}&days=1')
        assert response.status_code == 200
        assert response.data[str(event.start_date)][0]['time'] == '14:05:00'

    def test_midnight_crossing_keeps_local_date(self, auth_client, user, pet):
        # 01:00 по Москве = 22:00 UTC предыдущих суток; дата вхождения остаётся локальной
        event = _make_event(user, pet, datetime.time(1, 0))

        response = self._period(auth_client, event)

        assert list(response.data) == [str(event.start_date)]
        assert response.data[str(event.start_date)][0]['time'] == '22:00:00'

    def test_events_without_time_are_unchanged(self, auth_client, user, pet):
        event = _make_event(user, pet, None)
        assert self._period(auth_client, event).data[str(event.start_date)][0]['time'] is None

    def test_sorting_follows_local_time_not_wrapped_utc(self, auth_client, user, pet):
        # Локально 01:00 раньше, чем 23:30. В UTC-строке было бы наоборот (22:00 vs 20:30).
        late = _make_event(user, pet, datetime.time(23, 30))
        early = _make_event(user, pet, datetime.time(1, 0))
        all_day = _make_event(user, pet, None)

        response = self._period(auth_client, early)

        ids = [item['id'] for item in response.data[str(early.start_date)]]
        assert ids == [str(all_day.id), str(early.id), str(late.id)]

    def test_sorting_across_different_offsets_follows_real_moment(self, auth_client, user, pet):
        # 10:00 UTC+3 = 07:00Z; 08:00 UTC+0 = 08:00Z → первым должно идти первое
        moscow = _make_event(user, pet, datetime.time(10, 0), offset=180)
        london = _make_event(user, pet, datetime.time(8, 0), offset=0)

        response = self._period(auth_client, moscow)

        ids = [item['id'] for item in response.data[str(moscow.start_date)]]
        assert ids == [str(moscow.id), str(london.id)]


@pytest.mark.django_db
class TestNotificationsFollowStoredLocalTime:
    def test_push_is_sent_at_local_17_05(self, auth_client, pet):
        response = auth_client.post(reverse('event_schedule-list'), _create_payload(pet, '14:05'), format='json')
        event = Event.objects.get(id=response.data['id'])

        # 14:05 UTC = 17:05 по Москве (UTC+3)
        now = timezone.make_aware(datetime.datetime(2026, 10, 5, 14, 5), datetime.timezone.utc)
        with patch('django.utils.timezone.now', return_value=now):
            send_event_notifications()

        assert EventNotificationLog.objects.filter(event=event, occurrence_date=datetime.date(2026, 10, 5)).exists()

    def test_push_is_not_sent_three_hours_late(self, auth_client, pet):
        response = auth_client.post(reverse('event_schedule-list'), _create_payload(pet, '14:05'), format='json')
        event = Event.objects.get(id=response.data['id'])

        now = timezone.make_aware(datetime.datetime(2026, 10, 5, 17, 5), datetime.timezone.utc)  # 20:05 МСК
        with patch('django.utils.timezone.now', return_value=now):
            send_event_notifications()

        assert not EventNotificationLog.objects.filter(event=event).exists()


MIGRATION_NAME = '0018_event_time_to_local'


@pytest.mark.django_db
class TestMigrationFunctions:
    """Функции миграции данных на реальных моделях."""

    @pytest.fixture
    def migration(self):
        return importlib.import_module(f'tracker.migrations.{MIGRATION_NAME}')

    def test_to_local_subtracts_offset(self, migration, user, pet):
        moscow = _make_event(user, pet, datetime.time(20, 5), offset=180)      # было 17:05 + 3 ч
        ny = _make_event(user, pet, datetime.time(12, 0), offset=-300)         # было 17:00 − 5 ч
        india = _make_event(user, pet, datetime.time(22, 35), offset=330)      # было 17:05 + 5:30
        wrap = _make_event(user, pet, datetime.time(2, 0), offset=180)         # было 23:00 + 3 ч → 02:00
        utc = _make_event(user, pet, datetime.time(9, 0), offset=0)
        all_day = _make_event(user, pet, None, offset=180)

        migration.to_local(django_apps, None)

        for event, expected in [
            (moscow, datetime.time(17, 5)),
            (ny, datetime.time(17, 0)),
            (india, datetime.time(17, 5)),
            (wrap, datetime.time(23, 0)),
            (utc, datetime.time(9, 0)),
            (all_day, None),
        ]:
            event.refresh_from_db()
            assert event.time == expected

    def test_reverse_restores_original_values(self, migration, user, pet):
        events = [_make_event(user, pet, datetime.time(h, m), offset=o)
                  for h, m, o in [(20, 5, 180), (12, 0, -300), (0, 30, 840), (23, 59, -840)]]
        before = [e.time for e in events]

        migration.to_local(django_apps, None)
        migration.to_legacy(django_apps, None)

        for event, original in zip(events, before):
            event.refresh_from_db()
            assert event.time == original

    def test_does_not_touch_updated_at(self, migration, user, pet):
        event = _make_event(user, pet, datetime.time(20, 5), offset=180)
        Event.objects.filter(id=event.id).update(updated_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))

        migration.to_local(django_apps, None)

        event.refresh_from_db()
        assert event.updated_at.year == 2026 and event.updated_at.month == 1

    def test_batches_larger_than_batch_size(self, migration, user, pet, monkeypatch):
        monkeypatch.setattr(migration, 'BATCH_SIZE', 2)
        events = [_make_event(user, pet, datetime.time(20, 5), offset=180) for _ in range(5)]

        migration.to_local(django_apps, None)

        for event in events:
            event.refresh_from_db()
            assert event.time == datetime.time(17, 5)


@pytest.mark.django_db(transaction=True)
def test_migration_runs_forward_and_backward_through_executor():
    """Миграция проходит через реальный MigrationExecutor и обратима."""
    previous = [('tracker', '0017_add_mixed_breed')]
    latest = [('tracker', MIGRATION_NAME)]

    executor = MigrationExecutor(connection)
    # Состояние «до»: схема совпадает, меняются только данные
    executor.migrate(previous)
    old_apps = executor.loader.project_state(previous).apps
    User = old_apps.get_model('tracker', 'User')
    Pet = old_apps.get_model('tracker', 'Pet')
    Breed = old_apps.get_model('tracker', 'Breed')
    OldEvent = old_apps.get_model('tracker', 'Event')

    user = User.objects.create(phone_number='79005556677')
    breed = Breed.objects.create(name='Metis', type='dog')
    pet = Pet.objects.create(owner=user, name='P', pet_type='dog', breed=breed, weight=5, color='x')
    event = OldEvent.objects.create(
        user=user, pet=pet, title='E', start_date=datetime.date(2026, 10, 5),
        time=datetime.time(20, 5), timezone_offset=180,
    )

    try:
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        new_apps = executor.loader.project_state(latest).apps
        assert new_apps.get_model('tracker', 'Event').objects.get(id=event.id).time == datetime.time(17, 5)

        executor = MigrationExecutor(connection)
        executor.migrate(previous)
        old_apps = executor.loader.project_state(previous).apps
        assert old_apps.get_model('tracker', 'Event').objects.get(id=event.id).time == datetime.time(20, 5)
    finally:
        # вернуть БД в актуальное состояние для остальных тестов
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
