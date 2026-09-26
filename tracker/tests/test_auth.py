import pytest
from django.urls import reverse
from unittest.mock import patch
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken
from tracker.models import User

@pytest.mark.django_db
class TestAuth:
    def test_send_code_success(self, api_client):
        url = reverse('send_code')
        data = {'phone_number': '79001112233'}
        
        with patch('tracker.tasks.send_confirmation_code.delay') as mock_send:
            response = api_client.post(url, data)
            
        assert response.status_code == 200
        assert response.data['detail'] == 'Код подтверждения отправлен'
        assert User.objects.filter(phone_number='79001112233').exists()
        user = User.objects.get(phone_number='79001112233')
        assert user.confirmation_code != ''

    def test_send_code_invalid_phone(self, api_client):
        url = reverse('send_code')
        data = {'phone_number': '123'}
        response = api_client.post(url, data)
        assert response.status_code == 400

    def test_verify_code_success(self, api_client, user):
        from django.utils import timezone
        user.confirmation_code = '1234'
        user.code_sent_at = timezone.now()
        user.save()
    
        url = reverse('verify_code')
        data = {'phone_number': user.phone_number, 'code': '1234'}
        response = api_client.post(url, data)
    
        assert response.status_code == 200
        assert 'access' in response.data
        assert 'refresh' in response.data
        
        user.refresh_from_db()
        assert user.is_verified is True

    def test_verify_code_invalid(self, api_client, user):
        from django.utils import timezone
        user.confirmation_code = '1234'
        user.code_sent_at = timezone.now()
        user.save()
    
        url = reverse('verify_code')
        data = {'phone_number': user.phone_number, 'code': '0000'}
        response = api_client.post(url, data)
        
        assert response.status_code == 400
        assert 'code' in response.data

    def test_refresh_token_success(self, api_client, user):
        refresh = RefreshToken.for_user(user)

        response = api_client.post(
            reverse('token-refresh'),
            {'refresh': str(refresh)},
            format='json',
        )

        assert response.status_code == 200
        assert response.data['refresh'] == str(refresh)
        assert response.data['refresh_expires'] == refresh.payload['exp']

        access = AccessToken(response.data['access'])
        assert access['user_id'] == str(user.id)
        assert response.data['access_expires'] == access.payload['exp']

    def test_refresh_token_rejects_invalid_token(self, api_client):
        response = api_client.post(
            reverse('token-refresh'),
            {'refresh': 'not-a-jwt'},
            format='json',
        )

        assert response.status_code == 401

    def test_refresh_token_requires_token(self, api_client):
        response = api_client.post(reverse('token-refresh'), {}, format='json')

        assert response.status_code == 400

    def test_logout_revokes_submitted_refresh_token(self, auth_client, user):
        refresh = RefreshToken.for_user(user)
        url = reverse('logout')

        response = auth_client.post(
            url,
            {'refresh': str(refresh)},
            format='json',
        )

        assert response.status_code == 200
        assert response.data['detail'] == 'Выход выполнен успешно'

        refresh_response = auth_client.post(
            reverse('token-refresh'),
            {'refresh': str(refresh)},
            format='json',
        )
        assert refresh_response.status_code == 401

    def test_logout_requires_refresh_token(self, auth_client):
        response = auth_client.post(reverse('logout'), {}, format='json')

        assert response.status_code == 400

    def test_logout_is_idempotent(self, auth_client, user):
        refresh = RefreshToken.for_user(user)
        url = reverse('logout')

        first = auth_client.post(url, {'refresh': str(refresh)}, format='json')
        second = auth_client.post(url, {'refresh': str(refresh)}, format='json')

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.data['detail'] == 'Выход выполнен успешно'

    def test_logout_rejects_foreign_refresh_token(self, auth_client, api_client, user):
        other_user = User.objects.create_user(phone_number='79005556677')
        foreign_refresh = RefreshToken.for_user(other_user)

        response = auth_client.post(
            reverse('logout'),
            {'refresh': str(foreign_refresh)},
            format='json',
        )

        assert response.status_code == 403

        # Чужой токен не должен быть отозван
        api_client.force_authenticate(user=None)
        still_valid = api_client.post(
            reverse('token-refresh'),
            {'refresh': str(foreign_refresh)},
            format='json',
        )
        assert still_valid.status_code == 200

    def test_logout_with_invalid_token_returns_401(self, auth_client):
        response = auth_client.post(
            reverse('logout'),
            {'refresh': 'not-a-jwt'},
            format='json',
        )

        assert response.status_code == 401
