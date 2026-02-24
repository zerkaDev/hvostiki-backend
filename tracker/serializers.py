from django.conf import settings
from drf_spectacular.utils import extend_schema_serializer
from rest_framework import serializers
from django.utils import timezone
from tracker.models import User, Pet, Breed, RecurrenceRule, Event, RecurrenceFrequency

from .utils import normalize_phone, shift_time_by_minutes, timezone_offset_minutes


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
        if settings.DEBUG:
           if code != '1234':
                user.increment_code_attempts()
                attempts_left = 5 - user.code_attempts
                raise serializers.ValidationError({
                    "code": f"Неверный код. Осталось попыток: {attempts_left}"
                })
        else:
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


class BreedSerializer(serializers.ModelSerializer):
    class Meta:
        model = Breed
        fields = ("id", "name", "type")


class PetSerializer(serializers.ModelSerializer):
    owner_id = serializers.ReadOnlyField(source='owner.id')
    breed_obj = BreedSerializer(read_only=True, source='breed')

    class Meta:
        model = Pet
        fields = [
            'id',
            'owner_id',
            'name',
            'pet_type',
            'breed_obj',
            'weight',
            'birthday',
            'gender',
            'color',
            'has_castration',
            'image',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']

    def validate_weight(self, value):
        """Проверка веса"""
        if value <= 0:
            raise serializers.ValidationError("Вес должен быть положительным числом")
        if value > 200:  # Максимальный вес 200 кг (для больших собак)
            raise serializers.ValidationError("Вес не может превышать 200 кг")
        return value


class PetCreateSerializer(serializers.ModelSerializer):
    """Сериализатор для создания питомца (без read_only полей)"""

    class Meta:
        model = Pet
        fields = [
            'name',
            'pet_type',
            'breed',
            'weight',
            'color',
            'image',
            'birthday',
            'gender',
            'has_castration'
        ]


class TokenResponseSerializer(serializers.Serializer):
    refresh = serializers.CharField(help_text="Refresh token для получения нового access token")
    access = serializers.CharField(help_text="Access token для аутентификации запросов")
    access_expires = serializers.IntegerField(help_text="Время истечения access token (Unix timestamp)")
    refresh_expires = serializers.IntegerField(help_text="Время истечения refresh token (Unix timestamp)")


class ErrorResponseSerializer(serializers.Serializer):
    detail = serializers.CharField(help_text="Описание ошибки")
    code = serializers.CharField(required=False, help_text="Код ошибки")


class RefreshTokenSerializer(serializers.Serializer):
    refresh = serializers.CharField(help_text="Refresh token obtained during authentication")


class RecurrenceRuleSerializer(serializers.ModelSerializer):

    class Meta:
        model = RecurrenceRule
        fields = (
            "frequency",
            "interval",
            "week_days",
            "month_days",
            "end_date",
        )

    def validate(self, data):
        frequency = data.get("frequency")

        if frequency == RecurrenceFrequency.WEEKLY and not data.get("week_days"):
            raise serializers.ValidationError("week_days required for weekly recurrence")

        if frequency == RecurrenceFrequency.MONTHLY and not data.get("month_days"):
            raise serializers.ValidationError("month_days required for monthly recurrence")

        return data


class EventTimezoneOffsetField(serializers.Field):
    """
    Единое поле timezone_offset:
    - в ответе: int (минуты offset относительно UTC)
    - в запросе: int, кладём во validated_data как timezone_offset_minutes
    """

    def to_representation(self, event: Event):
        return timezone_offset_minutes(event.timezone, event.start_date, event.time)

    def to_internal_value(self, data):
        try:
            offset = int(data)
        except (TypeError, ValueError):
            raise serializers.ValidationError("timezone_offset must be an integer (minutes).")

        # Реалистичный диапазон часовых поясов: [-14:00, +14:00]
        if offset < -14 * 60 or offset > 14 * 60:
            raise serializers.ValidationError("timezone_offset out of range.")

        return {"timezone_offset_minutes": offset}


class EventSerializer(serializers.ModelSerializer):
    recurrence = RecurrenceRuleSerializer(required=False, allow_null=True)
    # В запросах ждём time в UTC+0 и timezone_offset (минуты).
    # timezone оставляем опционально для обратной совместимости со старым клиентом.
    timezone = serializers.CharField(max_length=64, write_only=True, required=False, allow_blank=True)
    timezone_offset = EventTimezoneOffsetField(source="*", required=False)
    timezoneOffset = EventTimezoneOffsetField(source="*", required=False, write_only=True)

    class Meta:
        model = Event
        fields = (
            "id",
            "pet",
            "title",
            "description",
            "start_date",
            "time",
            "timezone",
            "timezone_offset",
            "timezoneOffset",
            "is_recurring",
            "recurrence",
        )
        read_only_fields = ("id",)

    def validate(self, data):
        # Если клиент прислал timezone_offset — считаем, что time пришёл в UTC
        # и переводим его в локальное время для хранения (как было раньше).
        offset = data.pop("timezone_offset_minutes", None)
        if offset is not None:
            if "time" in data and data["time"] is not None:
                data["time"] = shift_time_by_minutes(data["time"], offset)
            data["timezone"] = str(offset)
        elif self.instance is None and not data.get("timezone"):
            raise serializers.ValidationError(
                {"timezone_offset": "timezone_offset is required (minutes offset relative to UTC)."}
            )

        is_recurring = data.get("is_recurring")
        recurrence = data.get("recurrence")

        if is_recurring and not recurrence:
            raise serializers.ValidationError("Recurring event must include recurrence")

        if not is_recurring and recurrence:
            raise serializers.ValidationError("Non-recurring event must not include recurrence")

        return data

    def to_representation(self, instance):
        rep = super().to_representation(instance)

        # Возвращаем time как UTC+0 (клиент затем сам применит timezone_offset для локального отображения)
        offset = timezone_offset_minutes(instance.timezone, instance.start_date, instance.time)
        if offset is not None and instance.time is not None:
            rep["time"] = shift_time_by_minutes(instance.time, -offset).isoformat()

        # timezone write_only, но на всякий случай не отдаём его даже если где-то проскочит
        rep.pop("timezone", None)
        return rep

    def create(self, validated_data):
        recurrence_data = validated_data.pop("recurrence", None)

        if validated_data.get("is_recurring"):
            recurrence = RecurrenceRule.objects.create(**recurrence_data)
            validated_data["recurrence"] = recurrence

        validated_data["user"] = self.context["request"].user
        return Event.objects.create(**validated_data)

    def update(self, instance, validated_data):
        recurrence_data = validated_data.pop("recurrence", None)

        # обновляем простые поля
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()

        # если событие стало recurring
        if instance.is_recurring:
            if instance.recurrence:
                for attr, value in recurrence_data.items():
                    setattr(instance.recurrence, attr, value)
                instance.recurrence.save()
            else:
                recurrence = RecurrenceRule.objects.create(**recurrence_data)
                instance.recurrence = recurrence
                instance.save()

        # если убрали recurring
        if not instance.is_recurring and instance.recurrence:
            instance.recurrence.delete()
            instance.recurrence = None
            instance.save()

        return instance


class EventOccurrenceSerializer(serializers.Serializer):
    event_id = serializers.UUIDField()
    title = serializers.CharField()
    date = serializers.DateField()
    time = serializers.TimeField()
    pet_id = serializers.UUIDField()
