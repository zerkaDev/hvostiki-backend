"""Движок повторений событий (чистые функции, без обращений к БД).

Семантика (общая для сервера и приложения, эталон — ``tracker/tests/data/recurrence_vectors.json``):

1. ``first`` — первая дата ≥ ``start_date``, подходящая под выбор дней/чисел/дат **без учёта
   интервала** (daily: ``start_date``).
2. Якорный период — неделя (с понедельника) / месяц / год, содержащий ``first``; допустимы периоды
   ``якорь + k × interval``. Для yearly с 29.02 и ``interval > 1`` якорный год — ближайший
   високосный год не раньше года ``first`` (иначе 29 февраля не наступило бы никогда).
3. Monthly: число ``d`` → ``min(d, последний день месяца)``, ``-1`` («последний день») → последний день;
   совпавшие даты сливаются. Yearly: 29.02 в невисокосный год → 28.02.
4. Вхождения не позже ``until`` (``end_date`` либо вычисленная дата N-го вхождения-дня для ``end_count``).
"""
import calendar
from dataclasses import dataclass
from datetime import date, timedelta

DAILY = 'daily'
WEEKLY = 'weekly'
MONTHLY = 'monthly'
YEARLY = 'yearly'
FREQUENCIES = (DAILY, WEEKLY, MONTHLY, YEARLY)

LAST_DAY = -1

# Допустимые значения интервала по периодам (проверяются при записи; старые строки читаются как есть)
MAX_INTERVAL = {DAILY: 30, WEEKLY: 12, MONTHLY: 12, YEARLY: 10}

MAX_TIMES = 6

MIN_END_COUNT = 2
MAX_END_COUNT = 999

# Верхний горизонт ``until`` для end_count: защита от выхода за пределы ``date`` и абсурдных значений
MAX_UNTIL = date(2100, 12, 31)

END_NEVER = 'never'
END_DATE = 'date'
END_COUNT = 'count'
END_TYPES = (END_NEVER, END_DATE, END_COUNT)


