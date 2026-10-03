import io
import os
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import RefreshToken

from tracker.models import FCMDevice, Pet, User

SEND = 'tracker.services.account_deletion.send_confirmation_code'


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)


def stored_code(user):
    return cache.get(f'account_delete_code_{user.pk}')


def image_file():
    buffer = io.BytesIO()
    Image.new('RGB', (20, 20), (1, 2, 3)).save(buffer, format='PNG')
    return SimpleUploadedFile('a.png', buffer.getvalue(), content_type='image/png')


@pytest.mark.django_db
class TestSendDeletionCode:
    def test_sends_call_code_without_touching_login_code(self, auth_client, user):
        user.confirmation_code = '1111'
        user.save()
        with patch(SEND) as send:
            response = auth_client.post(reverse('profile-delete-send-code'))

        assert response.status_code == 200
        assert response.data['resend_timeout'] == 60
        code = stored_code(user)
        assert len(code) == 4
        send.delay.assert_called_once_with(user.phone_number, code)
        user.refresh_from_db()
        assert user.confirmation_code == '1111'

    def test_repeat_within_minute_is_429(self, auth_client):
        with patch(SEND):
            auth_client.post(reverse('profile-delete-send-code'))
            response = auth_client.post(reverse('profile-delete-send-code'))
        assert response.status_code == 429
        assert 'detail' in response.data

    def test_code_is_not_logged(self, auth_client, user, caplog):
        caplog.set_level('DEBUG')
        with patch(SEND):
            auth_client.post(reverse('profile-delete-send-code'))
        assert stored_code(user) not in caplog.text

    def test_requires_auth(self, api_client):
        assert api_client.post(reverse('profile-delete-send-code')).status_code == 401


@pytest.mark.django_db
class TestDeleteAccount:
    def request_code(self, client, user):
        with patch(SEND):
            client.post(reverse('profile-delete-send-code'))
        return stored_code(user)

    def test_without_requested_code_is_400(self, auth_client, user):
        response = auth_client.post(reverse('profile-delete'), {'code': '1234'})
        assert response.status_code == 400
        assert User.objects.filter(pk=user.pk).exists()

    def test_missing_code_field_is_400(self, auth_client):
        assert auth_client.post(reverse('profile-delete'), {}).status_code == 400

    def test_wrong_code_decrements_attempts(self, auth_client, user):
        code = self.request_code(auth_client, user)
        wrong = '0000' if code != '0000' else '1111'
        response = auth_client.post(reverse('profile-delete'), {'code': wrong})
        assert response.status_code == 400
        assert 'Осталось попыток: 4' in response.data['detail']
        assert User.objects.filter(pk=user.pk).exists()

    def test_attempts_exhausted_invalidates_code(self, auth_client, user):
        code = self.request_code(auth_client, user)
        wrong = '0000' if code != '0000' else '1111'
        for _ in range(5):
            response = auth_client.post(reverse('profile-delete'), {'code': wrong})
        assert 'Превышено' in response.data['detail']
        # даже верный код после исчерпания попыток не работает
        response = auth_client.post(reverse('profile-delete'), {'code': code})
        assert response.status_code == 400
        assert User.objects.filter(pk=user.pk).exists()

    def test_login_code_does_not_work_for_deletion(self, auth_client, user):
        user.confirmation_code = '5555'
        user.save()
        code = self.request_code(auth_client, user)
        wrong = '5555' if code != '5555' else '1111'
        assert auth_client.post(reverse('profile-delete'), {'code': wrong}).status_code == 400

    def test_success_deletes_everything(
        self, auth_client, user, pet, django_capture_on_commit_callbacks
    ):
        auth_client.patch(reverse('profile'), {'avatar': image_file()}, format='multipart')
        user.refresh_from_db()
        avatar_path = user.avatar.path
        pet.image = image_file()
        pet.save()
        pet_path = pet.image.path
        FCMDevice.objects.create(user=user, fcm_token='tok')
        refresh = RefreshToken.for_user(user)
        code = self.request_code(auth_client, user)

        with django_capture_on_commit_callbacks(execute=True):
            response = auth_client.post(reverse('profile-delete'), {'code': code})

        assert response.status_code == 204
        assert not User.objects.filter(pk=user.pk).exists()
        assert not Pet.objects.filter(pk=pet.pk).exists()
        assert not FCMDevice.objects.filter(fcm_token='tok').exists()
        assert not os.path.exists(avatar_path)
        assert not os.path.exists(pet_path)
        assert BlacklistedToken.objects.filter(token__jti=refresh['jti']).exists()
        assert stored_code(user) is None

    def test_code_cannot_be_reused(self, auth_client, user):
        code = self.request_code(auth_client, user)
        auth_client.post(reverse('profile-delete'), {'code': code})
        auth_client.force_authenticate(user=None)
        assert auth_client.post(reverse('profile-delete'), {'code': code}).status_code == 401

    def test_old_delete_profile_is_gone(self, auth_client, user):
        assert auth_client.delete(reverse('profile')).status_code == 405
        assert User.objects.filter(pk=user.pk).exists()

    def test_other_users_data_untouched(self, auth_client, user, breed):
        other = User.objects.create_user(phone_number='79005554433')
        Pet.objects.create(owner=other, name='X', pet_type='dog', breed=breed, weight=1)
        code = self.request_code(auth_client, user)
        auth_client.post(reverse('profile-delete'), {'code': code})
        assert User.objects.filter(pk=other.pk).exists()
        assert Pet.objects.filter(owner=other).count() == 1
