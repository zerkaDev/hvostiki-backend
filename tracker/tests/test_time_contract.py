"""Контракт времени события: режимы ``utc`` и ``legacy`` (заголовок ``X-Time-Contract``).

Инвариант: в БД ``Event.time`` всегда локальное время события (``UTC + timezone_offset``).
"""
import datetime
import importlib
import logging

import pytest
from django.apps import apps as django_apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch

from tracker.models import Event, EventNotificationLog, EventTypeChoices
from tracker.tasks import send_event_notifications
from tracker.time_contract import (
    CONTRACT_LEGACY,
    CONTRACT_UTC,
    is_event_path,
    parse_app_version,
    resolve_time_contract,
    time_to_stored,
    time_to_wire,
)

UTC_HEADERS = {'HTTP_X_TIME_CONTRACT': 'utc'}
LEGACY_HEADERS = {}

MODES = [
    pytest.param(UTC_HEADERS, '14:05:00', id='utc'),
    pytest.param(LEGACY_HEADERS, '17:05:00', id='legacy'),
]


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


class TestContractHelpers:
    @pytest.mark.parametrize('value,expected', [
        ('utc', CONTRACT_UTC),
        ('UTC', CONTRACT_UTC),
        (' utc ', CONTRACT_UTC),
        (None, CONTRACT_LEGACY),
        ('', CONTRACT_LEGACY),
        ('v2', CONTRACT_LEGACY),
        ('local', CONTRACT_LEGACY),
    ])
    def test_resolve_time_contract(self, value, expected):
        assert resolve_time_contract(value) == expected

    def test_conversions(self):
        local = datetime.time(17, 5)
        utc = datetime.time(14, 5)
        assert time_to_stored(utc, 180, CONTRACT_UTC) == local
        assert time_to_wire(local, 180, CONTRACT_UTC) == utc
        # legacy: без сдвига в обе стороны
        assert time_to_stored(local, 180, CONTRACT_LEGACY) == local
        assert time_to_wire(local, 180, CONTRACT_LEGACY) == local
        assert time_to_stored(None, 180, CONTRACT_UTC) is None
        assert time_to_wire(None, 180, CONTRACT_UTC) is None

    @pytest.mark.parametrize('offset', [-840, -300, 0, 180, 330, 840])
    def test_roundtrip_is_identity(self, offset):
        value = datetime.time(1, 0)
        stored = time_to_stored(value, offset, CONTRACT_UTC)
        assert time_to_wire(stored, offset, CONTRACT_UTC) == value

    @pytest.mark.parametrize('raw,expected', [
        ('0.1.0+5', (0, 1, 0, 5)),
        ('1.2.3', (1, 2, 3)),
        ('', None),
        (None, None),
        ('abc', None),
        ('1.x', None),
    ])
    def test_parse_app_version(self, raw, expected):
        assert parse_app_version(raw) == expected

    @pytest.mark.parametrize('path,expected', [
        ('/event_schedule/', True),
        ('/event_schedule/period/', True),
        ('/event_schedule/3f2c/mark_done/', True),
        ('/pets/12/upcoming/', True),
        ('/pets/12/', False),
        ('/pets/', False),
        ('/profile/', False),
        ('/auth/send-code/', False),
    ])
    def test_is_event_path(self, path, expected):
        assert is_event_path(path) is expected


