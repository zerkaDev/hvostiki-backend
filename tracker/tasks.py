from celery import shared_task
from django.core.management import call_command
from django.utils import timezone
from datetime import datetime, time, timedelta

from tracker.models import Event, EventNotificationLog, EventCompletion, EventNotificationType, RecurrenceFrequency, FCMDevice

from tracker.services.ucalles_service import UCallerService
from tracker.services.firebase_service import firebase_service
from tracker.utils import generate_occurrences

# Beat и воркер могут сработать с задержкой, поэтому уведомление считается
# актуальным, если целевой момент наступил не более NOTIFICATION_LOOKBACK назад.
# Повторные отправки отсекает EventNotificationLog.
NOTIFICATION_LOOKBACK = timedelta(minutes=2)


def _is_due(target: datetime, now_local: datetime) -> bool:
    """Попадает ли целевой момент в окно [now_local - NOTIFICATION_LOOKBACK, now_local]."""
    return timedelta(0) <= now_local - target < NOTIFICATION_LOOKBACK


@shared_task
def send_confirmation_code(phone_number, confirmation_code):
    UCallerService().send_call_code(phone_number, confirmation_code)
    return 'Done'


@shared_task
def flush_expired_tokens():
    """Удаляет просроченные записи blacklist'а JWT (token_blacklist)."""
    call_command('flushexpiredtokens')
    return 'Done'


@shared_task
def send_event_notifications():
    now_utc = timezone.now()
    # Загружаем события с питомцами и пользователями
    events = Event.objects.select_related('recurrence', 'pet', 'user').all()

    for event in events:
        # Локальное «настенное» время пользователя
        now_local = (now_utc + timedelta(minutes=event.timezone_offset)).replace(tzinfo=None)

        is_daily = (
            event.is_recurring and
            event.recurrence and
            event.recurrence.frequency == RecurrenceFrequency.DAILY
        )

        notification_type = None
        occurrence_date = None

        # Смотрим текущие и предыдущие сутки: рассылка могла задержаться и
        # сработать уже после полуночи.
        for day in (now_local.date(), (now_local - timedelta(days=1)).date()):
            if event.time is not None:
                # Scenario A: событие со временем
                if _is_due(datetime.combine(day, event.time), now_local):
                    notification_type = EventNotificationType.STANDARD
                    occurrence_date = day
                    break
                continue

            if _is_due(datetime.combine(day, time(8, 0)), now_local):
                # Scenario B & C (Final): 8:00 дня события
                notification_type = EventNotificationType.FINAL
                occurrence_date = day
                break

            if not is_daily and _is_due(datetime.combine(day, time(21, 0)), now_local):
                # Scenario C (Reminder): 21:00 накануне
                notification_type = EventNotificationType.REMINDER
                occurrence_date = day + timedelta(days=1)
                break

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
