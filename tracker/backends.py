from django.conf import settings
from django.contrib.auth.backends import BaseBackend
from django.contrib.auth import get_user_model
from django.db.models import Q

User = get_user_model()


class PhoneBackend(BaseBackend):
    """
    Аутентификация по номеру телефона для Django Admin.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username:
            return None

        # Собираем условия явно: у модели нет ни username, ни email,
        # но если появятся — поиск будет учитывать и их.
        query = Q(phone_number=username)
        if hasattr(User, 'username'):
            query |= Q(username=username)
        if hasattr(User, 'email'):
            query |= Q(email=username)

        try:
            user = User.objects.get(query)
        except (User.DoesNotExist, User.MultipleObjectsReturned):
            return None

        if user.check_password(password):
            return user

        # Вход staff-пользователя без пароля с пустым паролем разрешён
        # только локально: в проде это открыло бы доступ в админку.
        if (
            settings.DEBUG
            and not user.has_usable_password()
            and not password
            and (user.is_staff or user.is_superuser)
        ):
            return user

        return None

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None