from datetime import date as date_type, datetime, time as time_type, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def normalize_phone(phone: str) -> str:
    """
    Нормализует номер телефона.
    uCaller ожидает номер в формате 79001000010 (без +)
    """
    # Удаляем все нецифровые символы
    phone = ''.join(filter(str.isdigit, phone))

    # Если номер начинается с 8, заменяем на 7
    if phone.startswith('8'):
        phone = '7' + phone[1:]
    # Если номер начинается с +7, убираем +
    elif phone.startswith('7'):
        phone = phone  # оставляем как есть
    # Если номер меньше 11 цифр, добавляем 7 в начало
    elif len(phone) == 10:
        phone = '7' + phone

    return phone


def generate_occurrences(event, date_from, date_to):
    occurrences = []

    current = max(event.start_date, date_from)

    if not event.is_recurring:
        if date_from <= event.start_date <= date_to:
            occurrences.append(event.start_date)
        return occurrences

    rule = event.recurrence

    while current <= date_to:
        if rule.end_date and current > rule.end_date:
            break

        if rule.frequency == "daily":
            occurrences.append(current)

        elif rule.frequency == "weekly":
            if current.isoweekday() in (rule.week_days or []):
                occurrences.append(current)

        elif rule.frequency == "monthly":
            if current.day in (rule.month_days or []):
                occurrences.append(current)

        current += timedelta(days=1)

    return occurrences


def timezone_offset_minutes(tz_name, date, time):
    """
    Возвращает смещение временной зоны в минутах относительно UTC для указанной локальной даты/времени.
    Например: UTC+3 -> 180, UTC-5 -> -300.
    """
    if tz_name in (None, "") or not date or not time:
        return None

    # Поддержка хранения timezone как фиксированного offset (в минутах) строкой: "180"
    try:
        return int(tz_name)
    except (TypeError, ValueError):
        pass

    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return None

    local_dt = datetime.combine(date, time).replace(tzinfo=tz)
    offset = local_dt.utcoffset()
    if offset is None:
        return None

    return int(offset.total_seconds() // 60)


def shift_time_by_minutes(value: time_type, delta_minutes: int) -> time_type:
    """
    Сдвигает time на delta_minutes с переходом через сутки (mod 24h).
    """
    if value is None:
        return None

    base = datetime.combine(date_type(2000, 1, 1), value)
    shifted = base + timedelta(minutes=int(delta_minutes))
    return shifted.time()
