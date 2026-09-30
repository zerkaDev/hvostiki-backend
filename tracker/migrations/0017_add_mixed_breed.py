from django.db import migrations

MIXED_BREED = 'Метис или не знаю'


def add_mixed_breed(apps, schema_editor):
    """Добавляет породу «Метис или не знаю» для собак и кошек."""
    Breed = apps.get_model('tracker', 'Breed')

    for pet_type in ('dog', 'cat'):
        Breed.objects.get_or_create(name=MIXED_BREED, type=pet_type)


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0016_fcmdevice'),
    ]

    operations = [
        # Откат намеренно не удаляет породу: у Pet.breed стоит on_delete=CASCADE,
        # и удаление породы каскадом снесло бы питомцев, выбравших этот вариант.
        migrations.RunPython(add_mixed_breed, migrations.RunPython.noop),
    ]