@pytest.mark.django_db
class TestCreateAndUpdate:
    @pytest.mark.parametrize('headers,sent', [
        pytest.param(UTC_HEADERS, '14:05', id='utc'),
        pytest.param(LEGACY_HEADERS, '17:05', id='legacy'),
    ])
    def test_create_stores_local_time(self, auth_client, pet, headers, sent):
        response = auth_client.post(
            reverse('event_schedule-list'), _create_payload(pet, sent), format='json', **headers
        )

        assert response.status_code == 201
        event = Event.objects.get(id=response.data['id'])
        # Инвариант: в БД локальное время
        assert event.time == datetime.time(17, 5)

    @pytest.mark.parametrize('headers,sent,returned', [
        pytest.param(UTC_HEADERS, '14:05', '14:05:00', id='utc'),
        pytest.param(LEGACY_HEADERS, '17:05', '17:05:00', id='legacy'),
    ])
    def test_create_response_roundtrips_what_client_sent(
        self, auth_client, pet, headers, sent, returned
    ):
        response = auth_client.post(
            reverse('event_schedule-list'), _create_payload(pet, sent), format='json', **headers
        )

        assert response.status_code == 201
        assert response.data['time'] == returned
        assert response['X-Time-Contract'] == ('utc' if headers else 'legacy')

    def test_create_without_time_is_unaffected(self, auth_client, pet):
        for headers in (UTC_HEADERS, LEGACY_HEADERS):
            response = auth_client.post(
                reverse('event_schedule-list'), _create_payload(pet, None), format='json', **headers
            )
            assert response.status_code == 201
            assert response.data['time'] is None

    @pytest.mark.parametrize('headers,sent', [
        pytest.param(UTC_HEADERS, '15:00', id='utc'),
        pytest.param(LEGACY_HEADERS, '18:00', id='legacy'),
    ])
    def test_patch_time_stores_local_time(self, auth_client, user, pet, headers, sent):
        event = _make_event(user, pet, datetime.time(17, 5))

        response = auth_client.patch(
            reverse('event_schedule-detail', kwargs={'pk': event.id}),
            {'time': sent}, format='json', **headers,
        )

        assert response.status_code == 200
        event.refresh_from_db()
        assert event.time == datetime.time(18, 0)

    @pytest.mark.parametrize('headers', [UTC_HEADERS, LEGACY_HEADERS])
    def test_patch_without_time_keeps_stored_time(self, auth_client, user, pet, headers):
        event = _make_event(user, pet, datetime.time(17, 5))

        response = auth_client.patch(
            reverse('event_schedule-detail', kwargs={'pk': event.id}),
            {'title': 'Renamed'}, format='json', **headers,
        )

        assert response.status_code == 200
        event.refresh_from_db()
        assert event.time == datetime.time(17, 5)

    def test_put_uses_contract_too(self, auth_client, user, pet):
        event = _make_event(user, pet, datetime.time(17, 5))
        payload = _create_payload(pet, '10:00', title='Put')

        response = auth_client.put(
            reverse('event_schedule-detail', kwargs={'pk': event.id}),
            payload, format='json', **UTC_HEADERS,
        )

        assert response.status_code == 200
        event.refresh_from_db()
        assert event.time == datetime.time(13, 0)  # 10:00 UTC + 3 ч

    def test_patch_offset_with_time_uses_new_offset(self, auth_client, user, pet):
        event = _make_event(user, pet, datetime.time(17, 5), offset=180)

        response = auth_client.patch(
            reverse('event_schedule-detail', kwargs={'pk': event.id}),
            {'time': '12:00', 'timezone_offset': 300}, format='json', **UTC_HEADERS,
        )

        assert response.status_code == 200
        event.refresh_from_db()
        assert event.time == datetime.time(17, 0)  # 12:00 UTC + 5 ч


@pytest.mark.django_db
class TestReading:
    def _period(self, client, headers, event):
        url = reverse('event_schedule-period')
        day = event.start_date
        response = client.get(f'{url}?date_from={day}&date_to={day}', **headers)
        assert response.status_code == 200
        return response

    def _upcoming(self, client, headers, pet, event):
        url = reverse('pet-upcoming', kwargs={'pk': pet.id})
        response = client.get(f'{url}?date_from={event.start_date}&days=1', **headers)
        assert response.status_code == 200
        return response

    @pytest.mark.parametrize('headers,expected', MODES)
    def test_period_returns_time_in_requested_mode(self, auth_client, user, pet, headers, expected):
        event = _make_event(user, pet, datetime.time(17, 5))

        response = self._period(auth_client, headers, event)

        assert response.data[str(event.start_date)][0]['time'] == expected

    @pytest.mark.parametrize('headers,expected', MODES)
    def test_upcoming_returns_time_in_requested_mode(self, auth_client, user, pet, headers, expected):
        event = _make_event(user, pet, datetime.time(17, 5))

        response = self._upcoming(auth_client, headers, pet, event)

        assert response.data[str(event.start_date)][0]['time'] == expected

    @pytest.mark.parametrize('headers,expected', [
        pytest.param(UTC_HEADERS, '22:00:00', id='utc'),
        pytest.param(LEGACY_HEADERS, '01:00:00', id='legacy'),
    ])
    def test_midnight_crossing_keeps_local_date(self, auth_client, user, pet, headers, expected):
        # 01:00 по Москве = 22:00 UTC предыдущих суток; дата вхождения остаётся локальной
        event = _make_event(user, pet, datetime.time(1, 0))

        response = self._period(auth_client, headers, event)

        assert list(response.data) == [str(event.start_date)]
        assert response.data[str(event.start_date)][0]['time'] == expected

    @pytest.mark.parametrize('headers', [UTC_HEADERS, LEGACY_HEADERS])
    def test_events_without_time_are_unchanged(self, auth_client, user, pet, headers):
        event = _make_event(user, pet, None)

        response = self._period(auth_client, headers, event)

        assert response.data[str(event.start_date)][0]['time'] is None

    @pytest.mark.parametrize('headers', [UTC_HEADERS, LEGACY_HEADERS])
    def test_sorting_follows_local_time_not_wrapped_utc(self, auth_client, user, pet, headers):
        # Локально 01:00 раньше, чем 23:30. В UTC-строке было бы наоборот (22:00 vs 20:30).
        late = _make_event(user, pet, datetime.time(23, 30))
        early = _make_event(user, pet, datetime.time(1, 0))
        all_day = _make_event(user, pet, None)

        response = self._period(auth_client, headers, early)

        ids = [item['id'] for item in response.data[str(early.start_date)]]
        assert ids == [str(all_day.id), str(early.id), str(late.id)]

    @pytest.mark.parametrize('headers', [UTC_HEADERS, LEGACY_HEADERS])
    def test_sorting_across_different_offsets_follows_real_moment(self, auth_client, user, pet, headers):
        # 10:00 UTC+3 = 07:00Z; 08:00 UTC+0 = 08:00Z → первым должно идти первое
        moscow = _make_event(user, pet, datetime.time(10, 0), offset=180)
        london = _make_event(user, pet, datetime.time(8, 0), offset=0)

        response = self._period(auth_client, headers, moscow)

        ids = [item['id'] for item in response.data[str(moscow.start_date)]]
        assert ids == [str(moscow.id), str(london.id)]

    def test_mode_is_echoed_in_response_header(self, auth_client):
        url = reverse('event_schedule-period')
        params = '?date_from=2026-10-01&date_to=2026-10-02'

        assert auth_client.get(url + params, **UTC_HEADERS)['X-Time-Contract'] == 'utc'
        assert auth_client.get(url + params)['X-Time-Contract'] == 'legacy'
        assert auth_client.get(url + params, HTTP_X_TIME_CONTRACT='junk')['X-Time-Contract'] == 'legacy'


