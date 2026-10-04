from datetime import date, datetime, time
from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse
from django.utils import timezone

from tracker.models import (
    Event, EventNotificationLog, FCMDevice, RecurrenceFrequency, RecurrenceRule, User,
)
from tracker.services import firebase_service as firebase_module
from tracker.services.firebase_service import FirebaseService
from tracker.tasks import send_event_notifications

REGISTER = reverse('register_device')
UNREGISTER = reverse('unregister_device')


@pytest.mark.django_db
class TestRegisterDevice:
    def test_registers_token_with_platform(self, auth_client, user):
        response = auth_client.post(REGISTER, {'fcm_token': 'tok', 'platform': 'ios'}, format='json')

        assert response.status_code == 200
        device = FCMDevice.objects.get(fcm_token='tok')
        assert device.user == user
        assert device.platform == 'ios'

    def test_platform_is_optional(self, auth_client):
        response = auth_client.post(REGISTER, {'fcm_token': 'tok'}, format='json')

        assert response.status_code == 200
        assert FCMDevice.objects.get(fcm_token='tok').platform == ''

    def test_missing_platform_keeps_stored_value(self, auth_client, user):
        FCMDevice.objects.create(user=user, fcm_token='tok', platform='android')

        auth_client.post(REGISTER, {'fcm_token': 'tok'}, format='json')

        assert FCMDevice.objects.get(fcm_token='tok').platform == 'android'

    def test_rejects_unknown_platform(self, auth_client):
        response = auth_client.post(REGISTER, {'fcm_token': 'tok', 'platform': 'web'}, format='json')

        assert response.status_code == 400

    def test_token_moves_to_current_user(self, auth_client, user):
        other = User.objects.create_user(phone_number='79005550000')
        FCMDevice.objects.create(user=other, fcm_token='tok')

        auth_client.post(REGISTER, {'fcm_token': 'tok'}, format='json')

        assert FCMDevice.objects.get(fcm_token='tok').user == user
        assert FCMDevice.objects.count() == 1

    def test_requires_authentication(self, api_client):
        assert api_client.post(REGISTER, {'fcm_token': 'tok'}, format='json').status_code == 401


@pytest.mark.django_db
class TestUnregisterDevice:
    def test_removes_own_token(self, auth_client, user):
        FCMDevice.objects.create(user=user, fcm_token='tok')

        response = auth_client.post(UNREGISTER, {'fcm_token': 'tok'}, format='json')

        assert response.status_code == 200
        assert not FCMDevice.objects.exists()

    def test_is_idempotent(self, auth_client):
        assert auth_client.post(UNREGISTER, {'fcm_token': 'nope'}, format='json').status_code == 200

    def test_does_not_remove_foreign_token(self, auth_client):
        other = User.objects.create_user(phone_number='79005550000')
        FCMDevice.objects.create(user=other, fcm_token='tok')

        response = auth_client.post(UNREGISTER, {'fcm_token': 'tok'}, format='json')

        assert response.status_code == 200
        assert FCMDevice.objects.filter(fcm_token='tok').exists()

    def test_requires_token(self, auth_client):
        assert auth_client.post(UNREGISTER, {}, format='json').status_code == 400

    def test_requires_authentication(self, api_client):
        assert api_client.post(UNREGISTER, {'fcm_token': 'tok'}, format='json').status_code == 401


class _FakeUnregistered(Exception):
    pass


class _FakeSenderMismatch(Exception):
    pass


@pytest.fixture
def messaging(monkeypatch):
    """Подменяет firebase_admin.messaging и помечает сервис инициализированным."""
    fake = MagicMock()
    fake.UnregisteredError = _FakeUnregistered
    fake.SenderIdMismatchError = _FakeSenderMismatch
    monkeypatch.setattr(firebase_module, 'messaging', fake, raising=False)
    monkeypatch.setattr(firebase_module, 'HAS_FIREBASE', True)
    service = FirebaseService()
    monkeypatch.setattr(service, '_initialized', True)
    return fake


