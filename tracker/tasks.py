from celery import shared_task
from django.utils import timezone
from datetime import timedelta

from tracker.models import Event, EventNotificationLog, EventCompletion, EventNotificationType, RecurrenceFrequency, FCMDevice

from tracker.services.ucalles_service import UCallerService
from tracker.services.firebase_service import firebase_service
from tracker.utils import generate_occurrences


@shared_task
def send_confirmation_code(phone_number, confirmation_code):
    UCallerService().send_call_code(phone_number, confirmation_code)
    return 'Done'


@shared_task
def send_event_notifications():
    now_utc = timezone.now()
    # Загружаем события с питомцами и пользователями
    events = Event.objects.select_related('recurrence', 'pet', 'user').all()

    for event in events:
        now_local = now_utc + timedelta(minutes=event.timezone_offset)
        today = now_local.date()
        current_hour = now_local.hour
        current_minute = now_local.minute

        notification_type = None
        occurrence_date = today

        if event.time is not None:
            # Scenario A: Event has time
            if current_hour == event.time.hour and current_minute == event.time.minute:
                notification_type = EventNotificationType.STANDARD
        else:
            # Events WITHOUT time
            is_daily = (
                event.is_recurring and 
                event.recurrence and 
                event.recurrence.frequency == RecurrenceFrequency.DAILY
            )

            if current_hour == 8 and current_minute == 0:
                # Scenario B & C (Final): 8:00 AM day of
                notification_type = EventNotificationType.FINAL
                occurrence_date = today
            elif not is_daily and current_hour == 21 and current_minute == 0:
                # Scenario C (Reminder): 9:00 PM day before
                notification_type = EventNotificationType.REMINDER
                occurrence_date = today + timedelta(days=1)

        if not notification_type:
            continue

        # Проверяем, есть ли occurrence в целевую дату
        occurrences = generate_occurrences(event, occurrence_date, occurrence_date)
        if not occurrences:
            continue

        # Проверяем, не выполнено ли уже
        if EventCompletion.objects.filter(event=event, occurrence_date=occurrence_date).exists():
            continue

        # Проверяем, не отправляли ли уже такой тип уведомления для этой даты
        already_sent = EventNotificationLog.objects.filter(
            event=event,
            occurrence_date=occurrence_date,
            notification_type=notification_type
        ).exists()

        if already_sent:
            continue

        # Формируем текст уведомления
        title = f"{event.pet.name}: {event.title}"
        if notification_type == EventNotificationType.REMINDER:
            body = f"Напоминание: завтра в плане {event.title}"
        else:
            body = event.description or f"Пора выполнить: {event.title}"

        # Отправка пуша на все устройства пользователя
        devices = FCMDevice.objects.filter(user=event.user)
        for device in devices:
            firebase_service.send_push_notification(
                token=device.fcm_token,
                title=title,
                body=body,
                data={'event_id': str(event.id), 'type': notification_type}
            )

        # Логируем
        EventNotificationLog.objects.create(
            event=event,
            occurrence_date=occurrence_date,
            notification_type=notification_type
        )
