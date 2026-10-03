"""Время события на проводе и в БД.

Инвариант: в БД ``Event.time`` (и слоты ``RecurrenceRule.times``) хранятся в локальном времени
события (``UTC + timezone_offset``). В запросах и ответах API время — в **UTC**.
"""
from datetime import time as time_type

from tracker.utils import shift_time_by_minutes


def time_to_stored(value: time_type, offset_minutes: int) -> time_type:
    """Время из запроса (UTC) → значение для БД (локальное)."""
    if value is None:
        return value
    return shift_time_by_minutes(value, offset_minutes)


def time_to_wire(value: time_type, offset_minutes: int) -> time_type:
    """Значение из БД (локальное) → время для ответа (UTC)."""
    if value is None:
        return value
    return shift_time_by_minutes(value, -offset_minutes)
