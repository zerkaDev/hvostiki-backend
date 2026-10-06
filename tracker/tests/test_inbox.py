"""Центр уведомлений: хранение при рассылке, API списка/прочтения, очистка."""
from datetime import date, datetime, time, timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from tracker.models import (
    Breed, Event, EventNotificationLog, EventTypeChoices, FCMDevice, Notification, NotificationKind,
    NotificationSettings, Pet, PetType, RecurrenceFrequency, RecurrenceRule, User,
)
from tracker.tasks import cleanup_old_notifications, send_event_notifications

LIST_URL = '/notifications/'


def make_notification(user, offset_minutes=0, **extra):
    """Уведомление, созданное ``offset_minutes`` минут назад."""
    fields = {'user': user, 'title': 'Таблетка', 'body': 'Пора'}
    fields.update(extra)
    notification = Notification.objects.create(**fields)
    Notification.objects.filter(pk=notification.pk).update(
        created_at=timezone.now() - timedelta(minutes=offset_minutes)
    )
    notification.refresh_from_db()
    return notification


@pytest.fixture
def other_user(db):
    return User.objects.create_user(phone_number='79005556677')


@pytest.mark.django_db
class TestNotificationList:
    def test_requires_auth(self, api_client):
        assert api_client.get(LIST_URL).status_code == 401

    def test_empty(self, auth_client):
        response = auth_client.get(LIST_URL)
        assert response.status_code == 200
        assert response.json() == {'results': [], 'next_cursor': None, 'unread_count': 0}

    def test_newest_first_and_item_shape(self, auth_client, user, pet):
        old = make_notification(user, offset_minutes=30, title='Старое')
        new = make_notification(
            user, offset_minutes=1, title='Новое', pet=pet, event_type=EventTypeChoices.DAILY_PILLS,
            notification_type='standard', occurrence_date=date(2026, 10, 4),
        )
        results = auth_client.get(LIST_URL).json()['results']
        assert [r['id'] for r in results] == [str(new.id), str(old.id)]
        assert results[0] == {
            'id': str(new.id), 'kind': 'event', 'title': 'Новое', 'body': 'Пора',
            'created_at': results[0]['created_at'], 'is_read': False,
            'pet_id': pet.id, 'event_id': None, 'event_type': 'dailyPills',
            'notification_type': 'standard', 'occurrence_date': '2026-10-04', 'occurrence_time': None,
        }

    def test_only_own_notifications(self, auth_client, user, other_user):
        make_notification(user, title='Моё')
        make_notification(other_user, title='Чужое')
        data = auth_client.get(LIST_URL).json()
        assert [r['title'] for r in data['results']] == ['Моё']
        assert data['unread_count'] == 1

    def test_occurrence_time_is_utc(self, auth_client, user, pet):
        # Событие в UTC+3: слот 10:00 локального времени → 07:00 UTC на проводе
        event = Event.objects.create(
            user=user, pet=pet, title='Прогулка', start_date=date.today(), time=time(10, 0),
            timezone_offset=180,
        )
        make_notification(user, event=event, pet=pet, occurrence_date=date.today(), occurrence_time=time(10, 0))
        assert auth_client.get(LIST_URL).json()['results'][0]['occurrence_time'] == '07:00'

    def test_pagination_walks_all_without_gaps_or_duplicates(self, auth_client, user):
        # Одинаковый created_at у части записей: порядок задаёт id, курсор не должен терять строки
        same = timezone.now() - timedelta(minutes=5)
        ids = []
        for i in range(7):
            n = make_notification(user, title=f'n{i}')
            Notification.objects.filter(pk=n.pk).update(created_at=same if i < 5 else same - timedelta(minutes=i))
            ids.append(str(n.pk))

        seen, cursor, pages = [], None, 0
        while True:
            url = LIST_URL + '?limit=3' + (f'&cursor={cursor}' if cursor else '')
            data = auth_client.get(url).json()
            assert data['unread_count'] == 7
            seen += [r['id'] for r in data['results']]
            pages += 1
            cursor = data['next_cursor']
            if cursor is None:
                break
        assert pages == 3
        assert sorted(seen) == sorted(ids)
        assert len(seen) == len(set(seen))

    def test_exact_page_has_no_next_cursor(self, auth_client, user):
        for _ in range(3):
            make_notification(user)
        assert auth_client.get(LIST_URL + '?limit=3').json()['next_cursor'] is None

    def test_unread_filter_and_count(self, auth_client, user):
        read = make_notification(user, title='Прочитано')
        make_notification(user, title='Новое')
        Notification.objects.filter(pk=read.pk).update(read_at=timezone.now())

        data = auth_client.get(LIST_URL + '?unread=true').json()
        assert [r['title'] for r in data['results']] == ['Новое']
        assert data['unread_count'] == 1
        all_items = auth_client.get(LIST_URL).json()
        assert len(all_items['results']) == 2
        assert all_items['unread_count'] == 1
        assert {r['title']: r['is_read'] for r in all_items['results']} == {'Прочитано': True, 'Новое': False}

    @pytest.mark.parametrize('query', ['limit=0', 'limit=101', 'limit=abc', 'cursor=!!!', 'cursor=YWJj'])
    def test_bad_params(self, auth_client, query):
        response = auth_client.get(f'{LIST_URL}?{query}')
        assert response.status_code == 400
        assert isinstance(response.json()['detail'], str)