class RuleError(Exception):
    """Нарушение инварианта правила; ``field`` — поле для ответа API, ``code`` — стабильный код."""

    def __init__(self, code, message, field=None, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.extra = extra


@dataclass(frozen=True)
class Rule:
    """Неизменяемое правило повторения для движка (без обращений к БД).

    ``week_days`` — 1..7 (Пн = 1), ``month_days`` — 1..31 и ``-1`` («последний день»),
    ``year_dates`` — пары ``(месяц, день)`` без года. Лишние для периода поля игнорируются.
    Собирается из модели (:meth:`from_model`) или из результата :func:`normalize_rule`.
    """

    frequency: str
    interval: int = 1
    week_days: tuple = ()
    month_days: tuple = ()
    year_dates: tuple = ()  # ((month, day), ...)
    end_date: date | None = None
    end_count: int | None = None

    @classmethod
    def from_model(cls, rule):
        """Правило из ``RecurrenceRule``; некорректные значения в старых строках отбрасываются."""
        return cls(
            frequency=rule.frequency,
            interval=max(1, rule.interval or 1),
            week_days=tuple(sorted({d for d in (rule.week_days or []) if _is_int(d) and 1 <= d <= 7})),
            month_days=tuple(sorted({
                d for d in (rule.month_days or []) if _is_int(d) and (1 <= d <= 31 or d == LAST_DAY)
            })),
            year_dates=tuple(sorted({
                (item['month'], item['day'])
                for item in (rule.year_dates or [])
                if isinstance(item, dict) and _valid_month_day(item.get('month'), item.get('day'))
            })),
            end_date=rule.end_date,
            end_count=rule.end_count,
        )


def _is_int(value):
    """``int``, но не ``bool`` (``True`` в JSON не должно проходить как число)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_month_day(month, day):
    """Существующая календарная дата без года (29.02 допустимо)."""
    return _is_int(month) and _is_int(day) and 1 <= month <= 12 and 1 <= day <= calendar.monthrange(2000, month)[1]


def _last_dom(year, month):
    """Последний день месяца (число)."""
    return calendar.monthrange(year, month)[1]


def _monday(day):
    """Понедельник недели, в которую входит ``day``."""
    return day - timedelta(days=day.isoweekday() - 1)


def _month_dates(year, month, month_days):
    """Даты месяца по правилу: числа 29–31 переносятся на последний день, ``-1`` — последний день;
    совпавшие даты сливаются."""
    last = _last_dom(year, month)
    return sorted({
        date(year, month, last if d == LAST_DAY else min(d, last)) for d in month_days
    })


def _year_dates_in(year, year_dates):
    """Даты года по правилу; 29.02 в невисокосный год → 28.02."""
    return sorted({date(year, m, min(d, _last_dom(year, m))) for m, d in year_dates})


def _has_feb29(rule):
    """Есть ли среди дат года 29 февраля (влияет на выбор якорного года при ``interval > 1``)."""
    return (2, 29) in rule.year_dates


def _anchor(rule, start):
    """``(first, anchor)`` либо ``None``, если правило не задаёт ни одной даты."""
    if rule.frequency == DAILY:
        return start, start

    if rule.frequency == WEEKLY:
        if not rule.week_days:
            return None
        first = next(
            start + timedelta(days=i) for i in range(7)
            if (start + timedelta(days=i)).isoweekday() in rule.week_days
        )
        return first, _monday(first)

    if rule.frequency == MONTHLY:
        if not rule.month_days:
            return None
        year, month = start.year, start.month
        for _ in range(3):
            candidates = [d for d in _month_dates(year, month, rule.month_days) if d >= start]
            if candidates:
                return candidates[0], candidates[0]
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return None

    if rule.frequency == YEARLY:
        if not rule.year_dates:
            return None
        for year in range(start.year, start.year + 3):
            candidates = [d for d in _year_dates_in(year, rule.year_dates) if d >= start]
            if candidates:
                anchor_year = candidates[0].year
                if _has_feb29(rule) and rule.interval > 1:
                    while not calendar.isleap(anchor_year):
                        anchor_year += 1
                    first = next(d for d in _year_dates_in(anchor_year, rule.year_dates) if d >= start)
                    return first, anchor_year
                return candidates[0], anchor_year
        return None

    return None


def _period_dates(rule, anchor, k):
    """Даты ``k``-го допустимого периода (``якорь + k × interval``) в порядке возрастания."""
    step = rule.interval
    if rule.frequency == DAILY:
        return [anchor + timedelta(days=k * step)]
    if rule.frequency == WEEKLY:
        week = anchor + timedelta(days=7 * step * k)
        return [week + timedelta(days=wd - 1) for wd in rule.week_days]
    if rule.frequency == MONTHLY:
        year, month0 = divmod(anchor.year * 12 + anchor.month - 1 + k * step, 12)
        return _month_dates(year, month0 + 1, rule.month_days)
    return _year_dates_in(anchor + k * step, rule.year_dates)


def _first_period_index(rule, anchor, date_from):
    """Индекс периода, с которого стоит начинать, чтобы не перебирать всё от старта."""
    if date_from is None:
        return 0
    step = rule.interval
    if rule.frequency == DAILY:
        return max(0, (date_from - anchor).days // step)
    if rule.frequency == WEEKLY:
        return max(0, (date_from - anchor).days // (7 * step))
    if rule.frequency == MONTHLY:
        diff = (date_from.year * 12 + date_from.month - 1) - (anchor.year * 12 + anchor.month - 1)
        return max(0, diff // step)
    return max(0, (date_from.year - anchor) // step)


def first_occurrence(rule: Rule, start: date):
    """Первое реальное вхождение (после якоря и правила 29.02) либо ``None``."""
    found = _anchor(rule, start)
    return found[0] if found else None


def iter_occurrences(rule: Rule, start: date, date_from: date | None = None):
    """Вхождения по возрастанию, начиная с первого (или с периода, содержащего ``date_from``)."""
    found = _anchor(rule, start)
    if found is None:
        return
    first, anchor = found
    k = _first_period_index(rule, anchor, date_from)
    try:
        while True:
            for day in _period_dates(rule, anchor, k):
                if day >= first and (date_from is None or day >= date_from):
                    yield day
            k += 1
    except (OverflowError, ValueError):
        # вышли за пределы допустимых дат (год > 9999)
        return


def occurrences(rule: Rule, start: date, date_from: date, date_to: date, until: date | None = None):
    """Вхождения в окне ``[date_from, date_to]`` с учётом ``until``."""
    limit = min(date_to, until) if until else date_to
    result = []
    for day in iter_occurrences(rule, start, date_from):
        if day > limit:
            break
        result.append(day)
    return result


def compute_until(rule: Rule, start: date):
    """Эффективная дата окончания: ``end_date``, дата N-го вхождения-дня для ``end_count`` или ``None``."""
    if rule.end_date is not None:
        return rule.end_date
    if rule.end_count:
        count = 0
        for day in iter_occurrences(rule, start):
            count += 1
            if day > MAX_UNTIL:
                return MAX_UNTIL
            if count >= rule.end_count:
                return day
        return MAX_UNTIL
    return None


def check_end_against_start(rule: Rule, start: date):
    """Окончание не раньше даты события и не раньше первого вхождения (только для ``end_date``)."""
    if rule.end_date is None:
        return
    if rule.end_date < start:
        raise RuleError(
            'end_before_start',
            f'Дата окончания {rule.end_date:%d.%m.%Y} раньше даты события {start:%d.%m.%Y}',
            field='end_date',
        )
    first = first_occurrence(rule, start)
    if first is not None and rule.end_date < first:
        raise RuleError(
            'end_before_first',
            f'Дата окончания {rule.end_date:%d.%m.%Y} раньше первого повторения {first:%d.%m.%Y}',
            field='end_date',
            first=first,
        )


def derive_end_type(end_date, end_count):
    """Вид окончания для ответа API: ``date`` / ``count`` / ``never`` (хранится не он, а поля)."""
    if end_date is not None:
        return END_DATE
    if end_count:
        return END_COUNT
    return END_NEVER


def normalize_rule(data: dict) -> dict:
    """Проверяет и нормализует итоговое состояние правила (instance + patch).

    * поля чужих периодов обнуляются;
    * списки сортируются, дубли удаляются;
    * противоречия → :class:`RuleError` (ответ 400).

    ``data`` — словарь с ключами ``frequency``, ``interval``, ``week_days``, ``month_days``,
    ``year_dates``, ``end_date``, ``end_count``, а также необязательным ``end_type``.
    Возвращает словарь только с полями модели (без ``end_type``).
    """
    frequency = data.get('frequency')
    if frequency not in FREQUENCIES:
        raise RuleError('invalid_frequency', 'Неизвестный период повторения', field='frequency')

    interval = data.get('interval') or 1
    if not _is_int(interval) or not 1 <= interval <= MAX_INTERVAL[frequency]:
        raise RuleError(
            'invalid_interval',
            f'Интервал должен быть от 1 до {MAX_INTERVAL[frequency]}',
            field='interval',
        )

    week_days = month_days = year_dates = None

    if frequency == WEEKLY:
        raw = data.get('week_days')
        if not raw:
            raise RuleError('week_days_required', 'Нужен хотя бы один день недели', field='week_days')
        if not isinstance(raw, (list, tuple)) or any(not _is_int(d) or not 1 <= d <= 7 for d in raw):
            raise RuleError('invalid_week_days', 'Дни недели — числа от 1 до 7', field='week_days')
        week_days = sorted(set(raw))

    if frequency == MONTHLY:
        raw = data.get('month_days')
        if not raw:
            raise RuleError('month_days_required', 'Нужно хотя бы одно число месяца', field='month_days')
        if not isinstance(raw, (list, tuple)) or any(
            not _is_int(d) or not (1 <= d <= 31 or d == LAST_DAY) for d in raw
        ):
            raise RuleError(
                'invalid_month_days',
                'Числа месяца — от 1 до 31 или -1 («последний день»)',
                field='month_days',
            )
        month_days = sorted(set(raw))

    if frequency == YEARLY:
        raw = data.get('year_dates')
        if not raw:
            raise RuleError('year_dates_required', 'Нужна хотя бы одна дата в году', field='year_dates')
        if not isinstance(raw, (list, tuple)) or any(
            not isinstance(item, dict) or not _valid_month_day(item.get('month'), item.get('day'))
            for item in raw
        ):
            raise RuleError(
                'invalid_year_dates',
                'Дата в году — объект {"month": 1..12, "day": 1..31} с существующим днём месяца',
                field='year_dates',
            )
        year_dates = [
            {'month': m, 'day': d} for m, d in sorted({(item['month'], item['day']) for item in raw})
        ]

    end_date = data.get('end_date')
    end_count = data.get('end_count')
    end_type = data.get('end_type')

    if end_date is not None and end_count is not None:
        raise RuleError(
            'end_conflict', 'Нельзя одновременно указать дату окончания и число повторений', field='end_date'
        )
    if end_type == END_NEVER:
        end_date = end_count = None
    elif end_type == END_DATE and end_date is None:
        raise RuleError('end_date_required', 'Укажите дату окончания', field='end_date')
    elif end_type == END_COUNT and end_count is None:
        raise RuleError('end_count_required', 'Укажите число повторений', field='end_count')
    elif end_type == END_DATE:
        end_count = None
    elif end_type == END_COUNT:
        end_date = None

    if end_count is not None and (
        not _is_int(end_count) or not MIN_END_COUNT <= end_count <= MAX_END_COUNT
    ):
        raise RuleError(
            'invalid_end_count',
            f'Число повторений — от {MIN_END_COUNT} до {MAX_END_COUNT}',
            field='end_count',
        )

    return {
        'frequency': frequency,
        'interval': interval,
        'week_days': week_days,
        'month_days': month_days,
        'year_dates': year_dates,
        'end_date': end_date,
        'end_count': end_count,
        'times': data.get('times'),
    }


def rule_from_normalized(data: dict) -> Rule:
    """``Rule`` из результата :func:`normalize_rule`."""
    return Rule(
        frequency=data['frequency'],
        interval=data['interval'],
        week_days=tuple(data['week_days'] or ()),
        month_days=tuple(data['month_days'] or ()),
        year_dates=tuple((item['month'], item['day']) for item in (data['year_dates'] or ())),
        end_date=data['end_date'],
        end_count=data['end_count'],
    )


def recompute_until(rule_model, start: date):
    """Пересчитывает ``rule_model.until`` по текущему состоянию (вызывать перед save)."""
    rule_model.until = compute_until(Rule.from_model(rule_model), start)
    return rule_model.until


def normalize_times(frequency, times, event_time):
    """Слоты времени в день (локальные ``datetime.time``) → ``(times_для_БД, время_события)``.

    * не daily или список пуст → ``(None, event_time)`` (``times`` обнуляются);
    * нужен ``event_time`` (для события «весь день» слоты бессмысленны);
    * дубли сливаются, порядок — по возрастанию; 1 слот → ``times = None``, ``event_time = слот``;
      ≥2 слотов → ``event_time`` = первый слот;
    * не больше :data:`MAX_TIMES` слотов.
    """
    if frequency != DAILY or not times:
        return None, event_time
    if event_time is None:
        raise RuleError('times_without_time', 'Несколько времён нельзя задать событию без времени', field='times')
    slots = sorted({t.replace(second=0, microsecond=0) for t in times})
    if len(slots) > MAX_TIMES:
        raise RuleError('invalid_times', f'Не больше {MAX_TIMES} времён в день', field='times')
    if len(slots) == 1:
        return None, slots[0]
    return [t.strftime('%H:%M') for t in slots], slots[0]


def parse_stored_times(values):
    """Хранимые ``['08:00', ...]`` → список ``datetime.time``."""
    from datetime import time
    return [time(int(v[:2]), int(v[3:5])) for v in (values or [])]


def event_slots(event):
    """Слоты события (локальное время): ``[time]`` для многослотового, иначе ``[event.time]`` (может быть ``None``)."""
    rule = event.recurrence if event.is_recurring else None
    if rule is not None and rule.frequency == DAILY and rule.times:
        return parse_stored_times(rule.times)
    return [event.time]
