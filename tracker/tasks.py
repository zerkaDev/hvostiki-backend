from celery import shared_task
from django.conf import settings
from django.core.management import call_command
from django.utils import timezone
import logging
from datetime import datetime, time, timedelta
from django.db import IntegrityError, transaction
from django.db.models import Q

from tracker.models import (
    Event, EventNotificationLog, EventCompletion, EventNotificationType, Feedback, RecurrenceFrequency,
    FCMDevice, Notification, NotificationKind, NotificationSettings,
)
from tracker.event_time import time_to_wire

from tracker.services import feedback_logs
from tracker.services.ucalles_service import UCallerService
from tracker.services.firebase_service import firebase_service
from tracker.recurrence import event_slots
from tracker.notification_categories import category_for_event_type
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


@shared_task(bind=True, max_retries=5, default_retry_delay=60)
def deliver_feedback(self, feedback_id):
    """Доставляет обращение через настроенный FeedbackNotifier (с повторами при сбое)."""
    from tracker.models import Feedback
    from tracker.services.feedback_notifier import get_notifier

    feedback = Feedback.objects.filter(pk=feedback_id).first()
    if feedback is None or feedback.delivered_at:
        return 'Skipped'
    try:
        get_notifier().send(feedback)
    except Exception as exc:
        logger.warning('Не удалось доставить обращение #%s', feedback_id, exc_info=True)
        raise self.retry(exc=exc)
    feedback.delivered_at = timezone.now()
    feedback.save(update_fields=['delivered_at'])
    return 'Done'


@shared_task
def flush_expired_tokens():
    """Удаляет просроченные записи blacklist'а JWT (token_blacklist)."""
    call_command('flushexpiredtokens')
    return 'Done'


@shared_task
def delete_expired_feedback_logs():
    """Удаляет журналы приложения старше FEEDBACK_LOGS_RETENTION_DAYS (обращение остаётся)."""
    cutoff = timezone.now() - timedelta(days=settings.FEEDBACK_LOGS_RETENTION_DAYS)
    expired = (
        Feedback.objects.filter(created_at__lt=cutoff)
        .exclude(logs__isnull=True).exclude(logs='')
    )
    deleted = 0
    for feedback_id, name in list(expired.values_list('pk', 'logs')):
        feedback_logs.delete_files([name])
        Feedback.objects.filter(pk=feedback_id).update(logs=None)
        deleted += 1
    return f'Deleted: {deleted}'


@shared_task
def cleanup_old_notifications():
    """Удаляет из «Центра уведомлений» записи старше ``NOTIFICATION_RETENTION_DAYS`` дней."""
    threshold = timezone.now() - timedelta(days=settings.NOTIFICATION_RETENTION_DAYS)
    deleted, _ = Notification.objects.filter(created_at__lt=threshold).delete()
    return f'Deleted: {deleted}'


@shared_task
def send_event_notifications():
    """Рассылает пуши по событиям (запускается beat каждую минуту).

    Кандидатов отбирает SQL-предфильтр по дате старта и ``RecurrenceRule.until`` (законченные и
    ещё не начавшиеся события не проверяются). Сбой одного события логируется и не останавливает
    рассылку остальным.
    """
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
    """Уведомления по всем слотам времени события (см. :func:`tracker.recurrence.event_slots`).

    Каждый слот обрабатывается независимо: сбой одного не лишает уведомления остальные.
    """
    slots = event_slots(event)
    for slot in slots:
        # Сбой одного слота не должен лишать уведомления остальные слоты того же события
        try:
            _notify_slot(event, slot, len(slots) > 1, now_utc)
        except Exception:
            logger.exception('send_event_notifications: сбой по событию %s, слот %s', event.id, slot)


def _notify_slot(event, slot_time, multi_slot, now_utc):
    """Уведомление по одному слоту события, если его момент попал в окно ``NOTIFICATION_LOOKBACK``.

    ``slot_time`` — локальное время слота (``None`` — событие «весь день»: пуши в 08:00 и накануне в 21:00).
    ``multi_slot`` — у события несколько времён в день: тогда отметка выполнения и запись лога привязаны
    к слоту (``occurrence_time``), а в данных пуша есть ``time``. Запись лога «занимается» до отправки
    (уникальный индекс защищает от дублей между воркерами) и снимается, если отправка упала.
    """
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
        if slot_time is not None:
            # Scenario A: событие со временем (или один из слотов времени в день)
            if _is_due(datetime.combine(day, slot_time), now_local):
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
    occurrence_time = slot_time if multi_slot else None
    done = EventCompletion.objects.filter(event=event, occurrence_date=occurrence_date)
    if multi_slot:
        # старая отметка без слота (NULL) относится к первому слоту
        slot_filter = Q(occurrence_time=slot_time)
        if slot_time == event.time:
            slot_filter |= Q(occurrence_time__isnull=True)
        done = done.filter(slot_filter)
    if done.exists():
        return

    # Пользователь отключил эту категорию — не отправляем и не занимаем запись лога
    category = category_for_event_type(event.type)
    if category:
        disabled = NotificationSettings.objects.filter(user_id=event.user_id).values_list(
            'disabled_categories', flat=True
        ).first()
        if disabled and category in disabled:
            return

    # Текст уведомления: он же попадает в «Центр уведомлений». Имя питомца в заголовок не выносим:
    # в приложении рядом показан его аватар, а кличку нельзя склонять на сервере.
    title = event.title
    if notification_type == EventNotificationType.REMINDER:
        body = f"{event.pet.name}: напоминание на завтра"
    else:
        body = event.description or f"{event.pet.name}: пора выполнить"

    # «Занимаем» запись лога ДО отправки: уникальный индекс не даёт двум воркерам/запускам
    # отправить одно и то же уведомление дважды. Если отправка упадёт — запись снимаем
    # (вместе с ней по каскаду уходит и запись «Центра уведомлений»). Запись создаётся и тогда,
    # когда у пользователя нет устройств: история доступна и без пушей.
    try:
        with transaction.atomic():
            log = EventNotificationLog.objects.create(
                event=event,
                occurrence_date=occurrence_date,
                occurrence_time=occurrence_time,
                notification_type=notification_type,
            )
            notification = Notification.objects.create(
                user=event.user,
                kind=NotificationKind.EVENT,
                title=title,
                body=body,
                pet=event.pet,
                event=event,
                event_type=event.type,
                notification_type=notification_type,
                occurrence_date=occurrence_date,
                occurrence_time=slot_time,
                log=log,
            )
    except IntegrityError:
        return

    data = {
        'notification_id': str(notification.id),
        'event_id': str(event.id),
        'pet_id': str(event.pet_id),
        'event_type': event.type,
        'type': notification_type,
        'date': occurrence_date.isoformat(),
        # Время на проводе — UTC (как в остальном API), слот в БД хранится локальным
        **(
            {'time': time_to_wire(slot_time, event.timezone_offset).strftime('%H:%M')}
            if multi_slot else {}
        ),
    }

    # Отправка пуша на все устройства пользователя. Сбой одного устройства не мешает остальным;
    # если не дошло ни до одного — снимаем запись лога, и следующий запуск (в пределах окна) повторит.
    delivered = 0
    first_error = None
    for device in FCMDevice.objects.filter(user=event.user):
        try:
            firebase_service.send_push_notification(
                token=device.fcm_token, title=title, body=body, data=data,
            )
            delivered += 1
        except Exception as exc:
            logger.warning('send_event_notifications: сбой отправки на устройство %s', device.pk)
            first_error = first_error or exc

    if first_error is not None and not delivered:
        log.delete()
        raise first_error
