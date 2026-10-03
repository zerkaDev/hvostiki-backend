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

    def test_event_with_time_sends_within_lookback_window(self, user, pet):
        """Beat может опоздать: уведомление уходит, если момент уже наступил."""
        event_time = time(15, 30)
        event = Event.objects.create(
            user=user, pet=pet, title="Late Beat Task",
            start_date=date.today(), time=event_time,
            is_recurring=False,
            timezone_offset=0
        )

        # Beat сработал на минуту позже
        mock_now = datetime.combine(date.today(), time(15, 31))
        with patch('django.utils.timezone.now', return_value=timezone.make_aware(mock_now)):
            send_event_notifications()

        assert EventNotificationLog.objects.filter(
            event=event,
            notification_type=EventNotificationType.STANDARD,
            occurrence_date=date.today()
        ).exists()

    def test_event_with_time_not_sent_outside_lookback_window(self, user, pet):
        event_time = time(15, 30)
        event = Event.objects.create(
            user=user, pet=pet, title="Too Late Task",
            start_date=date.today(), time=event_time,
            is_recurring=False,
            timezone_offset=0
        )

        # Опоздали на 10 минут — окно срабатывания уже прошло
        mock_now = datetime.combine(date.today(), time(15, 40))
        with patch('django.utils.timezone.now', return_value=timezone.make_aware(mock_now)):
            send_event_notifications()

        assert not EventNotificationLog.objects.filter(event=event).exists()

    def test_event_after_midnight_uses_previous_day(self, user, pet):
        """Событие почти в полночь и задержавшийся beat не должны терять уведомление."""
        event_time = time(23, 59)
        event = Event.objects.create(
            user=user, pet=pet, title="Midnight Task",
            start_date=date.today(), time=event_time,
            is_recurring=False,
            timezone_offset=0
        )

        mock_now = datetime.combine(date.today() + timedelta(days=1), time(0, 0))
        with patch('django.utils.timezone.now', return_value=timezone.make_aware(mock_now)):
            send_event_notifications()

        assert EventNotificationLog.objects.filter(
            event=event,
            notification_type=EventNotificationType.STANDARD,
            occurrence_date=date.today()
        ).exists()


def test_beat_schedule_references_registered_tasks():
    """Все задачи в CELERY_BEAT_SCHEDULE должны быть зарегистрированы в Celery.

    Регрессия: раньше в расписании стояло 'events.tasks.*', а задача
    регистрируется как 'tracker.tasks.*', из-за чего beat падал с NotRegistered.
    """
    from django.conf import settings
    from config.celery import app

    app.loader.import_default_modules()
    registered = set(app.tasks)

    missing = [
        f'{name}: {entry["task"]}'
        for name, entry in settings.CELERY_BEAT_SCHEDULE.items()
        if entry['task'] not in registered
    ]
    assert not missing, f'Незарегистрированные задачи в CELERY_BEAT_SCHEDULE: {missing}'


@pytest.mark.django_db
class TestNotificationReliability:
    def _event(self, user, pet, title, **extra):
        rule = RecurrenceRule.objects.create(frequency=RecurrenceFrequency.DAILY, **extra.pop('rule', {}))
        return Event.objects.create(
            user=user, pet=pet, title=title, start_date=date.today(), time=None,
            is_recurring=True, recurrence=rule, timezone_offset=0,
        )

    def _run(self):
        mock_now = datetime.combine(date.today(), time(8, 0))
        with patch('django.utils.timezone.now', return_value=timezone.make_aware(mock_now)):
            send_event_notifications()

    def test_failure_of_one_event_does_not_stop_others(self, user, pet):
        from tracker.models import FCMDevice
        FCMDevice.objects.create(user=user, fcm_token='t')
        first = self._event(user, pet, 'A')
        second = self._event(user, pet, 'B')
        calls = []

        def fake_send(**kwargs):
            calls.append(kwargs['title'])
            if kwargs['title'].endswith('A'):
                raise RuntimeError('boom')

        with patch('tracker.tasks.firebase_service.send_push_notification', side_effect=fake_send):
            self._run()

        assert len(calls) == 2
        # неудавшееся уведомление не «занято» — будет повторено; удавшееся записано
        assert not EventNotificationLog.objects.filter(event=first).exists()
        assert EventNotificationLog.objects.filter(event=second).exists()

    def test_already_claimed_log_prevents_duplicate_push(self, user, pet):
        from tracker.models import FCMDevice
        FCMDevice.objects.create(user=user, fcm_token='t')
        event = self._event(user, pet, 'A')
        EventNotificationLog.objects.create(
            event=event, occurrence_date=date.today(), notification_type=EventNotificationType.FINAL,
        )
        with patch('tracker.tasks.firebase_service.send_push_notification') as send:
            self._run()
        send.assert_not_called()

    def test_finished_rule_is_not_notified(self, user, pet):
        from tracker.models import FCMDevice
        FCMDevice.objects.create(user=user, fcm_token='t')
        ended = self._event(user, pet, 'old', rule={'end_date': date.today() - timedelta(days=10),
                                                    'until': date.today() - timedelta(days=10)})
        with patch('tracker.tasks.firebase_service.send_push_notification') as send:
            self._run()
        send.assert_not_called()
        assert not EventNotificationLog.objects.filter(event=ended).exists()
