import gzip
import logging
import os
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from tracker.models import Feedback, User
from tracker.services import account_deletion
from tracker.tasks import delete_expired_feedback_logs

PAYLOAD = {'topic': 'problem', 'message': 'Не загружается расписание'}
LOG_TEXT = '12:03:41.535 I NET  → #42 GET /pets/3/\n12:03:41.719 I NET  ← #42 200 184ms\n'


@pytest.fixture(autouse=True)
def roots(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / 'media')
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / 'private')


def gz_file(text=LOG_TEXT, name='tails-log-8f2c.log.gz', content_type='application/gzip'):
    return SimpleUploadedFile(name, gzip.compress(text.encode('utf-8')), content_type=content_type)


def post(client, logs=None, **extra):
    data = {**PAYLOAD, **extra}
    if logs is not None:
        data['logs'] = logs
    with patch('tracker.views.deliver_feedback'):
        return client.post(reverse('feedback'), data, format='multipart')


def make_feedback(user, text=LOG_TEXT, age_days=0):
    feedback = Feedback.objects.create(user=user, topic='problem', message='x')
    feedback.logs.save('any.log.gz', ContentFile(gzip.compress(text.encode())), save=True)
    if age_days:
        Feedback.objects.filter(pk=feedback.pk).update(
            created_at=timezone.now() - timedelta(days=age_days)
        )
    feedback.refresh_from_db()
    return feedback


