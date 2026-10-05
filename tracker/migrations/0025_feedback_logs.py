import tracker.models
import tracker.storage
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0024_fcmdevice_platform'),
    ]

    operations = [
        migrations.AddField(
            model_name='feedback',
            name='logs',
            field=models.FileField(blank=True, null=True, storage=tracker.storage.private_storage, upload_to=tracker.models.feedback_logs_path),
        ),
    ]
