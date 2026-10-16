"""Действия над вхождениями событий: отмена одного вхождения и остановка серии, возврат отменённого.

Вхождения не хранятся: отмена — это строка :class:`EventCancellation` на ``(event, date, slot)``,
остановка серии («и все следующие») — отмена вхождения даты D плюс ``end_date`` правила = D.
"""
from django.db import transaction

from tracker.models import Event, EventCancellation, EventCompletion, EventOccurrenceOverride
from tracker.occurrence_state import slot_filter
from tracker.recurrence import event_slots, recompute_until


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
