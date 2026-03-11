import pytest
from django.urls import reverse
import datetime
from tracker.models import Event, EventTypeChoices, EventCompletion, RecurrenceRule, RecurrenceFrequency
import tracker.models

@pytest.mark.django_db
class TestEvents:
    def test_create_event_non_recurring(self, auth_client, pet):
        url = reverse('event_schedule-list')
        data = {
            'pet': str(pet.id),
            'title': 'Vet visit',
            'start_date': str(datetime.date.today()),
            'time': '12:00:00',
            'timezone_offset': 180,
            'is_recurring': False,
            'type': EventTypeChoices.CUSTOM
        }
        response = auth_client.post(url, data, format='json')
        assert response.status_code == 201
        assert Event.objects.filter(title='Vet visit').exists()

    def test_create_event_recurring(self, auth_client, pet):
        url = reverse('event_schedule-list')
        data = {
            'pet': str(pet.id),
            'title': 'Daily Feed',
            'start_date': str(datetime.date.today()),
            'time': '08:00:00',
            'timezone_offset': 180,
            'is_recurring': True,
            'recurrence': {
                'frequency': 'daily',
                'interval': 1
            },
            'type': EventTypeChoices.FEEDING
        }
        response = auth_client.post(url, data, format='json')
        assert response.status_code == 201
        event = Event.objects.get(title='Daily Feed')
        assert event.is_recurring is True
        assert event.recurrence.frequency == 'daily'

    def test_get_period_events(self, auth_client, event):
        url = reverse('event_schedule-period')
        date_from = event.start_date - datetime.timedelta(days=1)
        date_to = event.start_date + datetime.timedelta(days=1)
        
        response = auth_client.get(f"{url}?date_from={date_from}&date_to={date_to}")
        assert response.status_code == 200
        assert str(event.start_date) in response.data
        assert len(response.data[str(event.start_date)]) == 1

    def test_mark_done(self, auth_client, event):
        url = reverse('event_schedule-mark-done', kwargs={'pk': event.id})
        data = {'date': str(event.start_date)}
        response = auth_client.post(url, data)
        assert response.status_code == 200
        assert response.data['done'] is True
        assert EventCompletion.objects.filter(event=event, occurrence_date=event.start_date).exists()

    def test_mark_undone(self, auth_client, event):
        # Сначала помечаем как выполненное
        EventCompletion.objects.create(event=event, occurrence_date=event.start_date)
        
        url = reverse('event_schedule-mark-undone', kwargs={'pk': event.id})
        data = {'date': str(event.start_date)}
        response = auth_client.post(url, data)
        
        assert response.status_code == 200
        assert response.data['done'] is False
        assert not EventCompletion.objects.filter(event=event, occurrence_date=event.start_date).exists()

    def test_period_events_sorting_and_grouping(self, auth_client, user, pet):
        """Тест группировки по датам и сортировки по времени внутри периода"""
        url = reverse('event_schedule-period')
        today = datetime.date.today()
        tomorrow = today + datetime.timedelta(days=1)
        
        # Создаем события для "сегодня" вразнобой по времени
        times = [
            datetime.time(15, 0),
            datetime.time(8, 0),
            datetime.time(12, 0),
        ]
        for t in times:
            Event.objects.create(
                user=user, pet=pet, title=f"Event at {t}",
                start_date=today, time=t, type=EventTypeChoices.CUSTOM,
                timezone_offset=0
            )
            
        # Создаем событие на завтра
        Event.objects.create(
            user=user, pet=pet, title="Tomorrow event",
            start_date=tomorrow, time=datetime.time(10, 0), 
            type=EventTypeChoices.CUSTOM, timezone_offset=0
        )
        
        # Создаем ежедневное повторяющееся событие
        Event.objects.create(
            user=user, pet=pet, title="Daily morning feed",
            start_date=today, time=datetime.time(7, 0),
            is_recurring=True,
            recurrence=tracker.models.RecurrenceRule.objects.create(
                frequency=tracker.models.RecurrenceFrequency.DAILY, interval=1
            ),
            type=EventTypeChoices.FEEDING, timezone_offset=0
        )

        response = auth_client.get(f"{url}?date_from={today}&date_to={tomorrow}")
        
        assert response.status_code == 200
        
        # Проверяем наличие обеих дат в ключе
        assert str(today) in response.data
        assert str(tomorrow) in response.data
        
        # Проверяем сортировку для "сегодня"
        # Ожидаемый порядок по времени: 07:00 (daily), 08:00, 12:00, 15:00
        today_events = response.data[str(today)]
        assert len(today_events) == 4
        assert "07:00" in today_events[0]['time']
        assert "08:00" in today_events[1]['time']
        assert "12:00" in today_events[2]['time']
        assert "15:00" in today_events[3]['time']
        
        # Проверяем завтрашний день
        # Там должно быть 2 события: разовое на завтра и вхождение ежедневного
        tomorrow_events = response.data[str(tomorrow)]
        assert len(tomorrow_events) == 2
        # 07:00 (daily) должно быть первым, 10:00 (разовое) вторым
        assert "07:00" in tomorrow_events[0]['time']
        assert "10:00" in tomorrow_events[1]['time']
