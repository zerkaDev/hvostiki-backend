"""Режим проверки приложения магазином (App Store / Google Play).

Пока режим включён, вход не требует реального звонка uCaller:

* ``/auth/send-code/`` не инициирует платный звонок на номер;
* ``/auth/verify-code/`` принимает ``REVIEW_LOGIN_CODE`` вместо кода из звонка.

Зачем: ревьюер Apple/Google не может принять звонок на свой номер, а каждый
запрос кода тратит деньги на uCaller. Включается переменными окружения и
**только на время ревью** — по умолчанию выключен.

Настройки (``config/settings.py``):

* ``REVIEW_LOGIN_ENABLED`` — ``1`` включает режим;
* ``REVIEW_LOGIN_CODE`` — код, который принимается (например, ``0011``);
* ``REVIEW_LOGIN_PHONES`` — необязательный список номеров через запятую.
  Если пуст, режим действует для любого номера (так ревьюер может ввести любой);
  если задан, только эти номера.

Каждое использование пишется в лог с уровнем WARNING, чтобы включённый режим
не остался незамеченным в проде.
"""
import logging
import secrets

from django.conf import settings

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    """Включён ли режим: нужен и флаг, и непустой код."""
    return bool(settings.REVIEW_LOGIN_ENABLED and settings.REVIEW_LOGIN_CODE)


def phone_allowed(phone_number: str) -> bool:
    """Попадает ли номер под режим проверки (пустой список номеров = любой)."""
    if not is_enabled():
        return False
    phones = settings.REVIEW_LOGIN_PHONES
    if not phones:
        return True
    return phone_number in phones


def code_matches(code: str) -> bool:
    """Совпадает ли введённый код с кодом проверки (сравнение постоянного времени)."""
    if not is_enabled():
        return False
    return secrets.compare_digest(str(code), str(settings.REVIEW_LOGIN_CODE))


def log_use(phone_number: str, client_ip: str | None = None) -> None:
    """Отмечает использование режима в логах: его нужно выключить после ревью."""
    logger.warning(
        'Review-вход использован: номер %s, ip %s — режим REVIEW_LOGIN_ENABLED включён',
        phone_number, client_ip or '?',
    )
