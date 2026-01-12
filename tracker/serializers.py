from django.conf import settings
from rest_framework import serializers
from django.utils import timezone
from .models import User

from .utils import normalize_phone


class PhoneNumberSerializer(serializers.Serializer):
    """Сериализатор для запроса отправки кода"""
    phone_number = serializers.CharField(max_length=20, required=True)

    def validate_phone_number(self, value):
        """Базовая валидация номера телефона"""
        # Убираем все нецифровые символы кроме +
        cleaned = ''.join(c for c in value if c.isdigit() or c == '+')

        # Простая проверка длины
        if len(cleaned) not in (12, 11, 10):
            raise serializers.ValidationError("Номер телефона невалидный")
        return normalize_phone(cleaned)


class VerifyCodeSerializer(serializers.Serializer):
    """Сериализатор для проверки кода подтверждения"""
    phone_number = serializers.CharField(max_length=20, required=True)
    code = serializers.CharField(max_length=6, required=True)

    def validate(self, data):
        phone_number = normalize_phone(data['phone_number'])
        code = data['code']

        user = User.objects.get(phone_number=phone_number)

        # Проверяем, не истек ли код
        if user.is_code_expired:
            raise serializers.ValidationError({
                "code": "Срок действия кода истек. Запросите новый код."
            })

        # Проверяем количество попыток
        if user.code_attempts >= 5:
            raise serializers.ValidationError({
                "code": "Превышено количество попыток. Запросите новый код."
            })

        # Проверяем код
        if not settings.DEBUG:
            if user.confirmation_code != code:
                user.increment_code_attempts()
                attempts_left = 5 - user.code_attempts
                raise serializers.ValidationError({
                    "code": f"Неверный код. Осталось попыток: {attempts_left}"
                })

        # Код верный
        user.reset_code_attempts()
        user.is_verified = True
        user.last_login = timezone.now()
        user.save()

        data['user'] = user

        return data


class UserSerializer(serializers.ModelSerializer):
    """Сериализатор для отображения данных пользователя"""

    class Meta:
        model = User
        fields = [
            'id',
            'phone_number',
            'is_verified',
            'created_at',
        ]
        read_only_fields = ['id', 'phone_number', 'is_verified', 'created_at']
