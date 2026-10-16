"""Модели управления событием: отмена вхождения и правка одного вхождения."""
import datetime

import pytest
from django.db import IntegrityError, transaction

from tracker.models import Event, EventCancellation, EventOccurrenceOverride

DAY = datetime.date(2026, 10, 15)


@pytest.mark.django_db
class TestEventCancellation:
    def test_unique_per_slot(self, event):
        EventCancellation.objects.create(event=event, occurrence_date=DAY, occurrence_time=datetime.time(9, 0))
        EventCancellation.objects.create(event=event, occurrence_date=DAY, occurrence_time=datetime.time(18, 0))

        with pytest.raises(IntegrityError), transaction.atomic():
            EventCancellation.objects.create(event=event, occurrence_date=DAY, occurrence_time=datetime.time(9, 0))

    def test_unique_without_slot(self, event):
        """NULL-слот тоже уникален (nulls_distinct=False), как у отметок выполнения."""
        EventCancellation.objects.create(event=event, occurrence_date=DAY)

        with pytest.raises(IntegrityError), transaction.atomic():
            EventCancellation.objects.create(event=event, occurrence_date=DAY)

    def test_deleted_with_event(self, event):
        EventCancellation.objects.create(event=event, occurrence_date=DAY)

        event.delete()

        assert not EventCancellation.objects.exists()


@pytest.mark.django_db
class TestEventOccurrenceOverride:
    def test_fields_are_optional(self, event):
        override = EventOccurrenceOverride.objects.create(event=event, occurrence_date=DAY, time=datetime.time(8, 30))

        override.refresh_from_db()
        assert override.title is None
        assert override.description is None
        assert override.type is None
        assert override.time == datetime.time(8, 30)

    def test_unique_per_slot(self, event):
        EventOccurrenceOverride.objects.create(event=event, occurrence_date=DAY)

        with pytest.raises(IntegrityError), transaction.atomic():
            EventOccurrenceOverride.objects.create(event=event, occurrence_date=DAY)

    def test_deleted_with_event(self, event):
        EventOccurrenceOverride.objects.create(event=event, occurrence_date=DAY, title='Другое')

        Event.objects.filter(pk=event.pk).delete()

        assert not EventOccurrenceOverride.objects.exists()


@pytest.mark.django_db
def test_admin_pages_open(admin_client, event):
    from django.urls import reverse

    EventCancellation.objects.create(event=event, occurrence_date=DAY)
    EventOccurrenceOverride.objects.create(event=event, occurrence_date=DAY, title='Своё')

    for name in ('eventcancellation', 'eventoccurrenceoverride'):
        assert admin_client.get(reverse(f'admin:tracker_{name}_changelist')).status_code == 200
