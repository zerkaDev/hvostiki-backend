from datetime import date, datetime, time
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from tracker.models import (
    Event, EventNotificationLog, EventTypeChoices, FCMDevice, NotificationSettings,
)
from tracker.notification_categories import CATEGORIES, category_for_event_type
from tracker.tasks import send_event_notifications

ALL_ON = {c: True for c in CATEGORIES}


@pytest.mark.django_db
class TestNotificationSettingsApi:
    def test_defaults_all_enabled_without_creating_row(self, auth_client, user):
        response = auth_client.get(reverse('profile-notification-settings'))
        assert response.status_code == 200
        assert response.data == ALL_ON
        assert not NotificationSettings.objects.filter(user=user).exists()

    def test_profile_embeds_settings(self, auth_client):
        response = auth_client.get(reverse('profile'))
        assert response.data['notification_settings'] == ALL_ON

    def test_patch_single_toggle(self, auth_client):
        response = auth_client.patch(
            reverse('profile-notification-settings'), {'walks': False}, format='json'
        )
        assert response.status_code == 200
        assert response.data == {**ALL_ON, 'walks': False}
        assert auth_client.get(reverse('profile')).data['notification_settings']['walks'] is False

    def test_patch_re_enable(self, auth_client):
        url = reverse('profile-notification-settings')
        auth_client.patch(url, {'walks': False, 'feeding': False}, format='json')
        response = auth_client.patch(url, {'walks': True}, format='json')
        assert response.data == {**ALL_ON, 'feeding': False}

    def test_patch_unknown_key_ignored_and_invalid_value_rejected(self, auth_client):
        url = reverse('profile-notification-settings')
        assert auth_client.patch(url, {'unknown': False}, format='json').data == ALL_ON
        assert auth_client.patch(url, {'walks': 'maybe'}, format='json').status_code == 400

    def test_settings_are_per_user(self, auth_client, user, api_client):
        from tracker.models import User
        auth_client.patch(reverse('profile-notification-settings'), {'walks': False}, format='json')
        other = User.objects.create_user(phone_number='79005554433')
        api_client.force_authenticate(user=other)
        assert api_client.get(reverse('profile-notification-settings')).data == ALL_ON

    def test_requires_auth(self, api_client):
        assert api_client.get(reverse('profile-notification-settings')).status_code == 401


class TestCategoryMapping:
    @pytest.mark.parametrize('event_type,category', [
        ('walking', 'walks'),
        ('feeding', 'feeding'),
        ('dailyPills', 'medications'),
        ('weeklyPills', 'medications'),
        ('deworming', 'medications'),
        ('fleaTreatment', 'medications'),
        ('yearlyVaccination', 'vaccinations'),
        ('rabiesVaccination', 'vaccinations'),
        ('vetVisit', 'vet_visits'),
        ('grooming', None),
        ('bathing', None),
        ('nailTrimming', None),
        ('custom', None),
    ])
    def test_mapping(self, event_type, category):
        assert category_for_event_type(event_type) == category

    def test_every_event_type_is_covered_or_always_on(self):
        always_on = {'grooming', 'bathing', 'nailTrimming', 'custom'}
        for value in EventTypeChoices.values:
            assert (category_for_event_type(value) is None) == (value in always_on)


@pytest.mark.django_db
class TestVetVisitType:
    def test_vet_visit_event_can_be_created(self, auth_client, pet):
        response = auth_client.post(reverse('event_schedule-list'), {
            'pet': str(pet.id), 'is_recurring': False, 'type': 'vetVisit', 'title': 'Осмотр',
            'start_date': '2026-10-10', 'time': '12:00', 'timezone_offset': 180,
        }, format='json')
        assert response.status_code == 201, response.data
        assert response.data['type'] == 'vetVisit'


@pytest.mark.django_db
class TestNotificationFilter:
    def make_event(self, user, pet, event_type):
        return Event.objects.create(
            user=user, pet=pet, title='t', type=event_type,
            start_date=date.today(), time=time(10, 0), timezone_offset=0,
        )

    def run(self):
        now = timezone.make_aware(datetime.combine(date.today(), time(10, 0)))
        with patch('django.utils.timezone.now', return_value=now), \
                patch('tracker.tasks.firebase_service') as firebase:
            send_event_notifications()
        return firebase

    def test_enabled_category_sends(self, user, pet):
        FCMDevice.objects.create(user=user, fcm_token='t1')
        event = self.make_event(user, pet, 'walking')
        firebase = self.run()
        assert firebase.send_push_notification.call_count == 1
        assert EventNotificationLog.objects.filter(event=event).exists()

    def test_disabled_category_is_skipped_without_log(self, user, pet):
        FCMDevice.objects.create(user=user, fcm_token='t1')
        NotificationSettings.objects.create(user=user, disabled_categories=['walks'])
        event = self.make_event(user, pet, 'walking')
        firebase = self.run()
        assert firebase.send_push_notification.call_count == 0
        assert not EventNotificationLog.objects.filter(event=event).exists()

    def test_other_category_not_affected(self, user, pet):
        FCMDevice.objects.create(user=user, fcm_token='t1')
        NotificationSettings.objects.create(user=user, disabled_categories=['walks'])
        self.make_event(user, pet, 'feeding')
        assert self.run().send_push_notification.call_count == 1

    def test_always_on_type_ignores_settings(self, user, pet):
        FCMDevice.objects.create(user=user, fcm_token='t1')
        NotificationSettings.objects.create(user=user, disabled_categories=list(CATEGORIES))
        self.make_event(user, pet, 'grooming')
        assert self.run().send_push_notification.call_count == 1
