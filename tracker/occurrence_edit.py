"""Редактирование вхождений: правка одной даты, разделение серии и сброс устаревшего состояния.

* ``apply_override`` — «изменить только эту дату»: поля вхождения хранятся в
  :class:`EventOccurrenceOverride`, серия не меняется;
* ``split_series`` — «изменить эту дату и все следующие»: старая серия заканчивается накануне, а отметки,
  отмены, правки и журнал уведомлений с даты D переезжают на новое событие;
* ``prune_state`` — после смены расписания убирает отмены и правки дат/слотов, которых больше нет.
"""
from datetime import timedelta

from django.db import transaction

from tracker.models import (
    EventCancellation, EventCompletion, EventNotificationLog, EventOccurrenceOverride,
)
from tracker.occurrence_state import slot_filter
from tracker.recurrence import event_slots, recompute_until
from tracker.utils import generate_occurrences


def prune_state(event):
    """Удаляет отмены и правки, чьих дат (или слотов времени) нет в расписании события.

    Даты, которые в новом расписании остались, сохраняются. Отметки выполнения не трогаем.
    """
    slots = event_slots(event)
    for model in (EventCancellation, EventOccurrenceOverride):
        rows = list(model.objects.filter(event=event).only('id', 'occurrence_date', 'occurrence_time'))
        if not rows:
            continue
        dates = [row.occurrence_date for row in rows]
        valid_days = set(generate_occurrences(event, min(dates), max(dates)))
        stale = [
            row.pk for row in rows
            if row.occurrence_date not in valid_days
            or (row.occurrence_time is not None and row.occurrence_time not in slots)
        ]
        if stale:
            model.objects.filter(pk__in=stale).delete()


def apply_override(event, day, slot, fields):
    """Создаёт или обновляет правку вхождения. ``fields`` — подмножество title/description/type/time
    (``time`` уже локальное). ``slot`` — слот при нескольких временах в день, иначе ``None``."""
    row = EventOccurrenceOverride.objects.filter(slot_filter(event, slot), event=event, occurrence_date=day).first()
    if row is None:
        row = EventOccurrenceOverride(event=event, occurrence_date=day, occurrence_time=slot)
    for name, value in fields.items():
        setattr(row, name, value)
    row.save()
    return row


def _slot_mapper(old_slots, new_slots):
    """Функция «слот старой серии → слот новой» (``(ok, slot)``).

    Одно время в обеих сериях — слот остаётся ``None``; одинаковое число слотов — по порядку;
    иначе привязку к слоту сбрасываем (``ok = False``).
    """
    if len(old_slots) == 1 and len(new_slots) == 1:
        return lambda t: (True, None)
    if len(old_slots) != len(new_slots):
        return lambda t: (False, None)

    def mapper(t):
        if t is None:
            return True, new_slots[0]
        if t not in old_slots:
            return False, None
        return True, new_slots[old_slots.index(t)]

    return mapper


@transaction.atomic
def split_series(old, new, day):
    """Разделение серии: ``old`` заканчивается накануне ``day``, состояние с ``day`` переезжает на ``new``.

    Отметки, отмены, правки и журнал уведомлений переносятся, если их дата есть в расписании ``new``
    и слот сопоставляется (см. :func:`_slot_mapper`); остальное удаляется.
    """
    mapper = _slot_mapper(event_slots(old), event_slots(new))
    for model in (EventCompletion, EventCancellation, EventOccurrenceOverride, EventNotificationLog):
        rows = list(model.objects.filter(event=old, occurrence_date__gte=day))
        if not rows:
            continue
        valid_days = set(generate_occurrences(new, day, max(row.occurrence_date for row in rows)))
        for row in rows:
            ok, slot = mapper(row.occurrence_time)
            if ok and row.occurrence_date in valid_days:
                row.event = new
                row.occurrence_time = slot
                row.save(update_fields=['event', 'occurrence_time'])
            elif model is not EventNotificationLog:
                row.delete()

    rule = old.recurrence
    rule.end_date = day - timedelta(days=1)
    rule.end_count = None
    recompute_until(rule, old.start_date)
    rule.save()
    return old, new
