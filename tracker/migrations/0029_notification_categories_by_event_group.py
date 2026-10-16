from django.db import migrations

# Прежние категории -> новые группы типов события.
LEGACY = {
    'walks': 'daily',
    'feeding': 'daily',
    'medications': 'health',
    'vaccinations': 'health',
    'vet_visits': 'health',
}


def forwards(apps, schema_editor):
    """Новая категория выключена, только если выключены все прежние, которые в неё вошли."""
    NotificationSettings = apps.get_model('tracker', 'NotificationSettings')
    sources = {}
    for old, new in LEGACY.items():
        sources.setdefault(new, set()).add(old)

    for row in NotificationSettings.objects.exclude(disabled_categories=[]):
        disabled = set(row.disabled_categories or [])
        migrated = [new for new, olds in sources.items() if olds <= disabled]
        row.disabled_categories = [
            c for c in ('health', 'care', 'daily', 'other') if c in migrated or c in disabled
        ]
        row.save(update_fields=['disabled_categories'])


def backwards(apps, schema_editor):
    """Откат: группа превращается в прежние категории, которые в неё входили."""
    NotificationSettings = apps.get_model('tracker', 'NotificationSettings')
    for row in NotificationSettings.objects.exclude(disabled_categories=[]):
        disabled = set(row.disabled_categories or [])
        restored = [old for old, new in LEGACY.items() if new in disabled]
        row.disabled_categories = restored
        row.save(update_fields=['disabled_categories'])


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0028_event_reminders'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