@pytest.mark.django_db
class TestLogsUpload:
    def test_without_logs_still_works(self, auth_client):
        response = post(auth_client)
        assert response.status_code == 201
        assert not Feedback.objects.get().logs

    def test_logs_are_stored_privately(self, auth_client, settings):
        response = post(auth_client, gz_file())

        assert response.status_code == 201
        assert 'logs' not in response.data
        feedback = Feedback.objects.get()
        assert feedback.logs.name.startswith('feedback_logs/')
        assert feedback.logs.name.endswith('.log.gz')
        with feedback.logs.open('rb') as stored:
            assert gzip.decompress(stored.read()).decode() == LOG_TEXT
        assert os.path.exists(os.path.join(settings.PRIVATE_MEDIA_ROOT, feedback.logs.name))
        assert not os.path.exists(os.path.join(settings.MEDIA_ROOT, feedback.logs.name))

    def test_logs_have_no_public_url(self, auth_client):
        post(auth_client, gz_file())
        with pytest.raises(ValueError):
            Feedback.objects.get().logs.url

    def test_storage_is_private_alias(self):
        assert storages['private'].base_url is None

    def test_client_filename_is_not_used(self, auth_client):
        post(auth_client, gz_file(name='../../evil.log.gz'))
        name = Feedback.objects.get().logs.name
        assert '..' not in name and 'evil' not in name

    def test_content_type_is_not_trusted(self, auth_client):
        response = post(auth_client, gz_file(content_type='application/octet-stream'))
        assert response.status_code == 201

    def test_not_gzip_rejected(self, auth_client):
        response = post(auth_client, SimpleUploadedFile('a.log.gz', b'plain text, not gzip'))
        assert response.status_code == 400
        assert 'logs' in response.data
        assert Feedback.objects.count() == 0

    def test_truncated_gzip_rejected(self, auth_client):
        data = gzip.compress(('строка журнала\n' * 5000).encode())
        response = post(auth_client, SimpleUploadedFile('a.log.gz', data[: len(data) // 2]))
        assert response.status_code == 400
        assert Feedback.objects.count() == 0

    def test_empty_file_rejected(self, auth_client):
        response = post(auth_client, SimpleUploadedFile('a.log.gz', b''))
        assert response.status_code == 400
        assert Feedback.objects.count() == 0

    def test_too_large_compressed_returns_413(self, auth_client, settings):
        settings.FEEDBACK_LOGS_MAX_BYTES = 50
        response = post(auth_client, gz_file(text=os.urandom(2000).hex()))
        assert response.status_code == 413
        assert Feedback.objects.count() == 0

    def test_gzip_bomb_returns_413(self, auth_client, settings):
        settings.FEEDBACK_LOGS_MAX_UNPACKED_BYTES = 1024 * 1024
        bomb = SimpleUploadedFile('a.log.gz', gzip.compress(b'\0' * (5 * 1024 * 1024)))
        assert bomb.size < settings.FEEDBACK_LOGS_MAX_BYTES  # маленький до распаковки

        response = post(auth_client, bomb)

        assert response.status_code == 413
        assert Feedback.objects.count() == 0

    def test_file_at_unpacked_limit_is_accepted(self, auth_client, settings):
        settings.FEEDBACK_LOGS_MAX_UNPACKED_BYTES = 1024
        assert post(auth_client, gz_file(text='a' * 1024)).status_code == 201

    def test_content_is_not_logged(self, auth_client, caplog):
        caplog.set_level(logging.DEBUG)
        post(auth_client, gz_file(text='СЕКРЕТНАЯ-СТРОКА'))
        assert 'СЕКРЕТНАЯ-СТРОКА' not in caplog.text

    def test_requires_auth(self, api_client):
        assert post(api_client, gz_file()).status_code == 401


@pytest.mark.django_db
class TestLogsRetention:
    def test_expired_logs_deleted_and_feedback_kept(self, user, settings):
        old = make_feedback(user, age_days=settings.FEEDBACK_LOGS_RETENTION_DAYS + 1)
        old_path = os.path.join(settings.PRIVATE_MEDIA_ROOT, old.logs.name)
        assert os.path.exists(old_path)

        delete_expired_feedback_logs()

        old.refresh_from_db()
        assert not old.logs
        assert not os.path.exists(old_path)
        assert Feedback.objects.filter(pk=old.pk).exists()

    def test_recent_logs_kept(self, user, settings):
        fresh = make_feedback(user, age_days=settings.FEEDBACK_LOGS_RETENTION_DAYS - 1)
        path = os.path.join(settings.PRIVATE_MEDIA_ROOT, fresh.logs.name)

        delete_expired_feedback_logs()

        fresh.refresh_from_db()
        assert fresh.logs
        assert os.path.exists(path)

    def test_missing_file_does_not_break_task(self, user, settings):
        old = make_feedback(user, age_days=settings.FEEDBACK_LOGS_RETENTION_DAYS + 1)
        os.remove(os.path.join(settings.PRIVATE_MEDIA_ROOT, old.logs.name))

        delete_expired_feedback_logs()

        old.refresh_from_db()
        assert not old.logs

    def test_retention_days_setting_is_respected(self, user, settings):
        settings.FEEDBACK_LOGS_RETENTION_DAYS = 3
        feedback = make_feedback(user, age_days=4)
        delete_expired_feedback_logs()
        feedback.refresh_from_db()
        assert not feedback.logs

    def test_task_registered_in_beat(self):
        from django.conf import settings as django_settings
        tasks = {entry['task'] for entry in django_settings.CELERY_BEAT_SCHEDULE.values()}
        assert 'tracker.tasks.delete_expired_feedback_logs' in tasks


@pytest.mark.django_db
class TestLogsAccountDeletion:
    def test_account_deletion_removes_logs_but_keeps_feedback(
        self, user, settings, django_capture_on_commit_callbacks
    ):
        feedback = make_feedback(user)
        other = User.objects.create_user(phone_number='79005554433')
        others_feedback = make_feedback(other)
        path = os.path.join(settings.PRIVATE_MEDIA_ROOT, feedback.logs.name)
        others_path = os.path.join(settings.PRIVATE_MEDIA_ROOT, others_feedback.logs.name)

        with django_capture_on_commit_callbacks(execute=True):
            account_deletion.delete_account(user)

        feedback.refresh_from_db()
        assert feedback.user is None
        assert not feedback.logs
        assert not os.path.exists(path)
        assert os.path.exists(others_path)  # чужой журнал не тронут


@pytest.mark.django_db
class TestLogsAdminDownload:
    @pytest.fixture
    def staff(self, db):
        return User.objects.create_superuser(phone_number='79990000001', password='pass-12345')

    def url(self, feedback):
        return reverse('admin:tracker_feedback_logs', args=[feedback.pk])

    def test_staff_downloads_attachment(self, client, staff, user, caplog):
        feedback = make_feedback(user)
        client.force_login(staff)
        caplog.set_level(logging.INFO)

        response = client.get(self.url(feedback))

        assert response.status_code == 200
        assert response['Content-Type'] == 'application/gzip'
        assert 'attachment' in response['Content-Disposition']
        assert 'no-store' in response['Cache-Control']
        assert gzip.decompress(b''.join(response.streaming_content)).decode() == LOG_TEXT
        assert f'#{feedback.pk}' in caplog.text and str(staff.pk) in caplog.text
        assert 'GET /pets' not in caplog.text  # содержимое в лог не попадает

    def test_regular_user_has_no_access(self, client, user):
        feedback = make_feedback(user)
        client.force_login(user)
        response = client.get(self.url(feedback))
        assert response.status_code in (302, 403)
        assert response.status_code != 200

    def test_anonymous_redirected_to_login(self, client, user):
        response = client.get(self.url(make_feedback(user)))
        assert response.status_code == 302
        assert 'login' in response['Location']

    def test_staff_without_view_permission_forbidden(self, client, user):
        staff = User.objects.create_user(phone_number='79990000002', is_staff=True)
        feedback = make_feedback(user)
        client.force_login(staff)
        assert client.get(self.url(feedback)).status_code == 403

    def test_missing_logs_returns_404(self, client, staff, user):
        feedback = Feedback.objects.create(user=user, topic='idea', message='x')
        client.force_login(staff)
        assert client.get(self.url(feedback)).status_code == 404

    def test_change_page_renders_with_private_logs(self, client, staff, user):
        feedback = make_feedback(user)
        client.force_login(staff)
        response = client.get(reverse('admin:tracker_feedback_change', args=[feedback.pk]))
        assert response.status_code == 200
        assert self.url(feedback) in response.content.decode()

    def test_admin_delete_removes_file(self, user, settings):
        from django.contrib import admin
        from tracker.admin import FeedbackAdmin

        feedback = make_feedback(user)
        path = os.path.join(settings.PRIVATE_MEDIA_ROOT, feedback.logs.name)

        FeedbackAdmin(Feedback, admin.site).delete_queryset(None, Feedback.objects.all())

        assert not Feedback.objects.exists()
        assert not os.path.exists(path)
