from datetime import date as date_type, datetime, time as time_type, timedelta


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
    """Даты вхождений события в окне [date_from, date_to] (семантика — в tracker/recurrence.py)."""
    if not event.is_recurring or event.recurrence is None:
        return [event.start_date] if date_from <= event.start_date <= date_to else []

    from tracker.recurrence import Rule, occurrences

    rule = event.recurrence
    # until — кэш; для старых строк без него берём end_date
    until = rule.until or rule.end_date
    return occurrences(Rule.from_model(rule), event.start_date, max(date_from, event.start_date), date_to, until)


def shift_time_by_minutes(value: time_type, delta_minutes: int) -> time_type:
    """
    Сдвигает time на delta_minutes с переходом через сутки (mod 24h).
    """
    if value is None:
        return None

    base = datetime.combine(date_type(2000, 1, 1), value)
    shifted = base + timedelta(minutes=int(delta_minutes))
    return shifted.time()
