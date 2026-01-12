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