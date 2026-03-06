import pytest
from django.urls import reverse
from unittest.mock import patch
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

    def test_logout(self, auth_client):
        url = reverse('logout')
        response = auth_client.post(url)
        assert response.status_code == 200
        assert response.data['detail'] == 'Выход выполнен успешно'