@pytest.mark.django_db
class TestFirebaseService:
    def test_sends_message_with_string_data_and_priorities(self, messaging):
        messaging.send.return_value = 'projects/x/messages/1'

        result = FirebaseService().send_push_notification('tok', 'T', 'B', {'event_id': 5})

        assert result == 'projects/x/messages/1'
        kwargs = messaging.Message.call_args.kwargs
        assert kwargs['token'] == 'tok'
        assert kwargs['data'] == {'event_id': '5'}
        messaging.AndroidConfig.assert_called_once_with(priority='high')
        assert messaging.APNSConfig.call_args.kwargs['headers'] == {'apns-priority': '10'}
        messaging.Aps.assert_called_once_with(sound='default')

    @pytest.mark.parametrize('error', [_FakeUnregistered, _FakeSenderMismatch])
    def test_invalid_token_is_removed(self, messaging, user, error):
        FCMDevice.objects.create(user=user, fcm_token='dead')
        FCMDevice.objects.create(user=user, fcm_token='alive')
        messaging.send.side_effect = error('gone')

        assert FirebaseService().send_push_notification('dead', 'T', 'B') is None

        assert list(FCMDevice.objects.values_list('fcm_token', flat=True)) == ['alive']

    def test_transient_error_is_raised(self, messaging, user):
        FCMDevice.objects.create(user=user, fcm_token='tok')
        messaging.send.side_effect = RuntimeError('unavailable')

        with pytest.raises(RuntimeError):
            FirebaseService().send_push_notification('tok', 'T', 'B')

        assert FCMDevice.objects.filter(fcm_token='tok').exists()

    def test_not_initialized_does_nothing(self, monkeypatch):
        service = FirebaseService()
        monkeypatch.setattr(service, '_initialized', False)

        assert service.send_push_notification('tok', 'T', 'B') is None


@pytest.mark.django_db
class TestPushDelivery:
    @pytest.fixture
    def event(self, user, pet):
        rule = RecurrenceRule.objects.create(frequency=RecurrenceFrequency.DAILY)
        return Event.objects.create(
            user=user, pet=pet, title='Корм', start_date=date.today(), time=None,
            is_recurring=True, recurrence=rule, timezone_offset=0,
        )

    def _run(self):
        now = timezone.make_aware(datetime.combine(date.today(), time(8, 0)))
        with patch('django.utils.timezone.now', return_value=now):
            send_event_notifications()

    def test_payload_contains_event_type_and_date(self, user, event):
        FCMDevice.objects.create(user=user, fcm_token='tok')

        with patch('tracker.tasks.firebase_service.send_push_notification') as send:
            self._run()

        data = send.call_args.kwargs['data']
        assert data['event_id'] == str(event.id)
        assert data['type'] == 'final'
        assert data['date'] == date.today().isoformat()

    def test_one_failing_device_does_not_block_others(self, user, event):
        FCMDevice.objects.create(user=user, fcm_token='bad')
        FCMDevice.objects.create(user=user, fcm_token='good')
        sent = []

        def fake_send(**kwargs):
            if kwargs['token'] == 'bad':
                raise RuntimeError('boom')
            sent.append(kwargs['token'])

        with patch('tracker.tasks.firebase_service.send_push_notification', side_effect=fake_send):
            self._run()

        assert sent == ['good']
        # хотя бы одно устройство получило пуш — повторять отправку нельзя
        assert EventNotificationLog.objects.filter(event=event).exists()

    def test_log_is_released_when_no_device_received_push(self, user, event):
        FCMDevice.objects.create(user=user, fcm_token='bad')

        with patch('tracker.tasks.firebase_service.send_push_notification',
                   side_effect=RuntimeError('boom')):
            self._run()

        assert not EventNotificationLog.objects.filter(event=event).exists()
