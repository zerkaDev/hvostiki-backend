# tracker/backends.py
from django.contrib.auth.backends import BaseBackend
from django.contrib.auth import get_user_model
from django.db.models import Q

User = get_user_model()


class PhoneBackend(BaseBackend):
    """
    Аутентификация по номеру телефона для Django Admin.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        # Пытаемся найти пользователя по номеру телефона
        try:
            user = User.objects.get(
                Q(phone_number=username) |
                Q(email=username) |
                Q(username=username) if hasattr(User, 'username') else Q(phone_number=username)
            )

            # Проверяем пароль
            if user.check_password(password):
                return user
            elif not user.has_usable_password() and password == '':
                # Для пользователей без пароля (только телефонная аутентификация)
                # Разрешаем вход с пустым паролем в админке (только для разработки!)
                if user.is_staff or user.is_superuser:
                    return user

        except User.DoesNotExist:
            return None

        return None

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None