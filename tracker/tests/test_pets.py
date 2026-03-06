import pytest
from django.urls import reverse
from tracker.models import Pet, Breed, PetType

@pytest.mark.django_db
class TestPets:
    def test_list_pets(self, auth_client, pet):
        url = reverse('pet-list')
        response = auth_client.get(url)
        assert response.status_code == 200
        assert len(response.data) == 1
        assert response.data[0]['name'] == pet.name

    def test_create_pet(self, auth_client, breed):
        url = reverse('pet-list')
        data = {
            'name': 'Max',
            'pet_type': PetType.DOG,
            'breed': breed.id,
            'weight': 10.5,
            'color': 'Black',
            'gender': 'M',
            'has_castration': True
        }
        response = auth_client.post(url, data)
        assert response.status_code == 201
        assert Pet.objects.filter(name='Max').exists()

    def test_retrieve_pet(self, auth_client, pet):
        url = reverse('pet-detail', kwargs={'pk': pet.id})
        response = auth_client.get(url)
        assert response.status_code == 200
        assert response.data['name'] == pet.name

    def test_update_pet(self, auth_client, pet):
        url = reverse('pet-detail', kwargs={'pk': pet.id})
        data = {'name': 'Buddy Updated', 'weight': 26.0}
        response = auth_client.patch(url, data)
        assert response.status_code == 200
        pet.refresh_from_db()
        assert pet.name == 'Buddy Updated'

    def test_delete_pet(self, auth_client, pet):
        url = reverse('pet-detail', kwargs={'pk': pet.id})
        response = auth_client.delete(url)
        assert response.status_code == 204
        assert not Pet.objects.filter(id=pet.id).exists()

    def test_list_breeds(self, auth_client, breed):
        url = reverse('breed-list')
        response = auth_client.get(f"{url}?type={PetType.DOG}")
        assert response.status_code == 200
        assert len(response.data) == 1
        assert response.data[0]['name'] == breed.name

    def test_list_breeds_no_type(self, auth_client):
        url = reverse('breed-list')
        response = auth_client.get(url)
        assert response.status_code == 400
