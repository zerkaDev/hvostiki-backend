"""Журнал работы приложения, приложенный к обращению.

Приложение присылает gzip-файл с текстовыми записями. Содержимое не разбирается и
не выполняется: проверяется только, что это корректный gzip, а размер после
распаковки укладывается в лимит (защита от «gzip-бомбы»). Распаковка потоковая,
в памяти одновременно держится не больше одного блока.
"""
import logging
import zlib

from tracker.storage import private_storage

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 64 * 1024


class InvalidLogsArchive(Exception):
    """Файл не является корректным (целым) gzip-архивом."""


class LogsTooLarge(Exception):
    """Размер журнала после распаковки превышает лимит."""


def check_gzip(file, max_unpacked_bytes):
    """Проверяет gzip-вложение, не загружая его целиком в память.

    После проверки возвращает указатель файла в начало.
    """
    decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
    total = 0
    file.seek(0)
    try:
        while chunk := file.read(_CHUNK_SIZE):
            data = chunk
            while data:
                unpacked = decompressor.decompress(data, _CHUNK_SIZE)
                total += len(unpacked)
                if total > max_unpacked_bytes:
                    raise LogsTooLarge
                data = decompressor.unconsumed_tail
    except zlib.error as error:
        raise InvalidLogsArchive from error
    finally:
        file.seek(0)

    if not decompressor.eof:  # архив оборван
        raise InvalidLogsArchive


def delete_files(names):
    """Удаляет файлы журналов из приватного хранилища (сбой одного не мешает остальным)."""
    storage = private_storage()
    for name in names:
        if not name:
            continue
        try:
            storage.delete(name)
        except Exception:  # файл уже удалён или хранилище недоступно
            logger.warning('Не удалось удалить журнал %s', name, exc_info=True)