@pytest.mark.django_db
class TestNotificationsFollowStoredLocalTime:
    """Оба режима при одном и том же ЛОКАЛЬНОМ моменте дают пуш в одно и то же время."""

    @pytest.mark.parametrize('headers,sent', [
        pytest.param(UTC_HEADERS, '14:05', id='utc'),
        pytest.param(LEGACY_HEADERS, '17:05', id='legacy'),
    ])
    def test_push_is_sent_at_local_17_05(self, auth_client, pet, headers, sent):
        response = auth_client.post(
            reverse('event_schedule-list'), _create_payload(pet, sent), format='json', **headers
        )
        event = Event.objects.get(id=response.data['id'])

        # 14:05 UTC = 17:05 по Москве (UTC+3)
        now = timezone.make_aware(datetime.datetime(2026, 10, 5, 14, 5), datetime.timezone.utc)
        with patch('django.utils.timezone.now', return_value=now):
            send_event_notifications()

        assert EventNotificationLog.objects.filter(
            event=event, occurrence_date=datetime.date(2026, 10, 5)
        ).exists()

    def test_push_is_not_sent_three_hours_late(self, auth_client, pet):
        """Прежний дефект: пуш приходил в 20:05 (local + offset)."""
        response = auth_client.post(
            reverse('event_schedule-list'), _create_payload(pet, '17:05'), format='json'
        )
        event = Event.objects.get(id=response.data['id'])

        now = timezone.make_aware(datetime.datetime(2026, 10, 5, 17, 5), datetime.timezone.utc)  # 20:05 МСК
        with patch('django.utils.timezone.now', return_value=now):
            send_event_notifications()

        assert not EventNotificationLog.objects.filter(event=event).exists()


@pytest.mark.django_db
class TestUsageLogging:
    URL = '/event_schedule/period/?date_from=2026-10-01&date_to=2026-10-02'

    def _records(self, caplog):
        return [r for r in caplog.records if r.name == 'tracker.time_contract']

    def test_info_line_for_event_requests(self, auth_client, caplog):
        with caplog.at_level(logging.INFO, logger='tracker.time_contract'):
            auth_client.get(self.URL, HTTP_X_APP_VERSION='0.0.1+1')

        (record,) = self._records(caplog)
        assert record.levelno == logging.INFO
        assert record.time_contract == 'legacy'
        assert record.app_version == '0.0.1+1'

    def test_no_log_for_other_endpoints(self, auth_client, caplog):
        with caplog.at_level(logging.INFO, logger='tracker.time_contract'):
            auth_client.get('/profile/')

        assert self._records(caplog) == []

    @override_settings(TIME_CONTRACT_MIN_APP_VERSION='0.1.0')
    def test_warning_when_new_build_forgets_header(self, auth_client, caplog):
        with caplog.at_level(logging.INFO, logger='tracker.time_contract'):
            auth_client.get(self.URL, HTTP_X_APP_VERSION='0.1.0+7')

        (record,) = self._records(caplog)
        assert record.levelno == logging.WARNING
        assert 'time_contract_missing' in record.getMessage()

    @override_settings(TIME_CONTRACT_MIN_APP_VERSION='0.1.0')
    def test_no_warning_for_old_builds_or_with_header(self, auth_client, caplog):
        with caplog.at_level(logging.INFO, logger='tracker.time_contract'):
            auth_client.get(self.URL, HTTP_X_APP_VERSION='0.0.1+1')  # старая сборка
            auth_client.get(self.URL)  # версия неизвестна
            auth_client.get(self.URL, HTTP_X_APP_VERSION='0.1.0+7', **UTC_HEADERS)  # заголовок есть

        assert all(r.levelno == logging.INFO for r in self._records(caplog))

    def test_warnings_disabled_when_threshold_not_set(self, auth_client, caplog):
        with caplog.at_level(logging.INFO, logger='tracker.time_contract'):
            auth_client.get(self.URL, HTTP_X_APP_VERSION='9.9.9')

        assert all(r.levelno == logging.INFO for r in self._records(caplog))


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
