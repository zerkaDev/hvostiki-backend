"""Напоминание о событии: смещение в минутах до вхождения.

Значение ``reminders`` (у :class:`Event` и :class:`EventOccurrenceOverride`):

* ``None`` — не задано: событие уведомляет «как раньше» (в момент события; у события «на весь день» —
  в 08:00 дня события и накануне в 21:00);
* ``[]`` — не напоминать;
* ``[m]`` — за ``m`` минут до вхождения (``0`` — в момент события). В v1 не больше одного элемента, но
  хранится списком, чтобы позже добавить несколько напоминаний без миграции.

У события «на весь день» (без времени) момент отсчёта — 09:00 дня вхождения, а ``m`` кратно суткам.
"""
from datetime import datetime, time, timedelta

MAX_REMINDER_MINUTES = 30 * 24 * 60
MINUTES_IN_DAY = 24 * 60
# Момент суток, от которого отсчитывается напоминание события «на весь день»
ALL_DAY_REMINDER_TIME = time(9, 0)
# Насколько вперёд от «сейчас» может лежать вхождение, для которого уже пора слать уведомление
MAX_LOOKAHEAD = timedelta(minutes=MAX_REMINDER_MINUTES)


class ReminderError(ValueError):
    """Недопустимое значение напоминания."""


def validate_reminders(value, *, all_day):
    """Проверяет значение ``reminders``: список целых 0..30 дней, не больше одного; у «всего дня» — кратно суткам."""
    if value is None:
        return value
    if not isinstance(value, list) or len(value) > 1:
        raise ReminderError('Напоминание: ожидается список из одного значения (минуты до события)')
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ReminderError('Напоминание: значение должно быть целым числом минут')
        if item < 0 or item > MAX_REMINDER_MINUTES:
            raise ReminderError(f'Напоминание: допустимо от 0 до {MAX_REMINDER_MINUTES} минут')
        if all_day and item % MINUTES_IN_DAY:
            raise ReminderError('Напоминание для события на весь день задаётся в днях')
    return value


def effective_reminders(event, override=None):
    """Напоминание вхождения: правка даты (если задана) вытесняет напоминание события."""
    if override is not None and override.reminders is not None:
        return override.reminders
    return event.reminders


def reminder_moment(occurrence_date, occurrence_time, minutes):
    """Локальный момент уведомления: вхождение (``occurrence_time`` или 09:00 у «всего дня») минус ``minutes``."""
    at = occurrence_time if occurrence_time is not None else ALL_DAY_REMINDER_TIME
    return datetime.combine(occurrence_date, at) - timedelta(minutes=minutes)


def _plural(n, one, few, many):
    if n % 100 in range(11, 15):
        return many
    return one if n % 10 == 1 else few if n % 10 in (2, 3, 4) else many


def lead_text(minutes, occurrence_time):
    """Фраза о том, когда событие: «через 10 минут», «завтра в 15:00», «через 2 дня в 15:00»."""
    at = f' в {occurrence_time.strftime("%H:%M")}' if occurrence_time is not None else ''
    if minutes == MINUTES_IN_DAY:
        return f'завтра{at}'
    if minutes % (7 * MINUTES_IN_DAY) == 0:
        n = minutes // (7 * MINUTES_IN_DAY)
        return f'через {n} {_plural(n, "неделю", "недели", "недель")}{at}'
    if minutes % MINUTES_IN_DAY == 0:
        n = minutes // MINUTES_IN_DAY
        return f'через {n} {_plural(n, "день", "дня", "дней")}{at}'
    if minutes % 60 == 0:
        n = minutes // 60
        return f'через {n} {_plural(n, "час", "часа", "часов")}'
    return f'через {minutes} {_plural(minutes, "минуту", "минуты", "минут")}'
