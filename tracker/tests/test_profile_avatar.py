import io
import os

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from tracker.models import User


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


def make_image(fmt='PNG', size=(40, 30), mode='RGB', name='photo.png'):
    buffer = io.BytesIO()
    Image.new(mode, size, (200, 10, 10)).save(buffer, format=fmt)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type='image/png')


@pytest.mark.django_db
class TestAvatarUpload:
    def test_upload_converts_to_jpeg_and_returns_absolute_url(self, auth_client, user, media_root):
        response = auth_client.patch(
            reverse('profile'), {'avatar': make_image()}, format='multipart'
        )

        assert response.status_code == 200
        assert response.data['avatar'].startswith('http')
        user.refresh_from_db()
        assert user.avatar.name.startswith(f'avatars/{user.pk}/')
        assert user.avatar.name.endswith('.jpg')
        with Image.open(user.avatar.path) as saved:
            assert saved.format == 'JPEG'

    def test_large_image_is_downscaled(self, auth_client, user):
        auth_client.patch(
            reverse('profile'),
            {'avatar': make_image(size=(3000, 1500))},
            format='multipart',
        )
        user.refresh_from_db()
        with Image.open(user.avatar.path) as saved:
            assert max(saved.size) == 1024

    def test_rgba_png_is_flattened(self, auth_client, user):
        response = auth_client.patch(
            reverse('profile'),
            {'avatar': make_image(mode='RGBA')},
            format='multipart',
        )
        assert response.status_code == 200

    def test_replacing_avatar_deletes_old_file(
        self, auth_client, user, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            auth_client.patch(reverse('profile'), {'avatar': make_image()}, format='multipart')
        user.refresh_from_db()
        old_path = user.avatar.path
        assert os.path.exists(old_path)

        with django_capture_on_commit_callbacks(execute=True):
            response = auth_client.patch(
                reverse('profile'), {'avatar': make_image()}, format='multipart'
            )
        assert response.status_code == 200
        user.refresh_from_db()
        assert user.avatar.path != old_path
        assert not os.path.exists(old_path)
        assert os.path.exists(user.avatar.path)

    def test_not_an_image_rejected(self, auth_client, user):
        fake = SimpleUploadedFile('a.png', b'not an image', content_type='image/png')
        response = auth_client.patch(reverse('profile'), {'avatar': fake}, format='multipart')

        assert response.status_code == 400
        assert 'avatar' in response.data
        user.refresh_from_db()
        assert not user.avatar

    def test_too_large_file_rejected(self, auth_client, monkeypatch):
        monkeypatch.setattr('tracker.services.avatar.MAX_AVATAR_BYTES', 10)
        response = auth_client.patch(
            reverse('profile'), {'avatar': make_image()}, format='multipart'
        )
        assert response.status_code == 400
        assert 'avatar' in response.data

    def test_name_and_avatar_in_one_request(self, auth_client, user):
        response = auth_client.patch(
            reverse('profile'),
            {'name': 'Анна', 'avatar': make_image()},
            format='multipart',
        )
        assert response.status_code == 200
        assert response.data['name'] == 'Анна'
        assert response.data['avatar']


@pytest.mark.django_db
class TestAvatarDelete:
    def test_delete_removes_file_and_is_idempotent(
        self, auth_client, user, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            auth_client.patch(reverse('profile'), {'avatar': make_image()}, format='multipart')
        user.refresh_from_db()
        path = user.avatar.path

        with django_capture_on_commit_callbacks(execute=True):
            response = auth_client.delete(reverse('profile-avatar'))
        assert response.status_code == 204
        assert not os.path.exists(path)
        user.refresh_from_db()
        assert not user.avatar

        assert auth_client.delete(reverse('profile-avatar')).status_code == 204
        assert auth_client.get(reverse('profile')).data['avatar'] is None

    def test_requires_auth(self, api_client):
        assert api_client.delete(reverse('profile-avatar')).status_code == 401
