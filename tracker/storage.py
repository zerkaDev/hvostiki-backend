"""Приватное хранилище для файлов, которые нельзя отдавать по публичному URL.

Сейчас так хранятся журналы приложения, приложенные к обращениям. Каталог
``PRIVATE_MEDIA_ROOT`` не пересекается с ``MEDIA_ROOT`` и никогда не раздаётся
через ``/media/``; у файла нет URL, получить содержимое можно только из кода
(например, через скачивание в админке с проверкой прав).

Для переезда на S3-совместимое хранилище достаточно задать ``PRIVATE_STORAGE_BACKEND``
(бакет без публичного доступа); код работает только через storage API.
"""
import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage, storages


class PrivateFileSystemStorage(FileSystemStorage):
    """Файловая система в ``PRIVATE_MEDIA_ROOT``, без URL."""

    @property
    def base_location(self):
        return settings.PRIVATE_MEDIA_ROOT

    @property
    def location(self):
        return os.path.abspath(self.base_location)

    @property
    def base_url(self):
        # ``url()`` у FileSystemStorage при base_url=None бросает ValueError.
        return None


def private_storage():
    """Хранилище для ``FileField(storage=...)`` (вызывается один раз при загрузке моделей)."""
    return storages['private']
