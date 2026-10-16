"""Состояние отдельного вхождения: отметки выполнения, отмены и правки.

Все три сущности привязаны к ``(event, occurrence_date, occurrence_time)``, где ``occurrence_time`` —
локальное время слота при нескольких временах в день и ``NULL`` иначе. В многослотовом режиме
``NULL``-запись относится к первому слоту (так было до появления слотов).
"""
from django.db.models import Q


def lookup_keys(event, occurrence_date, slot):
    """Ключи ``(event_id, date, slot)``, под которыми вхождение могло быть записано.

    ``slot`` — слот вхождения при нескольких временах в день, иначе ``None``.
    """
    keys = [(event.id, occurrence_date, slot)]
    if slot is not None and slot == event.time:
        keys.append((event.id, occurrence_date, None))
    return keys


def find(mapping, event, occurrence_date, slot):
    """Значение из ``mapping`` (ключи ``(event_id, date, slot)``) для вхождения или ``None``."""
    for key in lookup_keys(event, occurrence_date, slot):
        if key in mapping:
            return mapping[key]
    return None


def slot_filter(event, slot):
    """Q-фильтр по слоту для таблиц отметок/отмен/правок (поле ``occurrence_time``).

    Без слота (одно время / весь день) подходит любая запись даты, как у отметок выполнения.
    """
    if slot is None:
        return Q()
    flt = Q(occurrence_time=slot)
    if slot == event.time:
        flt |= Q(occurrence_time__isnull=True)
    return flt
