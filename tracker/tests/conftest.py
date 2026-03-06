import pytest
from rest_framework.test import APIClient
from tracker.models import User, Pet, Breed, PetType

@pytest.fixture(autouse=True)
def clear_cache():
    from django.core.cache import cache
    cache.clear()

@pytest.fixture
def api_client():
    return APIClient()

@pytest.fixture
def user(db):
    return User.objects.create_user(phone_number='79001112233')

@pytest.fixture
def auth_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client

@pytest.fixture
def breed(db):
    return Breed.objects.create(name='Golden Retriever', type=PetType.DOG)

@pytest.fixture
def pet(db, user, breed):
    return Pet.objects.create(
        owner=user,
        name='Buddy',
        pet_type=PetType.DOG,
        breed=breed,
        weight=25.5,
        color='Golden'
    )

@pytest.fixture
def event(db, user, pet):
    from tracker.models import Event, EventTypeChoices
    import datetime
    return Event.objects.create(
        user=user,
        pet=pet,
        title='Walk Buddy',
        start_date=datetime.date.today(),
        time=datetime.time(10, 0),
        type=EventTypeChoices.WALKING
    )
