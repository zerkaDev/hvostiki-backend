from celery import shared_task
from django.core.management import call_command
from django.utils import timezone
import logging
from datetime import datetime, time, timedelta
from django.db import IntegrityError, transaction
from django.db.models import Q

from tracker.models import Event, EventNotificationLog, EventCompletion, EventNotificationType, RecurrenceFrequency, FCMDevice

from tracker.services.ucalles_service import UCallerService
from tracker.services.firebase_service import firebase_service
from tracker.utils import generate_occurrences

# Beat и воркер могут сработать с задержкой, поэтому уведомление считается
# актуальным, если целевой момент наступил не более NOTIFICATION_LOOKBACK назад.
# Повторные отправки отсекает EventNotificationLog.
NOTIFICATION_LOOKBACK = timedelta(minutes=2)

logger = logging.getLogger(__name__)


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
    # Сутки в любом часовом поясе: [now-14ч .. now+14ч] даёт локальные даты не шире ±1 дня от UTC.
    # Отсекаем в SQL события, которые заведомо закончились или ещё не начались.
    today_utc = now_utc.date()
    events = (
        Event.objects.select_related('recurrence', 'pet', 'user')
        .filter(start_date__lte=today_utc + timedelta(days=2))
        .filter(
            Q(is_recurring=False, start_date__gte=today_utc - timedelta(days=2))
            | Q(is_recurring=True) & (
                Q(recurrence__until__isnull=True, recurrence__end_date__isnull=True)
                | Q(recurrence__until__gte=today_utc - timedelta(days=2))
                | Q(recurrence__until__isnull=True, recurrence__end_date__gte=today_utc - timedelta(days=2))
            )
        )
    )

    for event in events.iterator():
        # Сбой одного события (битые данные, ошибка FCM) не должен останавливать рассылку остальным
        try:
            _notify_event(event, now_utc)
        except Exception:
            logger.exception('send_event_notifications: сбой по событию %s', event.id)


def _notify_event(event, now_utc):
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
        return

    # Проверяем, есть ли occurrence в целевую дату
    if not generate_occurrences(event, occurrence_date, occurrence_date):
        return

    # Проверяем, не выполнено ли уже
    if EventCompletion.objects.filter(event=event, occurrence_date=occurrence_date).exists():
        return

    # «Занимаем» запись лога ДО отправки: уникальный индекс не даёт двум воркерам/запускам
    # отправить одно и то же уведомление дважды. Если отправка упадёт — запись снимаем.
    try:
        with transaction.atomic():
            log = EventNotificationLog.objects.create(
                event=event,
                occurrence_date=occurrence_date,
                notification_type=notification_type,
            )
    except IntegrityError:
        return

    # Формируем текст уведомления
    title = f"{event.pet.name}: {event.title}"
    if notification_type == EventNotificationType.REMINDER:
        body = f"Напоминание: завтра в плане {event.title}"
    else:
        body = event.description or f"Пора выполнить: {event.title}"

    try:
        # Отправка пуша на все устройства пользователя
        for device in FCMDevice.objects.filter(user=event.user):
            firebase_service.send_push_notification(
                token=device.fcm_token,
                title=title,
                body=body,
                data={'event_id': str(event.id), 'type': notification_type}
            )
    except Exception:
        log.delete()  # следующий запуск (в пределах окна) попробует снова
        raise
