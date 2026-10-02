"""Годовые повторения и окончание по числу повторений.

Добавляет ``yearly``, ``year_dates``, ``end_count``, ``until`` (+ заполнение ``until`` из ``end_date``).
"""
from django.db import migrations, models
from django.db.models import F


def fill_until(apps, schema_editor):
    """Для существующих правил с ``end_date`` кэш ``until`` равен этой дате."""
    RecurrenceRule = apps.get_model('tracker', 'RecurrenceRule')
    RecurrenceRule.objects.filter(end_date__isnull=False).update(until=F('end_date'))


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0018_event_time_to_local'),
    ]

    operations = [
        migrations.AlterField(
            model_name='recurrencerule',
            name='frequency',
            field=models.CharField(
                choices=[('daily', 'Daily'), ('weekly', 'Weekly'), ('monthly', 'Monthly'), ('yearly', 'Yearly')],
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='recurrencerule',
            name='year_dates',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='recurrencerule',
            name='end_count',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='recurrencerule',
            name='until',
            field=models.DateField(blank=True, db_index=True, null=True),
        ),
        migrations.AlterField(
            model_name='recurrencerule',
            name='month_days',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.RunPython(fill_until, migrations.RunPython.noop),
    ]
