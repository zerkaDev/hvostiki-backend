"""Категории уведомлений раздела «Профиль → Уведомления» и их соответствие типам событий.

Типы событий, которых нет в соответствии (груминг, купание, стрижка когтей, custom),
не относятся ни к одной категории и отключаться не могут — уведомления по ним приходят всегда.
"""
from tracker.models import EventTypeChoices

CATEGORY_EVENT_TYPES = {
    'walks': (EventTypeChoices.WALKING,),
    'feeding': (EventTypeChoices.FEEDING,),
    'medications': (
        EventTypeChoices.DAILY_PILLS,
        EventTypeChoices.WEEKLY_PILLS,
        EventTypeChoices.DEWORMING,
        EventTypeChoices.FLEA_TREATMENT,
    ),
    'vaccinations': (
        EventTypeChoices.YEARLY_VACCINATION,
        EventTypeChoices.RABIES_VACCINATION,
    ),
    'vet_visits': (EventTypeChoices.VET_VISIT,),
}

CATEGORIES = tuple(CATEGORY_EVENT_TYPES)

_EVENT_TYPE_TO_CATEGORY = {
    str(event_type): category
    for category, event_types in CATEGORY_EVENT_TYPES.items()
    for event_type in event_types
}


def category_for_event_type(event_type):
    """Категория для типа события или ``None``, если тип нельзя отключить."""
    return _EVENT_TYPE_TO_CATEGORY.get(str(event_type))