@pytest.mark.django_db
class TestNotificationRead:
    def test_unread_count_endpoint(self, auth_client, user, other_user):
        make_notification(user)
        make_notification(user)
        make_notification(other_user)
        assert auth_client.get(LIST_URL + 'unread-count/').json() == {'unread_count': 2}

    def test_mark_read_is_idempotent(self, auth_client, user):
        first = make_notification(user)
        make_notification(user)

        response = auth_client.post(f'{LIST_URL}{first.id}/read/')
        assert response.status_code == 200
        assert response.json() == {'unread_count': 1}
        first.refresh_from_db()
        read_at = first.read_at
        assert read_at is not None

        again = auth_client.post(f'{LIST_URL}{first.id}/read/')
        assert again.status_code == 200
        assert again.json() == {'unread_count': 1}
        first.refresh_from_db()
        assert first.read_at == read_at

    def test_mark_read_foreign_or_unknown_is_404(self, auth_client, other_user):
        foreign = make_notification(other_user)
        assert auth_client.post(f'{LIST_URL}{foreign.id}/read/').status_code == 404
        foreign.refresh_from_db()
        assert foreign.read_at is None
        assert auth_client.post(f'{LIST_URL}00000000-0000-0000-0000-000000000000/read/').status_code == 404

    def test_read_all_only_own(self, auth_client, user, other_user):
        make_notification(user)
        make_notification(user)
        foreign = make_notification(other_user)

        response = auth_client.post(LIST_URL + 'read-all/')
        assert response.status_code == 200
        assert response.json() == {'unread_count': 0}
        assert not Notification.objects.filter(user=user, read_at__isnull=True).exists()
        foreign.refresh_from_db()
        assert foreign.read_at is None

    def test_requires_auth(self, api_client, user):
        n = make_notification(user)
        assert api_client.post(f'{LIST_URL}{n.id}/read/').status_code == 401
        assert api_client.post(LIST_URL + 'read-all/').status_code == 401
        assert api_client.get(LIST_URL + 'unread-count/').status_code == 401


def _at(day, hour, minute=0):
    return timezone.make_aware(datetime.combine(day, time(hour, minute)))


