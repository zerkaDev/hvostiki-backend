from celery import shared_task
from django.utils import timezone
from datetime import timedelta

from .models import Event, EventNotificationLog, EventCompletion

from tracker.services.ucalles_service import UCallerService
from tracker.utils import generate_occurrences


@shared_task
def send_confirmation_code(phone_number, confirmation_code):
    UCallerService().send_call_code(phone_number, confirmation_code)
    return 'Done'


@shared_task
def send_event_notifications():
    now_utc = timezone.now()

    events = Event.objects.select_related('recurrence').all()

    for event in events:
        now_local = now_utc + timedelta(minutes=event.timezone_offset)

        # Проверяем совпадение времени (по минуте)
        if (
            now_local.hour != event.time.hour or
            now_local.minute != event.time.minute
        ):
            continue

        today = now_local.date()

        # Проверяем, есть ли occurrence сегодня
        occurrences = generate_occurrences(event, today, today)

        if not occurrences:
            continue

        # Проверяем, не выполнено ли уже
        is_done = EventCompletion.objects.filter(
            event=event,
            occurrence_date=today,
        ).exists()

        if is_done:
            continue

        # Проверяем, не отправляли ли уже
        already_sent = EventNotificationLog.objects.filter(
            event=event,
            occurrence_date=today
        ).exists()

        if already_sent:
            continue

        # TODO 👉 Отправка пуша
        # send_push(event)

        # Логируем
        EventNotificationLog.objects.create(
            event=event,
            occurrence_date=today,
        )
