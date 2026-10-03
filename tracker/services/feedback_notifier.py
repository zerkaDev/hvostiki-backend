"""Доставка обращений пользователей команде.

Канал доставки подключается настройкой ``FEEDBACK_NOTIFIER`` (путь к классу).
Сейчас это заглушка ``LogNotifier``; чтобы подключить Telegram-бота, достаточно
написать класс с методом ``send(feedback)`` (например, ``TelegramNotifier``)
и указать его в ``FEEDBACK_NOTIFIER`` — остальной код менять не нужно.
"""
import logging

from django.conf import settings
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)


class FeedbackNotifier:
    """Интерфейс канала доставки. ``send`` должен бросить исключение при сбое."""

    def send(self, feedback):
        raise NotImplementedError


class LogNotifier(FeedbackNotifier):
    """Заглушка: пишет в лог только id и тему (без текста и персональных данных)."""

    def send(self, feedback):
        logger.info('Новое обращение #%s, тема: %s', feedback.pk, feedback.topic)


def get_notifier() -> FeedbackNotifier:
    return import_string(settings.FEEDBACK_NOTIFIER)()
