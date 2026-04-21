import pytest
from django.urls import reverse
from tracker.models import User

@pytest.mark.django_db
class TestProfile:
    def test_get_profile(self, auth_client, user):
        url = reverse('profile')
        response = auth_client.get(url)
        
        assert response.status_code == 200
        assert response.data['phone_number'] == user.phone_number
        assert response.data['id'] == str(user.id)

    def test_update_profile_put(self, auth_client, user):
        url = reverse('profile')
        new_phone = '79998887766'
        data = {
            'phone_number': new_phone
        }
        response = auth_client.put(url, data)
        
        assert response.status_code == 200
        assert response.data['phone_number'] == new_phone
        
        user.refresh_from_db()
        assert user.phone_number == new_phone

    def test_update_profile_patch(self, auth_client, user):
        url = reverse('profile')
        new_phone = '79995554433'
        data = {
            'phone_number': new_phone
        }
        response = auth_client.patch(url, data)
        
        assert response.status_code == 200
        assert response.data['phone_number'] == new_phone
        
        user.refresh_from_db()
        assert user.phone_number == new_phone

    def test_delete_profile(self, auth_client, user):
        url = reverse('profile')
        response = auth_client.delete(url)
        
        assert response.status_code == 204
        assert not User.objects.filter(id=user.id).exists()

    def test_profile_unauthorized(self, api_client):
        url = reverse('profile')
        response = api_client.get(url)
        assert response.status_code == 401
