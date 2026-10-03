import io
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from tracker.models import Feedback, User
from tracker.services.feedback_notifier import FeedbackNotifier, LogNotifier, get_notifier
from tracker.tasks import deliver_feedback


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)


def screenshot():
    buffer = io.BytesIO()
    Image.new('RGB', (30, 30)).save(buffer, format='PNG')
    return SimpleUploadedFile('shot.png', buffer.getvalue(), content_type='image/png')


PAYLOAD = {
    'topic': 'problem',
    'message': 'Не загружается расписание',
    'app_version': '1.2.0',
    'build_number': '45',
    'platform': 'ios',
    'os_version': '18.1',
    'device_model': 'iPhone 15',
}


@pytest.mark.django_db
class TestFeedbackApi:
    def test_create_json_schedules_delivery(
        self, auth_client, user, django_capture_on_commit_callbacks
    ):
        with patch('tracker.views.deliver_feedback') as task:
            with django_capture_on_commit_callbacks(execute=True):
                response = auth_client.post(reverse('feedback'), PAYLOAD, format='json')

        assert response.status_code == 201
        feedback = Feedback.objects.get()
        assert feedback.user == user
        assert feedback.topic == 'problem'
        assert feedback.device_model == 'iPhone 15'
        task.delay.assert_called_once_with(feedback.pk)

    def test_create_multipart_with_screenshot(self, auth_client):
        with patch('tracker.views.deliver_feedback'):
            response = auth_client.post(
                reverse('feedback'), {**PAYLOAD, 'screenshot': screenshot()}, format='multipart'
            )
        assert response.status_code == 201
        assert Feedback.objects.get().screenshot.name.startswith('feedback/')

    def test_technical_fields_optional(self, auth_client):
        with patch('tracker.views.deliver_feedback'):
            response = auth_client.post(
                reverse('feedback'), {'topic': 'idea', 'message': 'Тёмная тема'}, format='json'
            )
        assert response.status_code == 201

    @pytest.mark.parametrize('payload', [
        {'topic': 'other', 'message': 'x'},
        {'topic': 'idea', 'message': ''},
        {'topic': 'idea', 'message': '   '},
        {'topic': 'idea', 'message': 'я' * 2001},
        {'message': 'x'},
        {'topic': 'idea'},
    ])
    def test_validation(self, auth_client, payload):
        response = auth_client.post(reverse('feedback'), payload, format='json')
        assert response.status_code == 400
        assert Feedback.objects.count() == 0

    def test_not_an_image_screenshot_rejected(self, auth_client):
        fake = SimpleUploadedFile('a.png', b'nope', content_type='image/png')
        response = auth_client.post(
            reverse('feedback'), {**PAYLOAD, 'screenshot': fake}, format='multipart'
        )
        assert response.status_code == 400

    def test_too_large_screenshot_rejected(self, auth_client, monkeypatch):
        monkeypatch.setattr('tracker.serializers.MAX_SCREENSHOT_BYTES', 10)
        response = auth_client.post(
            reverse('feedback'), {**PAYLOAD, 'screenshot': screenshot()}, format='multipart'
        )
        assert response.status_code == 400
        assert 'screenshot' in response.data

    def test_requires_auth(self, api_client):
        assert api_client.post(reverse('feedback'), PAYLOAD, format='json').status_code == 401

    def test_rate_limit(self, auth_client):
        with patch('tracker.views.deliver_feedback'):
            statuses = [
                auth_client.post(reverse('feedback'), PAYLOAD, format='json').status_code
                for _ in range(6)
            ]
        assert statuses == [201] * 5 + [429]

    def test_feedback_survives_user_deletion(self, user):
        feedback = Feedback.objects.create(user=user, topic='idea', message='x')
        user.delete()
        feedback.refresh_from_db()
        assert feedback.user is None


@pytest.mark.django_db
class TestFeedbackDelivery:
    @pytest.fixture
    def feedback(self, user):
        return Feedback.objects.create(user=user, topic='question', message='секрет')

    def test_default_notifier_is_log_stub(self):
        assert isinstance(get_notifier(), LogNotifier)

    def test_log_notifier_does_not_log_message(self, feedback, caplog):
        caplog.set_level('INFO')
        LogNotifier().send(feedback)
        assert f'#{feedback.pk}' in caplog.text
        assert 'секрет' not in caplog.text

    def test_delivery_marks_delivered(self, feedback):
        deliver_feedback(feedback.pk)
        feedback.refresh_from_db()
        assert feedback.delivered_at is not None

    def test_custom_notifier_via_setting(self, feedback, settings):
        settings.FEEDBACK_NOTIFIER = f'{__name__}.RecordingNotifier'
        RecordingNotifier.sent.clear()
        deliver_feedback(feedback.pk)
        assert RecordingNotifier.sent == [feedback.pk]

    def test_already_delivered_is_skipped(self, feedback, settings):
        settings.FEEDBACK_NOTIFIER = f'{__name__}.RecordingNotifier'
        RecordingNotifier.sent.clear()
        deliver_feedback(feedback.pk)
        deliver_feedback(feedback.pk)
        assert RecordingNotifier.sent == [feedback.pk]

    def test_failure_leaves_undelivered_and_retries(self, feedback, settings):
        settings.FEEDBACK_NOTIFIER = f'{__name__}.FailingNotifier'
        with patch.object(deliver_feedback, 'retry', side_effect=RuntimeError('retry')) as retry:
            with pytest.raises(RuntimeError):
                deliver_feedback(feedback.pk)
        retry.assert_called_once()
        feedback.refresh_from_db()
        assert feedback.delivered_at is None


class RecordingNotifier(FeedbackNotifier):
    sent = []

    def send(self, feedback):
        self.sent.append(feedback.pk)


class FailingNotifier(FeedbackNotifier):
    def send(self, feedback):
        raise ConnectionError('telegram down')