@pytest.mark.django_db
class TestNotificationCreatedOnSend:
    @pytest.fixture(autouse=True)
    def _fcm_mock(self):
        with patch('tracker.tasks.firebase_service') as mocked:
            self.firebase = mocked
            yield

    def _timed_event(self, user, pet, **extra):
        defaults = dict(
            user=user, pet=pet, title='Таблетка', start_date=date.today(), time=time(9, 0),
            type=EventTypeChoices.DAILY_PILLS,
        )
        defaults.update(extra)
        return Event.objects.create(**defaults)

    def test_creates_record_and_adds_id_to_push(self, user, pet):
        FCMDevice.objects.create(user=user, fcm_token='tok')
        event = self._timed_event(user, pet, description='Дать с едой')
        with patch('django.utils.timezone.now', return_value=_at(date.today(), 9, 0)):
            send_event_notifications()

        notification = Notification.objects.get()
        assert notification.user == user
        assert notification.kind == NotificationKind.EVENT
        assert (notification.title, notification.body) == ('Таблетка', 'Дать с едой')
        assert notification.pet == pet and notification.event == event
        assert notification.event_type == EventTypeChoices.DAILY_PILLS
        assert notification.notification_type == 'standard'
        assert notification.occurrence_date == date.today()
        assert notification.read_at is None
        assert notification.log == EventNotificationLog.objects.get()

        kwargs = self.firebase.send_push_notification.call_args.kwargs
        assert kwargs['title'] == 'Таблетка'
        assert kwargs['data']['notification_id'] == str(notification.id)
        assert kwargs['data']['pet_id'] == str(pet.id)
        assert kwargs['data']['event_type'] == 'dailyPills'
        assert kwargs['data']['event_id'] == str(event.id)

    def test_default_body_mentions_pet(self, user, pet):
        self._timed_event(user, pet)
        with patch('django.utils.timezone.now', return_value=_at(date.today(), 9, 0)):
            send_event_notifications()
        assert Notification.objects.get().body == f'{pet.name}: пора выполнить'

    def test_reminder_body(self, user, pet):
        tomorrow = date.today() + timedelta(days=1)
        self._timed_event(user, pet, start_date=tomorrow, time=None)
        with patch('django.utils.timezone.now', return_value=_at(date.today(), 21, 0)):
            send_event_notifications()
        notification = Notification.objects.get()
        assert notification.notification_type == 'reminder'
        assert notification.body == f'{pet.name}: напоминание на завтра'
        assert notification.occurrence_date == tomorrow

    def test_created_without_devices(self, user, pet):
        self._timed_event(user, pet)
        with patch('django.utils.timezone.now', return_value=_at(date.today(), 9, 0)):
            send_event_notifications()
        assert Notification.objects.count() == 1
        self.firebase.send_push_notification.assert_not_called()

    def test_no_duplicate_on_repeated_run(self, user, pet):
        self._timed_event(user, pet)
        with patch('django.utils.timezone.now', return_value=_at(date.today(), 9, 0)):
            send_event_notifications()
            send_event_notifications()
        assert Notification.objects.count() == 1

    def test_rolled_back_when_delivery_fails_then_recreated(self, user, pet):
        FCMDevice.objects.create(user=user, fcm_token='tok')
        self._timed_event(user, pet)
        self.firebase.send_push_notification.side_effect = RuntimeError('fcm down')
        with patch('django.utils.timezone.now', return_value=_at(date.today(), 9, 0)):
            send_event_notifications()
        assert Notification.objects.count() == 0
        assert EventNotificationLog.objects.count() == 0

        self.firebase.send_push_notification.side_effect = None
        with patch('django.utils.timezone.now', return_value=_at(date.today(), 9, 1)):
            send_event_notifications()
        assert Notification.objects.count() == 1

    def test_disabled_category_creates_nothing(self, user, pet):
        NotificationSettings.objects.create(user=user, disabled_categories=['medications'])
        self._timed_event(user, pet)
        with patch('django.utils.timezone.now', return_value=_at(date.today(), 9, 0)):
            send_event_notifications()
        assert Notification.objects.count() == 0

    def test_multi_slot_time_in_push_is_utc(self, user, pet):
        # UTC+3, слоты 09:00 и 18:00 локального времени
        rule = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.DAILY, times=['09:00', '18:00'],
        )
        self._timed_event(
            user, pet, is_recurring=True, recurrence=rule, timezone_offset=180, time=time(9, 0),
        )
        FCMDevice.objects.create(user=user, fcm_token='tok')
        # 06:00 UTC = 09:00 по месту
        with patch('django.utils.timezone.now', return_value=_at(date.today(), 6, 0)):
            send_event_notifications()
        assert self.firebase.send_push_notification.call_args.kwargs['data']['time'] == '06:00'
        assert Notification.objects.get().occurrence_time == time(9, 0)


@pytest.mark.django_db
class TestNotificationLifecycle:
    def test_deleted_with_pet_event_and_user(self, user, pet):
        event = Event.objects.create(user=user, pet=pet, title='X', start_date=date.today())
        make_notification(user, pet=pet, event=event)
        make_notification(user, pet=pet)
        make_notification(user, title='Объявление', kind=NotificationKind.ANNOUNCEMENT)

        event.delete()
        assert Notification.objects.count() == 2
        pet.delete()
        assert Notification.objects.count() == 1
        assert Notification.objects.get().kind == NotificationKind.ANNOUNCEMENT
        user.delete()
        assert Notification.objects.count() == 0

    def test_cleanup_removes_only_old(self, user, settings):
        settings.NOTIFICATION_RETENTION_DAYS = 180
        old = make_notification(user, offset_minutes=181 * 24 * 60)
        fresh = make_notification(user, offset_minutes=179 * 24 * 60)
        cleanup_old_notifications()
        assert not Notification.objects.filter(pk=old.pk).exists()
        assert Notification.objects.filter(pk=fresh.pk).exists()

    def test_announcement_serializes_without_pet_and_event(self, auth_client, user):
        make_notification(user, kind=NotificationKind.ANNOUNCEMENT, title='Новое в Хвостиках')
        item = auth_client.get(LIST_URL).json()['results'][0]
        assert item['kind'] == 'announcement'
        assert item['pet_id'] is None and item['event_id'] is None
        assert item['occurrence_time'] is None
