"""Категории уведомлений раздела «Профиль → Уведомления» и их соответствие типам событий.

Категории совпадают с группами типов в форме создания события: здоровье, уход, повседневные дела
и другое. Каждый тип события относится ровно к одной категории.
"""
from tracker.models import EventTypeChoices

CATEGORY_EVENT_TYPES = {
    'health': (
        EventTypeChoices.VET_VISIT,
        EventTypeChoices.DAILY_PILLS,
        EventTypeChoices.WEEKLY_PILLS,
        EventTypeChoices.YEARLY_VACCINATION,
        EventTypeChoices.RABIES_VACCINATION,
        EventTypeChoices.FLEA_TREATMENT,
        EventTypeChoices.DEWORMING,
    ),
    'care': (
        EventTypeChoices.GROOMING,
        EventTypeChoices.BATHING,
        EventTypeChoices.NAIL_TRIMMING,
    ),
    'daily': (
        EventTypeChoices.WALKING,
        EventTypeChoices.FEEDING,
    ),
    'other': (EventTypeChoices.CUSTOM,),
}

CATEGORIES = tuple(CATEGORY_EVENT_TYPES)

# Прежние категории (до 0029) и новые, в которые они вошли: нужны для переноса сохранённых настроек.
LEGACY_CATEGORY_TARGETS = {
    'walks': 'daily',
    'feeding': 'daily',
    'medications': 'health',
    'vaccinations': 'health',
    'vet_visits': 'health',
}

_EVENT_TYPE_TO_CATEGORY = {
    str(event_type): category
    for category, event_types in CATEGORY_EVENT_TYPES.items()
    for event_type in event_types
}


def category_for_event_type(event_type):
    """Категория для типа события или ``None`` для неизвестного типа (уведомляем всегда)."""
    return _EVENT_TYPE_TO_CATEGORY.get(str(event_type))
