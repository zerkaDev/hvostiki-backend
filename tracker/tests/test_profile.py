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

    def test_profile_contains_name_and_avatar_fields(self, auth_client, user):
        response = auth_client.get(reverse('profile'))
        assert response.status_code == 200
        assert response.data['name'] == ''
        assert response.data['avatar'] is None

    def test_put_not_allowed(self, auth_client):
        response = auth_client.put(reverse('profile'), {'name': 'Анна'})
        assert response.status_code == 405

    def test_phone_number_cannot_be_changed(self, auth_client, user):
        old_phone = user.phone_number
        response = auth_client.patch(reverse('profile'), {'phone_number': '79998887766'})

        assert response.status_code == 200
        assert response.data['phone_number'] == old_phone
        user.refresh_from_db()
        assert user.phone_number == old_phone

    def test_update_name(self, auth_client, user):
        response = auth_client.patch(reverse('profile'), {'name': '  Анна  '}, format='json')

        assert response.status_code == 200
        assert response.data['name'] == 'Анна'
        user.refresh_from_db()
        assert user.name == 'Анна'

    def test_clear_name(self, auth_client, user):
        user.name = 'Анна'
        user.save()
        response = auth_client.patch(reverse('profile'), {'name': ''}, format='json')

        assert response.status_code == 200
        assert response.data['name'] == ''

    def test_name_too_long(self, auth_client):
        response = auth_client.patch(reverse('profile'), {'name': 'я' * 51}, format='json')
        assert response.status_code == 400
        assert 'name' in response.data


    def test_profile_unauthorized(self, api_client):
        url = reverse('profile')
        response = api_client.get(url)
        assert response.status_code == 401
