import pytest
from datetime import datetime, time, date, timedelta
from unittest.mock import patch
from django.utils import timezone
from tracker.models import Event, EventTypeChoices, RecurrenceRule, RecurrenceFrequency, EventNotificationLog, EventNotificationType
from tracker.tasks import send_event_notifications

@pytest.mark.django_db
class TestNotifications:
    def test_daily_event_no_time_sends_at_8am(self, user, pet):
        # Daily event, no time
        rule = RecurrenceRule.objects.create(frequency=RecurrenceFrequency.DAILY)
        event = Event.objects.create(
            user=user, pet=pet, title="Daily Feed",
            start_date=date.today(), time=None, 
            is_recurring=True, recurrence=rule,
            timezone_offset=0
        )
        
        # Mock time to 8:00 AM UTC
        mock_now = datetime.combine(date.today(), time(8, 0))
        with patch('django.utils.timezone.now', return_value=timezone.make_aware(mock_now)):
            send_event_notifications()
        
        assert EventNotificationLog.objects.filter(
            event=event, 
            notification_type=EventNotificationType.FINAL,
            occurrence_date=date.today()
        ).exists()

    def test_daily_event_no_time_does_not_send_at_9pm(self, user, pet):
        rule = RecurrenceRule.objects.create(frequency=RecurrenceFrequency.DAILY)
        event = Event.objects.create(
            user=user, pet=pet, title="Daily Feed",
            start_date=date.today(), time=None, 
            is_recurring=True, recurrence=rule,
            timezone_offset=0
        )
        
        # Mock time to 9:00 PM UTC
        mock_now = datetime.combine(date.today(), time(21, 0))
        with patch('django.utils.timezone.now', return_value=timezone.make_aware(mock_now)):
            send_event_notifications()
        
        assert not EventNotificationLog.objects.filter(event=event).exists()

    def test_non_recurring_event_no_time_sends_reminder_at_9pm_day_before(self, user, pet):
        tomorrow = date.today() + timedelta(days=1)
        event = Event.objects.create(
            user=user, pet=pet, title="One-off Task",
            start_date=tomorrow, time=None, 
            is_recurring=False,
            timezone_offset=0
        )
        
        # Mock time to 9:00 PM TODAY
        mock_now = datetime.combine(date.today(), time(21, 0))
        with patch('django.utils.timezone.now', return_value=timezone.make_aware(mock_now)):
            send_event_notifications()
        
        assert EventNotificationLog.objects.filter(
            event=event, 
            notification_type=EventNotificationType.REMINDER,
            occurrence_date=tomorrow
        ).exists()

    def test_non_recurring_event_no_time_sends_final_at_8am_day_of(self, user, pet):
        today = date.today()
        event = Event.objects.create(
            user=user, pet=pet, title="One-off Task",
            start_date=today, time=None, 
            is_recurring=False,
            timezone_offset=0
        )
        
        # Mock time to 8:00 AM TODAY
        mock_now = datetime.combine(today, time(8, 0))
        with patch('django.utils.timezone.now', return_value=timezone.make_aware(mock_now)):
            send_event_notifications()
        
        assert EventNotificationLog.objects.filter(
            event=event, 
            notification_type=EventNotificationType.FINAL,
            occurrence_date=today
        ).exists()

    def test_event_with_time_sends_standard_at_time(self, user, pet):
        event_time = time(15, 30)
        event = Event.objects.create(
            user=user, pet=pet, title="Timed Task",
            start_date=date.today(), time=event_time, 
            is_recurring=False,
            timezone_offset=0
        )
        
        # Mock time to 15:30 TODAY
        mock_now = datetime.combine(date.today(), event_time)
        with patch('django.utils.timezone.now', return_value=timezone.make_aware(mock_now)):
            send_event_notifications()
        
        assert EventNotificationLog.objects.filter(
            event=event, 
            notification_type=EventNotificationType.STANDARD,
            occurrence_date=date.today()
        ).exists()

    def test_allows_different_notification_types_for_same_date(self, user, pet):
        today = date.today()
        event = Event.objects.create(
            user=user, pet=pet, title="Multi-notify Task",
            start_date=today, time=None, 
            is_recurring=False,
            timezone_offset=0
        )
        
        # Create REMINDER
        EventNotificationLog.objects.create(
            event=event, 
            occurrence_date=today,
            notification_type=EventNotificationType.REMINDER
        )
        
        # Should be able to create FINAL for same date
        EventNotificationLog.objects.create(
            event=event, 
            occurrence_date=today,
            notification_type=EventNotificationType.FINAL
        )
        
        assert EventNotificationLog.objects.filter(event=event, occurrence_date=today).count() == 2
