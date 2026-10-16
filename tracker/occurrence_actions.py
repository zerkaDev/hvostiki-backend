"""Действия над вхождениями событий: отмена одного вхождения и остановка серии, возврат отменённого.

Вхождения не хранятся: отмена — это строка :class:`EventCancellation` на ``(event, date, slot)``,
остановка серии («и все следующие») — отмена вхождения даты D плюс ``end_date`` правила = D.
"""
from datetime import timedelta

from django.db import transaction

from tracker.models import Event, EventCancellation, EventCompletion, EventOccurrenceOverride
from tracker.occurrence_state import slot_filter
from tracker.recurrence import event_slots, recompute_until
from tracker.utils import generate_occurrences


class OccurrenceConflict(Exception):
    """Действие противоречит состоянию вхождения (например, отмена уже выполненного)."""


def is_cancelled(event, day, slot):
    return EventCancellation.objects.filter(slot_filter(event, slot), event=event, occurrence_date=day).exists()


def is_done(event, day, slot):
    return EventCompletion.objects.filter(slot_filter(event, slot), event=event, occurrence_date=day).exists()


def _ensure_cancellation(event, day, slot):
    """Идемпотентно создаёт строку отмены (старая запись без слота у первого слота тоже считается)."""
    if not is_cancelled(event, day, slot):
        EventCancellation.objects.create(event=event, occurrence_date=day, occurrence_time=slot)


def _stop_series(event, day):
    """Серия заканчивается датой ``day``; всё состояние позже неё удаляется (его уже нет в расписании)."""
    rule = event.recurrence
    rule.end_date = day
    rule.end_count = None
    recompute_until(rule, event.start_date)
    rule.save()
    for model in (EventCancellation, EventCompletion, EventOccurrenceOverride):
        model.objects.filter(event=event, occurrence_date__gt=day).delete()


@transaction.atomic
def cancel_occurrence(event, day, slot, scope):
    """Отменяет вхождение (``scope='one'``) либо вхождения даты ``day`` и всех следующих (``'future'``).

    ``slot`` — слот вхождения при нескольких временах в день, иначе ``None``. Выполненное вхождение
    отменить нельзя (:class:`OccurrenceConflict`). Для ``future`` затрагиваются все слоты дня
    ``day``, кроме уже выполненных; для одиночного события ``future`` равно ``one``.
    """
    event = Event.objects.select_for_update(of=('self',)).select_related('recurrence').get(pk=event.pk)
    if is_done(event, day, slot):
        raise OccurrenceConflict('Вхождение уже выполнено')

    if scope == 'future' and event.is_recurring and event.recurrence is not None:
        slots = event_slots(event)
        for target in (slots if len(slots) > 1 else [None]):
            if not is_done(event, day, target):
                _ensure_cancellation(event, day, target)
        _stop_series(event, day)
    else:
        _ensure_cancellation(event, day, slot)
    return event


@transaction.atomic
def restore_occurrence(event, day, slot):
    """Возвращает отменённое вхождение (идемпотентно). Остановленная серия не удлиняется."""
    EventCancellation.objects.filter(slot_filter(event, slot), event=event, occurrence_date=day).delete()
    return event


def series_stats(event, day):
    """Сводка по серии относительно даты ``day`` (для подтверждения «Отменится N событий»).

    * ``remaining`` — не отменённые вхождения с даты ``day`` до конца серии, каждое время в день
      считается отдельным; ``None`` у бессрочной серии;
    * ``past`` — не отменённые вхождения до даты ``day``;
    * ``series_end`` — дата окончания серии (``None`` у бессрочной), ``is_open_ended`` — серия бессрочна;
    * ``cancelled_dates`` — даты не ранее ``day``, на которые есть отмены (клиент по ним показывает
      подсказку о сбросе отмен при смене расписания).
    """
    slots = event_slots(event)
    rule = event.recurrence if event.is_recurring else None
    until = (rule.until or rule.end_date) if rule is not None else event.start_date
    is_open_ended = until is None

    cancelled = {}
    for cancel_day, cancel_time in EventCancellation.objects.filter(event=event).values_list(
        'occurrence_date', 'occurrence_time',
    ):
        # запись без слота относится к первому (единственному) слоту
        key = slots[0] if cancel_time is None else cancel_time
        cancelled.setdefault(cancel_day, set()).add(key)

    def count(date_from, date_to):
        total = 0
        for occurrence in generate_occurrences(event, date_from, date_to):
            gone = cancelled.get(occurrence, set())
            total += sum(1 for slot in slots if slot not in gone)
        return total

    past = count(event.start_date, day - timedelta(days=1)) if day > event.start_date else 0
    remaining = None if is_open_ended else (count(day, until) if until >= day else 0)
    return {
        'remaining': remaining,
        'past': past,
        'series_end': until.isoformat() if until else None,
        'is_open_ended': is_open_ended,
        'cancelled_dates': sorted(d.isoformat() for d in cancelled if d >= day),
    }
