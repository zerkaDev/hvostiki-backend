from django.db import migrations


class Migration(migrations.Migration):
    """Сводит две ветки миграций: журналы обращений (#7) и центр уведомлений (#6).

    Обе миграции 0025 добавляли свои модели/поля независимо, поэтому после слияния
    веток в графе оказалось два leaf-узла. Схему эта миграция не меняет.
    """

    dependencies = [
        ('tracker', '0025_feedback_logs'),
        ('tracker', '0025_notification'),
    ]

    operations = [
    ]
