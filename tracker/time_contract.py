"""Контракт передачи времени события между клиентом и API.

Инвариант: в БД ``Event.time`` всегда хранится в локальном времени события
(``UTC + timezone_offset``), независимо от режима клиента.

Режимы (определяются заголовком запроса ``X-Time-Contract``):

* ``utc`` — клиент присылает и ожидает время в UTC; сервер переводит
  UTC → локальное при записи и локальное → UTC при чтении.
* ``legacy`` (заголовка нет или значение неизвестно) — старые клиенты присылают
  и ожидают **локальное** время (то, что пользователь видит на экране);
  сервер ничего не сдвигает ни при записи, ни при чтении.
"""
import logging
import re
from datetime import time as time_type

from django.conf import settings

from tracker.utils import shift_time_by_minutes

TIME_CONTRACT_HEADER = 'X-Time-Contract'
APP_VERSION_HEADER = 'X-App-Version'

CONTRACT_UTC = 'utc'
CONTRACT_LEGACY = 'legacy'

# Заголовки в ``request.META`` (Django добавляет префикс HTTP_ и заменяет «-» на «_»)
_META_CONTRACT = 'HTTP_X_TIME_CONTRACT'
_META_APP_VERSION = 'HTTP_X_APP_VERSION'

logger = logging.getLogger('tracker.time_contract')

# Пути, на которых режим контракта влияет на ответ (для учёта доли legacy-запросов).
_EVENT_PATH_RE = re.compile(r'^/(event_schedule(/|$)|pets/\d+/upcoming/?$)')


def resolve_time_contract(value):
    """Режим контракта по значению заголовка (регистр не важен)."""
    if value and value.strip().lower() == CONTRACT_UTC:
        return CONTRACT_UTC
    return CONTRACT_LEGACY


def get_time_contract(request):
    """Режим контракта запроса. Без запроса (внутренние вызовы) действует документированный ``utc``."""
    if request is None:
        return CONTRACT_UTC
    return resolve_time_contract(request.META.get(_META_CONTRACT))


def time_to_stored(value: time_type, offset_minutes: int, contract: str) -> time_type:
    """Время из запроса → значение для БД (локальное)."""
    if value is None or contract == CONTRACT_LEGACY:
        return value
    return shift_time_by_minutes(value, offset_minutes)


def time_to_wire(value: time_type, offset_minutes: int, contract: str) -> time_type:
    """Значение из БД (локальное) → время для ответа."""
    if value is None or contract == CONTRACT_LEGACY:
        return value
    return shift_time_by_minutes(value, -offset_minutes)


def parse_app_version(raw):
    """``'0.1.0+5'`` → ``(0, 1, 0, 5)``; пустое или некорректное значение → ``None``."""
    if not raw:
        return None
    parts = re.split(r'[.+\-]', raw.strip())
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return None


def is_event_path(path: str) -> bool:
    return bool(_EVENT_PATH_RE.match(path))


def log_time_contract_usage(request, response):
    """Учёт режимов контракта (основа метрики и алерта).

    * ``INFO``: каждый запрос к событиям — режим и версия приложения (доля legacy-запросов).
    * ``WARNING``: запрос без заголовка от версии приложения не ниже
      ``settings.TIME_CONTRACT_MIN_APP_VERSION`` — «забыли заголовок».
    """
    if not is_event_path(request.path):
        return

    mode = get_time_contract(request)
    raw_version = request.META.get(_META_APP_VERSION, '')
    extra = {
        'time_contract': mode,
        'app_version': raw_version,
        'path': request.path,
        'method': request.method,
        'status': response.status_code,
    }

    required = parse_app_version(getattr(settings, 'TIME_CONTRACT_MIN_APP_VERSION', ''))
    version = parse_app_version(raw_version)
    if mode == CONTRACT_LEGACY and required is not None and version is not None and version >= required:
        logger.warning(
            'time_contract_missing app_version=%s path=%s', raw_version, request.path, extra=extra
        )
        return

    logger.info(
        'time_contract mode=%s app_version=%s path=%s', mode, raw_version or '-', request.path, extra=extra
    )
