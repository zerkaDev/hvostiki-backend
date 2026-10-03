"""Исправление исторических значений ``Event.time`` (контракт времени, поток B).

До этой миграции приложение присылало локальное время как UTC, а сервер прибавлял
``timezone_offset`` ещё раз: событие «17:05» (UTC+3) лежало в БД как 20:05, и пуш уходил
в 20:05 по местному времени. Приложение при этом показывало 17:05 (сдвиг применялся и при
записи, и при чтении). Миграция возвращает инвариант «в БД всегда локальное время»:
``time = time - timezone_offset``. Обратная миграция прибавляет offset обратно.

Все существующие строки считаются созданными приложением (других клиентов у API нет).

ВАЖНО: выполнять в одном окне с деплоем кода (UTC на проводе) и при остановленных
старых web/celery (иначе строки, созданные старым кодом после миграции, останутся сдвинутыми,
а созданные новым кодом — будут сдвинуты повторно).
"""
from datetime import date, datetime, timedelta

from django.db import migrations

BATCH_SIZE = 500


def _shift(value, minutes):
    base = datetime.combine(date(2000, 1, 1), value)
    return (base + timedelta(minutes=minutes)).time()


def _shift_all(apps, sign):
    Event = apps.get_model('tracker', 'Event')
    # Событий без времени и с нулевым смещением трогать не нужно
    queryset = (
        Event.objects.exclude(time__isnull=True)
        .exclude(timezone_offset=0)
        .only('id', 'time', 'timezone_offset')
    )

    batch = []
    for event in queryset.iterator(chunk_size=BATCH_SIZE):
        event.time = _shift(event.time, sign * event.timezone_offset)
        batch.append(event)
        if len(batch) >= BATCH_SIZE:
            Event.objects.bulk_update(batch, ['time'])
            batch = []
    if batch:
        # bulk_update не трогает updated_at (auto_now): сдвиг — не правка пользователя
        Event.objects.bulk_update(batch, ['time'])


def to_local(apps, schema_editor):
    """Было local + offset → станет local."""
    _shift_all(apps, sign=-1)


def to_legacy(apps, schema_editor):
    """Обратное преобразование: local → local + offset."""
    _shift_all(apps, sign=+1)


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0017_add_mixed_breed'),
    ]

    operations = [
        migrations.RunPython(to_local, to_legacy),
    ]
