"""Удаление аккаунта: одноразовый код подтверждения и полная очистка данных.

Код для удаления хранится отдельно от кода входа (в кэше, не в модели User),
живёт 5 минут, имеет свой счётчик попыток и никогда не попадает в логи.
"""
import logging
import secrets

from django.conf import settings
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.db import transaction
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from tracker.models import Pet
from tracker.tasks import send_confirmation_code

logger = logging.getLogger(__name__)

CODE_TTL = 5 * 60
RESEND_TIMEOUT = 60
MAX_ATTEMPTS = 5


class CodeAlreadySent(Exception):
    """Код уже отправлен меньше минуты назад."""


class InvalidDeletionCode(Exception):
    """Код не найден, истёк, неверен или попытки исчерпаны."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _key(kind, user):
    return f'account_delete_{kind}_{user.pk}'


def send_deletion_code(user):
    """Генерирует код и отправляет его звонком на номер пользователя."""
    if cache.get(_key('sent', user)):
        raise CodeAlreadySent

    code = str(secrets.randbelow(9000) + 1000)
    cache.set(_key('code', user), code, timeout=CODE_TTL)
    cache.set(_key('attempts', user), 0, timeout=CODE_TTL)
    cache.set(_key('sent', user), True, timeout=RESEND_TIMEOUT)

    if not settings.DEBUG:
        send_confirmation_code.delay(user.phone_number, code)
    logger.info('Код удаления аккаунта отправлен пользователю %s', user.pk)


def check_deletion_code(user, code):
    """Проверяет код; при ошибке бросает ``InvalidDeletionCode``."""
    expected = cache.get(_key('code', user))
    if settings.DEBUG and settings.DEBUG_CONFIRMATION_CODE:
        expected = expected and settings.DEBUG_CONFIRMATION_CODE
    if not expected:
        raise InvalidDeletionCode(
            'Код не найден или срок его действия истёк. Запросите новый код.'
        )

    attempts = cache.get(_key('attempts', user)) or 0
    if attempts >= MAX_ATTEMPTS:
        _forget_code(user)
        raise InvalidDeletionCode('Превышено количество попыток. Запросите новый код.')

    if not secrets.compare_digest(str(code), str(expected)):
        attempts += 1
        cache.set(_key('attempts', user), attempts, timeout=CODE_TTL)
        if attempts >= MAX_ATTEMPTS:
            _forget_code(user)
            raise InvalidDeletionCode('Превышено количество попыток. Запросите новый код.')
        raise InvalidDeletionCode(f'Неверный код. Осталось попыток: {MAX_ATTEMPTS - attempts}')


def _forget_code(user):
    cache.delete_many([_key('code', user), _key('attempts', user)])


def delete_account(user):
    """Безвозвратно удаляет пользователя, его токены и файлы."""
    with transaction.atomic():
        files = [pet.image.name for pet in Pet.objects.filter(owner=user) if pet.image]
        if user.avatar:
            files.append(user.avatar.name)

        for token in OutstandingToken.objects.filter(user=user):
            BlacklistedToken.objects.get_or_create(token=token)

        user.delete()
        transaction.on_commit(lambda: _delete_files(files))

    cache.delete_many([_key('code', user), _key('attempts', user), _key('sent', user)])


def _delete_files(names):
    for name in names:
        try:
            default_storage.delete(name)
        except Exception:  # файл уже удалён или хранилище недоступно — аккаунт всё равно удалён
            logger.warning('Не удалось удалить файл %s', name, exc_info=True)
