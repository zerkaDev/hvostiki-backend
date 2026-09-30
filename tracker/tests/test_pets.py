import datetime

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from tracker.models import (
    Breed, Event, EventCompletion, Pet, PetType,
    RecurrenceFrequency, RecurrenceRule, User,
)

MIXED_BREED = 'Метис или не знаю'

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
        names = [item['name'] for item in response.data]
        assert breed.name in names
        # Возвращаются только породы запрошенного типа
        assert all(item['type'] == PetType.DOG for item in response.data)

    def test_list_breeds_returns_mixed_breed(self, auth_client, breed):
        for pet_type in (PetType.DOG, PetType.CAT):
            response = auth_client.get(f"{reverse('breed-list')}?type={pet_type}")

            assert response.status_code == 200
            assert MIXED_BREED in [item['name'] for item in response.data]

    def test_list_breeds_no_type(self, auth_client):
        url = reverse('breed-list')
        response = auth_client.get(url)
        assert response.status_code == 400

    def test_list_pets_returns_numeric_weight(self, auth_client, pet):
        response = auth_client.get(reverse('pet-list'))

        assert response.status_code == 200
        weight = response.data[0]['weight']
        # Вес должен быть числом, а не строкой "25.50"
        assert not isinstance(weight, str)
        assert float(weight) == float(pet.weight)

    def test_retrieve_pet_returns_numeric_weight(self, auth_client, pet):
        response = auth_client.get(reverse('pet-detail', kwargs={'pk': pet.id}))

        assert response.status_code == 200
        assert not isinstance(response.data['weight'], str)
        assert float(response.data['weight']) == float(pet.weight)


@pytest.mark.django_db
class TestFillBreedsCommand:
    def test_adds_mixed_breed(self):
        call_command('fill_breeds')

        assert Breed.objects.filter(name=MIXED_BREED, type=PetType.DOG).exists()
        assert Breed.objects.filter(name=MIXED_BREED, type=PetType.CAT).exists()

    def test_is_idempotent(self):
        call_command('fill_breeds')
        breeds_count = Breed.objects.count()

        call_command('fill_breeds')

        assert Breed.objects.count() == breeds_count
        assert Breed.objects.filter(name=MIXED_BREED).count() == 2


@pytest.mark.django_db
class TestPetUpcoming:
    """GET /pets/{id}/upcoming/ — ближайшие события конкретного питомца."""

    def _event(self, user, pet, title, start_date, recurrence=None, time=None):
        return Event.objects.create(
            user=user,
            pet=pet,
            title=title,
            start_date=start_date,
            time=time,
            is_recurring=recurrence is not None,
            recurrence=recurrence,
            timezone_offset=0,
        )

    def _url(self, pet):
        return reverse('pet-upcoming', kwargs={'pk': pet.id})

    def test_returns_events_grouped_by_date(self, auth_client, user, pet):
        today = timezone.localdate()
        in_three_days = today + datetime.timedelta(days=3)

        self._event(user, pet, 'Сегодня', today, time=datetime.time(8, 0))
        self._event(user, pet, 'Через три дня', in_three_days, time=datetime.time(10, 0))

        response = auth_client.get(self._url(pet))

        assert response.status_code == 200
        assert str(today) in response.data
        assert str(in_three_days) in response.data
        assert response.data[str(today)][0]['title'] == 'Сегодня'

    def test_default_window_is_two_weeks(self, auth_client, user, pet):
        today = timezone.localdate()

        self._event(user, pet, 'На 13-й день', today + datetime.timedelta(days=13))
        self._event(user, pet, 'На 20-й день', today + datetime.timedelta(days=20))

        response = auth_client.get(self._url(pet))

        assert response.status_code == 200
        assert str(today + datetime.timedelta(days=13)) in response.data
        assert str(today + datetime.timedelta(days=20)) not in response.data

    def test_days_parameter_narrows_window(self, auth_client, user, pet):
        today = timezone.localdate()

        self._event(user, pet, 'Завтра', today + datetime.timedelta(days=1))
        self._event(user, pet, 'Через пять дней', today + datetime.timedelta(days=5))

        response = auth_client.get(f'{self._url(pet)}?days=2')

        assert response.status_code == 200
        assert str(today + datetime.timedelta(days=1)) in response.data
        assert str(today + datetime.timedelta(days=5)) not in response.data

    def test_date_from_parameter_shifts_window(self, auth_client, user, pet):
        today = timezone.localdate()
        start = today + datetime.timedelta(days=10)

        self._event(user, pet, 'Сегодня', today)
        self._event(user, pet, 'Через 12 дней', start + datetime.timedelta(days=2))

        response = auth_client.get(f'{self._url(pet)}?date_from={start}&days=7')

        assert response.status_code == 200
        assert str(today) not in response.data
        assert str(start + datetime.timedelta(days=2)) in response.data

    def test_returns_only_requested_pet_events(self, auth_client, user, pet):
        today = timezone.localdate()
        other_pet = Pet.objects.create(
            owner=user, name='Другой', pet_type=PetType.CAT,
            breed=Breed.objects.create(name='Сибирская', type=PetType.CAT),
            weight=4, color='Серый',
        )

        self._event(user, pet, 'Мой питомец', today)
        self._event(user, other_pet, 'Другой питомец', today)

        response = auth_client.get(self._url(pet))

        assert response.status_code == 200
        titles = [item['title'] for items in response.data.values() for item in items]
        assert titles == ['Мой питомец']

    def test_foreign_pet_returns_404(self, auth_client, pet):
        stranger = User.objects.create_user(phone_number='79007778899')
        stranger_pet = Pet.objects.create(
            owner=stranger, name='Чужой', pet_type=PetType.DOG,
            breed=Breed.objects.create(name='Такса', type=PetType.DOG),
            weight=9, color='Рыжий',
        )

        response = auth_client.get(self._url(stranger_pet))

        assert response.status_code == 404

    def test_recurring_event_returns_all_occurrences(self, auth_client, user, pet):
        today = timezone.localdate()
        rule = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.DAILY, interval=1
        )
        self._event(user, pet, 'Ежедневно', today, recurrence=rule)

        response = auth_client.get(f'{self._url(pet)}?days=3')

        assert response.status_code == 200
        assert len(response.data) == 4  # сегодня + 3 дня
        for day, items in response.data.items():
            assert items[0]['start_date'] == day

    def test_done_flag_is_per_date(self, auth_client, user, pet):
        today = timezone.localdate()
        rule = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.DAILY, interval=1
        )
        event = self._event(user, pet, 'Ежедневно', today, recurrence=rule)
        EventCompletion.objects.create(event=event, occurrence_date=today)

        response = auth_client.get(f'{self._url(pet)}?days=1')

        assert response.status_code == 200
        assert response.data[str(today)][0]['done'] is True
        assert response.data[str(today + datetime.timedelta(days=1))][0]['done'] is False

    @pytest.mark.parametrize('days', ['0', '61', 'abc'])
    def test_invalid_days_returns_400(self, auth_client, pet, days):
        response = auth_client.get(f'{self._url(pet)}?days={days}')

        assert response.status_code == 400

    def test_requires_authentication(self, api_client, pet):
        response = api_client.get(self._url(pet))

        assert response.status_code == 401
