from celery import shared_task
from django.conf import settings
from django.core.management import call_command
from django.utils import timezone
import logging
from collections import defaultdict
from datetime import datetime, time, timedelta
from django.db import IntegrityError, transaction
from django.db.models import Q

from tracker.models import (
    Event, EventNotificationLog, EventCompletion, EventCancellation, EventOccurrenceOverride, EventNotificationType, Feedback, RecurrenceFrequency,
    FCMDevice, Notification, NotificationKind, NotificationSettings,
)
from tracker.event_time import time_to_wire
from tracker.reminders import MAX_LOOKAHEAD, effective_reminders, lead_text, reminder_moment

from tracker.services import feedback_logs
from tracker.services.ucalles_service import UCallerService
from tracker.services.firebase_service import firebase_service
from tracker.recurrence import event_slots
from tracker.notification_categories import category_for_event_type
from tracker import occurrence_state
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
    ещё не начавшиеся события не проверяются). События с напоминанием заранее (``reminders``) могут
    уведомлять за срок до 30 дней, поэтому для них окно по дате старта шире. Сбой одного события
    логируется и не останавливает рассылку остальным.
    """
    now_utc = timezone.now()
    # Сутки в любом часовом поясе: [now-14ч .. now+14ч] даёт локальные даты не шире ±1 дня от UTC.
    # Отсекаем в SQL события, которые заведомо закончились или ещё не начались.
    today_utc = now_utc.date()
    lookahead = timedelta(days=MAX_LOOKAHEAD.days + 2)

    # Правки вхождений со своим напоминанием: одним запросом на запуск, не по событию
    override_dates = defaultdict(set)
    for event_id, day in EventOccurrenceOverride.objects.filter(
        reminders__isnull=False,
        occurrence_date__gte=today_utc - timedelta(days=2),
        occurrence_date__lte=today_utc + lookahead,
    ).values_list('event_id', 'occurrence_date'):
        override_dates[event_id].add(day)

    events = (
        Event.objects.select_related('recurrence', 'pet', 'user')
        .filter(
            Q(start_date__lte=today_utc + timedelta(days=2))
            | Q(reminders__isnull=False, start_date__lte=today_utc + lookahead)
            | Q(id__in=list(override_dates))
        )
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
            _notify_event(event, now_utc, override_dates.get(event.id, ()))
        except Exception:
            logger.exception('send_event_notifications: сбой по событию %s', event.id)


def _notify_event(event, now_utc, override_dates=()):
    """Уведомления по всем слотам времени события (см. :func:`tracker.recurrence.event_slots`).

    Каждый слот обрабатывается независимо: сбой одного не лишает уведомления остальные.
    """
    slots = event_slots(event)
    for slot in slots:
        # Сбой одного слота не должен лишать уведомления остальные слоты того же события
        try:
            _notify_slot(event, slot, len(slots) > 1, now_utc, override_dates)
        except Exception:
            logger.exception('send_event_notifications: сбой по событию %s, слот %s', event.id, slot)


def _reminder_dates(event, now_local, override_dates):
    """Даты вхождений, для которых напоминание заранее может сработать сейчас.

    Момент уведомления лежит в окне ``[now - LOOKBACK, now]``, значит вхождение — в окне, сдвинутом на
    смещение вперёд; его дата определяет кандидатов. Для дат с собственным напоминанием (правка вхождения)
    смещение своё, поэтому их проверяем отдельно.
    """
    dates = set(override_dates)
    if event.reminders:
        shift = timedelta(minutes=event.reminders[0])
        dates |= {(now_local + shift - NOTIFICATION_LOOKBACK).date(), (now_local + shift).date()}
    return dates


def _due_occurrences(event, slot_time, now_local, overrides, dates):
    """Вхождения слота, по которым пора слать уведомление: ``(тип, дата вхождения, смещение или None)``.

    Правило «как раньше» (``reminders`` не задано): событие со временем — в момент события; «весь день» —
    в 08:00 дня события и накануне в 21:00 (кроме ежедневных). Если у вхождения (или события) напоминание
    задано, уведомление — в момент ``вхождение - смещение`` (у «всего дня» вхождение отсчитывается от 09:00).
    """
    is_daily = (
        event.is_recurring and
        event.recurrence and
        event.recurrence.frequency == RecurrenceFrequency.DAILY
    )
    found = []

    # Смотрим текущие и предыдущие сутки: рассылка могла задержаться и
    # сработать уже после полуночи.
    for day in (now_local.date(), (now_local - timedelta(days=1)).date()):
        override = overrides.get(day)
        day_time = override.time if override is not None and override.time is not None else slot_time
        if day_time is not None:
            # Scenario A: событие со временем (или один из слотов времени в день)
            if effective_reminders(event, override) is None and _is_due(datetime.combine(day, day_time), now_local):
                found.append((EventNotificationType.STANDARD, day, None))
                break
            continue

        if effective_reminders(event, override) is None and _is_due(datetime.combine(day, time(8, 0)), now_local):
            # Scenario B & C (Final): 8:00 дня события
            found.append((EventNotificationType.FINAL, day, None))
            break

        tomorrow = day + timedelta(days=1)
        if (
            not is_daily
            and effective_reminders(event, overrides.get(tomorrow)) is None
            and _is_due(datetime.combine(day, time(21, 0)), now_local)
        ):
            # Scenario C (Reminder): 21:00 накануне
            found.append((EventNotificationType.REMINDER, tomorrow, None))
            break

    # Напоминание заранее: смещение выбрано пользователем
    for day in sorted(dates):
        reminders = effective_reminders(event, overrides.get(day))
        if not reminders:
            continue
        override = overrides.get(day)
        day_time = override.time if override is not None and override.time is not None else slot_time
        if not _is_due(reminder_moment(day, day_time, reminders[0]), now_local):
            continue
        if reminders[0] == 0:
            kind = EventNotificationType.STANDARD if day_time is not None else EventNotificationType.FINAL
        else:
            kind = EventNotificationType.REMINDER
        found.append((kind, day, reminders[0]))
    return found


def _notify_slot(event, slot_time, multi_slot, now_utc, override_dates=()):
    """Уведомления по одному слоту события, момент которых попал в окно ``NOTIFICATION_LOOKBACK``.

    ``slot_time`` — локальное время слота (``None`` — событие «весь день»).
    ``multi_slot`` — у события несколько времён в день: тогда отметка выполнения и запись лога привязаны
    к слоту (``occurrence_time``), а в данных пуша есть ``time``. Запись лога «занимается» до отправки
    (уникальный индекс защищает от дублей между воркерами) и снимается, если отправка упала.
    """
    # Локальное «настенное» время пользователя
    now_local = (now_utc + timedelta(minutes=event.timezone_offset)).replace(tzinfo=None)

    lookup_slot = slot_time if multi_slot else None
    today = now_local.date()
    dates = {
        today - timedelta(days=1), today, today + timedelta(days=1),
        *_reminder_dates(event, now_local, override_dates),
    }
    # Правки вхождений («изменить только эту дату») могут сдвинуть время, сменить название, тип и напоминание
    overrides = {
        row.occurrence_date: row
        for row in EventOccurrenceOverride.objects.filter(
            occurrence_state.slot_filter(event, lookup_slot), event=event, occurrence_date__in=dates,
        )
    }

    for notification_type, occurrence_date, lead_minutes in _due_occurrences(
        event, slot_time, now_local, overrides, dates,
    ):
        # Сбой одного вхождения не должен лишать уведомления другие, попавшие в окно
        try:
            _send_occurrence_notification(
                event, slot_time, multi_slot, notification_type, occurrence_date, lead_minutes,
                overrides.get(occurrence_date),
            )
        except Exception:
            logger.exception(
                'send_event_notifications: сбой по событию %s, дата %s', event.id, occurrence_date,
            )


def _send_occurrence_notification(
    event, slot_time, multi_slot, notification_type, occurrence_date, lead_minutes, override,
):
    """Проверяет вхождение и отправляет уведомление ``notification_type`` по нему.

    ``lead_minutes`` — смещение напоминания заранее (``None`` для правил «как раньше»).
    """
    lookup_slot = slot_time if multi_slot else None

    # Проверяем, есть ли occurrence в целевую дату
    if not generate_occurrences(event, occurrence_date, occurrence_date):
        return

    # Отменённое вхождение не напоминаем
    if EventCancellation.objects.filter(
        occurrence_state.slot_filter(event, lookup_slot), event=event, occurrence_date=occurrence_date,
    ).exists():
        return

    event_type = override.type if override is not None and override.type is not None else event.type
    event_title = override.title if override is not None and override.title is not None else event.title
    event_description = (
        override.description if override is not None and override.description is not None else event.description
    )
    shown_time = override.time if override is not None and override.time is not None else slot_time

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
    category = category_for_event_type(event_type)
    if category:
        disabled = NotificationSettings.objects.filter(user_id=event.user_id).values_list(
            'disabled_categories', flat=True
        ).first()
        if disabled and category in disabled:
            return

    # Текст уведомления: он же попадает в «Центр уведомлений». Имя питомца в заголовок не выносим:
    # в приложении рядом показан его аватар, а кличку нельзя склонять на сервере.
    title = event_title
    if lead_minutes:
        body = f"{event.pet.name}: {lead_text(lead_minutes, shown_time)}"
    elif notification_type == EventNotificationType.REMINDER:
        body = f"{event.pet.name}: напоминание на завтра"
    else:
        body = event_description or f"{event.pet.name}: пора выполнить"

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
                event_type=event_type,
                notification_type=notification_type,
                occurrence_date=occurrence_date,
                occurrence_time=shown_time,
                log=log,
            )
    except IntegrityError:
        return

    data = {
        'notification_id': str(notification.id),
        'event_id': str(event.id),
        'pet_id': str(event.pet_id),
        'event_type': event_type,
        'type': notification_type,
        'date': occurrence_date.isoformat(),
        # Время на проводе — UTC (как в остальном API), слот в БД хранится локальным
        **(
            {'time': time_to_wire(shown_time, event.timezone_offset).strftime('%H:%M')}
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
