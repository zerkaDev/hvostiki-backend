from django.conf import settings
from django.db import transaction
from django.db.models import Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import APIException, ErrorDetail
from tracker.recurrence import (
    END_TYPES, RuleError, check_end_against_start, derive_end_type, normalize_rule, normalize_times,
    parse_stored_times, recompute_until, rule_from_normalized,
)
from tracker.models import DevicePlatform, Feedback, Notification, NotificationSettings, User, Pet, Breed, RecurrenceRule, Event, RecurrenceFrequency, EventCompletion

from .event_time import time_to_stored, time_to_wire
from .notification_categories import CATEGORIES
from .services import feedback_logs
from .services.avatar import process_avatar

MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024
from .utils import normalize_phone


class PhoneNumberSerializer(serializers.Serializer):
    """Сериализатор для запроса отправки кода"""
    phone_number = serializers.CharField(max_length=20, required=True)

    def validate_phone_number(self, value):
        """Базовая валидация номера телефона"""
        # Убираем все нецифровые символы
        cleaned = ''.join(c for c in value if c.isdigit())
        
        # Нормализуем перед проверкой длины (8 -> 7 и т.д.)
        normalized = normalize_phone(cleaned)

        # Проверка на соответствие формату 7XXXXXXXXXX (11 цифр)
        if len(normalized) != 11 or not normalized.startswith('7'):
            raise serializers.ValidationError('Номер телефона должен быть в формате 7XXXXXXXXXX')
            
        return normalized


class VerifyCodeSerializer(serializers.Serializer):
    """Сериализатор для проверки кода подтверждения"""
    phone_number = serializers.CharField(max_length=20, required=True)
    code = serializers.CharField(max_length=6, required=True)

    def validate(self, data):
        phone_number = normalize_phone(data['phone_number'])
        code = data['code']

        try:
            user = User.objects.get(phone_number=phone_number)
        except User.DoesNotExist:
            raise serializers.ValidationError({'phone_number': 'Пользователь с таким номером не найден'})

        # Проверяем, не истек ли код
        if user.is_code_expired:
            raise serializers.ValidationError({
                'code': 'Срок действия кода истек. Запросите новый код.'
            })

        # Проверяем количество попыток
        if user.code_attempts >= 5:
            raise serializers.ValidationError({
                'code': 'Превышено количество попыток. Запросите новый код.'
            })

        # Код для локальной разработки задаётся в .env (DEBUG_CONFIRMATION_CODE).
        # Без него принимается только реально отправленный код — так включённый
        # по ошибке DEBUG не превращается во вход по любому номеру.
        expected_code = user.confirmation_code
        if settings.DEBUG and settings.DEBUG_CONFIRMATION_CODE:
            expected_code = settings.DEBUG_CONFIRMATION_CODE
        
        if code != expected_code:
            user.increment_code_attempts()
            attempts_left = 5 - user.code_attempts
            raise serializers.ValidationError({
                'code': f'Неверный код. Осталось попыток: {max(0, attempts_left)}'
            })

        # Код верный - сохраняем пользователя в валидированных данных
        # Обновление состояния пользователя (is_verified) лучше делать в представлении
        # после успешной валидации всех полей.
        data['user'] = user
        return data


class DeleteAccountSerializer(serializers.Serializer):
    """Подтверждение удаления аккаунта кодом из звонка"""
    code = serializers.CharField(max_length=6)


def notification_settings_for(user):
    """Карта ``категория -> включена`` (без записи в БД — всё включено)."""
    settings_row = NotificationSettings.objects.filter(user=user).first()
    disabled = set(settings_row.disabled_categories) if settings_row else set()
    return {category: category not in disabled for category in CATEGORIES}


class NotificationSettingsSerializer(serializers.Serializer):
    """Включённость категорий уведомлений. PATCH принимает любое подмножество ключей."""
    walks = serializers.BooleanField(required=False)
    feeding = serializers.BooleanField(required=False)
    medications = serializers.BooleanField(required=False)
    vaccinations = serializers.BooleanField(required=False)
    vet_visits = serializers.BooleanField(required=False)

    def to_representation(self, instance):
        return notification_settings_for(instance)

    def update(self, user, validated_data):
        row, _ = NotificationSettings.objects.get_or_create(user=user)
        disabled = set(row.disabled_categories)
        for category, enabled in validated_data.items():
            if enabled:
                disabled.discard(category)
            else:
                disabled.add(category)
        row.disabled_categories = [c for c in CATEGORIES if c in disabled]
        row.save(update_fields=['disabled_categories', 'updated_at'])
        return user


class UserSerializer(serializers.ModelSerializer):
    """Профиль пользователя. Номер телефона только для чтения — его нельзя сменить."""

    name = serializers.CharField(
        max_length=50, required=False, allow_blank=True, trim_whitespace=True
    )
    avatar = serializers.ImageField(required=False, allow_null=False)
    notification_settings = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id',
            'phone_number',
            'name',
            'avatar',
            'notification_settings',
            'is_verified',
            'created_at',
        ]
        read_only_fields = ['id', 'phone_number', 'is_verified', 'created_at']

    @extend_schema_field(NotificationSettingsSerializer)
    def get_notification_settings(self, user):
        return notification_settings_for(user)

    def validate_avatar(self, value):
        return process_avatar(value)

    def update(self, instance, validated_data):
        old_avatar = instance.avatar.name if instance.avatar else None
        instance = super().update(instance, validated_data)
        new_avatar = instance.avatar.name if instance.avatar else None
        if old_avatar and old_avatar != new_avatar:
            storage = instance.avatar.storage
            transaction.on_commit(lambda: storage.delete(old_avatar))
        return instance


class BreedSerializer(serializers.ModelSerializer):
    class Meta:
        model = Breed
        fields = ('id', 'name', 'type')


class PetSerializer(serializers.ModelSerializer):
    """Сериализатор для работы с питомцами (просмотр и создание)"""
    owner_id = serializers.ReadOnlyField(source='owner.id')
    # Используем PrimaryKeyRelatedField для записи и BreedSerializer для чтения (через to_representation)
    breed = serializers.PrimaryKeyRelatedField(queryset=Breed.objects.all())
    # coerce_to_string=False: отдаём вес числом (25.5), а не строкой "25.50"
    weight = serializers.DecimalField(max_digits=5, decimal_places=2, coerce_to_string=False)

    class Meta:
        model = Pet
        fields = [
            'id',
            'owner_id',
            'name',
            'pet_type',
            'breed',
            'weight',
            'birthday',
            'gender',
            'color',
            'has_castration',
            'image',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at', 'owner_id']

    def validate_weight(self, value):
        """Проверка веса"""
        if value <= 0:
            raise serializers.ValidationError('Вес должен быть положительным числом')
        if value > 200:  # Максимальный вес 200 кг (для больших собак)
            raise serializers.ValidationError('Вес не может превышать 200 кг')
        return value

    def to_representation(self, instance):
        """Возвращаем полный объект породы при чтении"""
        representation = super().to_representation(instance)
        representation['breed_obj'] = BreedSerializer(instance.breed).data
        return representation


class PetCreateSerializer(PetSerializer):
    """Оставлен для совместимости с вьюсетами, если требуется другое поведение"""
    pass


class TokenResponseSerializer(serializers.Serializer):
    refresh = serializers.CharField(help_text='Refresh token для получения нового access token')
    access = serializers.CharField(help_text='Access token для аутентификации запросов')
    access_expires = serializers.IntegerField(help_text='Время истечения access token (Unix timestamp)')
    refresh_expires = serializers.IntegerField(help_text='Время истечения refresh token (Unix timestamp)')


class ErrorResponseSerializer(serializers.Serializer):
    detail = serializers.CharField(help_text='Описание ошибки')
    code = serializers.CharField(required=False, help_text='Код ошибки')


class RefreshTokenSerializer(serializers.Serializer):
    refresh = serializers.CharField(help_text='Refresh token obtained during authentication')


class DeviceRegistrationSerializer(serializers.Serializer):
    fcm_token = serializers.CharField(required=True)
    platform = serializers.ChoiceField(
        choices=DevicePlatform.choices, required=False,
        help_text='Платформа устройства: ios или android',
    )


class DeviceUnregistrationSerializer(serializers.Serializer):
    fcm_token = serializers.CharField(required=True)


class RecurrenceRuleSerializer(serializers.ModelSerializer):
    """Правило повторения во вложенном поле ``recurrence``.

    Здесь только разбор полей; согласованность (границы, обнуление чужих периодов, окончание,
    слоты) проверяет :meth:`EventSerializer.validate` по итоговому состоянию правила. ``end_type``
    в ответе вычисляется из ``end_date`` / ``end_count``.
    """
    # Вычисляется из end_date / end_count; на вход можно передать явно (never/date/count)
    end_type = serializers.ChoiceField(choices=END_TYPES, required=False)
    # «HH:MM»; на проводе — UTC, в БД — локальное время события
    times = serializers.ListField(
        child=serializers.TimeField(), required=False, allow_null=True, allow_empty=True,
    )

    class Meta:
        model = RecurrenceRule
        fields = (
            'frequency',
            'interval',
            'week_days',
            'month_days',
            'year_dates',
            'times',
            'end_type',
            'end_date',
            'end_count',
        )
        extra_kwargs = {
            # границы по периодам и согласованность полей проверяет EventSerializer.validate
            # (по итоговому состоянию правила и дате старта события)
            'interval': {'min_value': 1, 'required': False},
            'end_count': {'min_value': 0},
        }

    def to_representation(self, instance):
        rep = super().to_representation(instance)
        rep['end_type'] = derive_end_type(instance.end_date, instance.end_count)
        return rep


class EventSerializer(serializers.ModelSerializer):
    """Событие питомца с вложенным правилом повторения.

    Время на проводе — UTC, в БД — локальное (``tracker/event_time.py``). ``done`` вычисляется по
    вхождению из контекста (``occurrence_date``, ``occurrence_time``, набор ``completed``). В списках
    (``include_pet_obj = False``) ``pet_obj`` не добавляется.
    """
    recurrence = RecurrenceRuleSerializer(required=False, allow_null=True)
    # В запросах ждём time в UTC+0 и timezone_offset (минуты).
    timezone_offset = serializers.IntegerField(required=False)

    done = serializers.SerializerMethodField()

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_done(self, obj):
        """
        Проверяет выполнено ли событие на конкретную дату.
        """
        occurrence_date = self.context.get('occurrence_date')

        if occurrence_date is None:
            occurrence_date = obj.start_date

        # Слот времени вхождения (только при нескольких временах в день), иначе None
        slot = self.context.get('occurrence_time')
        # Старая отметка без времени (NULL) в многослотовом режиме относится к первому слоту
        legacy_slot = slot is not None and slot == obj.time

        completed = self.context.get('completed')
        if completed is not None:
            return (obj.id, occurrence_date, slot) in completed or (
                legacy_slot and (obj.id, occurrence_date, None) in completed
            )

        query = EventCompletion.objects.filter(event=obj, occurrence_date=occurrence_date)
        if slot is None:
            return query.filter(occurrence_time__isnull=True).exists()
        flt = Q(occurrence_time=slot) | (Q(occurrence_time__isnull=True) if legacy_slot else Q())
        return query.filter(flt).exists()

    class Meta:
        model = Event
        fields = (
            'id',
            'pet',
            'title',
            'description',
            'start_date',
            'time',
            'timezone_offset',
            'is_recurring',
            'recurrence',
            'type',
            'done',
            'created_at',
            'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')
        extra_kwargs = {
            'time': {'required': False, 'allow_null': True}
        }

    def validate_timezone_offset(self, value):
        # Реалистичный диапазон часовых поясов: [-14:00, +14:00]
        if value < -14 * 60 or value > 14 * 60:
            raise serializers.ValidationError('timezone_offset out of range.')
        return value

    def validate(self, data):
        """Переводит время в локальное и проверяет правило повторения по итоговому состоянию.

        Итоговое состояние = текущее правило события + переданные поля (PATCH), поэтому проверки
        работают и при смене только ``start_date`` или периода. Ошибки — 400 с кодом
        (``end_before_start``, ``end_before_first`` и др.).
        """
        # В базе время всегда локальное (UTC + timezone_offset). Что присылает клиент —
        # API принимает время в UTC — переводим в локальное.
        if self.instance is None and data.get('timezone_offset') is None:
            raise serializers.ValidationError(
                {'timezone_offset': 'timezone_offset is required (minutes offset relative to UTC).'}
            )

        effective_offset = data.get('timezone_offset')
        if effective_offset is None and self.instance is not None:
            effective_offset = self.instance.timezone_offset

        if effective_offset is not None and 'time' in data and data['time'] is not None:
            data['time'] = time_to_stored(data['time'], effective_offset)

        is_recurring = data.get('is_recurring', self.instance.is_recurring if self.instance else False)
        recurrence = data.get('recurrence')

        if is_recurring and not recurrence and not (self.instance and self.instance.recurrence):
            raise serializers.ValidationError('Recurring event must include recurrence')

        if not is_recurring and recurrence:
            raise serializers.ValidationError('Non-recurring event must not include recurrence')

        self._rule_state = None
        if is_recurring:
            start_date = data.get('start_date') or (self.instance.start_date if self.instance else None)
            self._rule_state = self._validate_rule(recurrence, start_date, data, effective_offset)

        return data

    def _validate_rule(self, patch, start_date, data, offset):
        """Итоговое состояние правила (instance + patch): нормализация и проверка окончания."""
        current = self.instance.recurrence if self.instance else None
        merged = {}
        if current is not None:
            merged = {
                'frequency': current.frequency, 'interval': current.interval,
                'week_days': current.week_days, 'month_days': current.month_days,
                'year_dates': current.year_dates, 'end_date': current.end_date,
                'end_count': current.end_count,
            }
        patch = dict(patch or {})
        # явно переданное окончание вытесняет прежнее другого вида
        if patch.get('end_date') is not None and 'end_count' not in patch:
            merged['end_count'] = None
        if patch.get('end_count') is not None and 'end_date' not in patch:
            merged['end_date'] = None
        merged.update(patch)
        try:
            state = normalize_rule(merged)
            check_end_against_start(rule_from_normalized(state), start_date)
            self._apply_times(state, patch, current, data, offset)
        except RuleError as exc:
            raise serializers.ValidationError({
                exc.field or 'recurrence': [ErrorDetail(exc.message, code=exc.code)],
            })
        return state

    def _apply_times(self, state, patch, current, data, offset):
        """Слоты времени: в БД — локальные; ``Event.time`` согласуется с первым слотом."""
        from_patch = patch.get('times') is not None
        if from_patch:
            raw = [time_to_stored(t, offset) for t in patch['times']]
        elif current is not None:
            raw = parse_stored_times(current.times)
        else:
            raw = []

        event_time = data['time'] if 'time' in data else (self.instance.time if self.instance else None)
        times, new_time = normalize_times(state['frequency'], raw, event_time)

        if times and not from_patch and 'time' in data and data['time'].replace(second=0, microsecond=0) != new_time:
            raise RuleError(
                'time_conflicts_with_times',
                'Время события задаётся списком времён в правиле (recurrence.times)',
                field='time',
            )
        state['times'] = times
        if raw and state['frequency'] == 'daily':
            data['time'] = new_time

    def to_representation(self, instance):
        """Время слота (из контекста) или события отдаётся в UTC; ``recurrence.times`` — тоже в UTC."""
        rep = super().to_representation(instance)

        # Отдаём время в UTC+0.
        # В списках вхождение может относиться к слоту (несколько времён в день) — его время в контексте.
        shown_time = self.context.get('slot_time', instance.time)
        if shown_time is not None:
            rep['time'] = time_to_wire(shown_time, instance.timezone_offset).isoformat()
        if rep.get('recurrence') and instance.recurrence and instance.recurrence.times:
            rep['recurrence']['times'] = [
                time_to_wire(t, instance.timezone_offset).strftime('%H:%M')
                for t in parse_stored_times(instance.recurrence.times)
            ]

        # В списках (/period/, /upcoming/) питомец не дублируется в каждом вхождении: клиент
        # берёт его по `pet` (id) из своего списка питомцев. В одиночных ответах pet_obj остаётся.
        if self.context.get('include_pet_obj', True):
            rep['pet_obj'] = PetSerializer(instance.pet, context=self.context).data
        return rep

    def create(self, validated_data):
        """Создаёт событие и правило; ``until`` вычисляется до сохранения."""
        recurrence_data = validated_data.pop('recurrence', None)

        if validated_data.get('is_recurring'):
            recurrence = RecurrenceRule(**self._rule_state)
            recompute_until(recurrence, validated_data['start_date'])
            recurrence.save()
            validated_data['recurrence'] = recurrence

        validated_data['user'] = self.context['request'].user
        return Event.objects.create(**validated_data)

    def update(self, instance, validated_data):
        """Обновляет событие и правило; ``until`` пересчитывается (в т.ч. при смене ``start_date``)."""
        recurrence_data = validated_data.pop('recurrence', None)

        # обновляем простые поля
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        # если событие стало recurring или обновило параметры повторения
        if instance.is_recurring:
            state = self._rule_state
            if instance.recurrence:
                for attr, value in state.items():
                    setattr(instance.recurrence, attr, value)
                recompute_until(instance.recurrence, instance.start_date)
                instance.recurrence.save()
            else:
                recurrence = RecurrenceRule(**state)
                recompute_until(recurrence, instance.start_date)
                recurrence.save()
                instance.recurrence = recurrence

        # если убрали recurring
        elif instance.recurrence:
            instance.recurrence.delete()
            instance.recurrence = None
            
        instance.save()
        return instance


class EventOccurrenceSerializer(serializers.Serializer):
    event_id = serializers.UUIDField()
    title = serializers.CharField()
    date = serializers.DateField()
    time = serializers.TimeField()
    pet_id = serializers.IntegerField()


class EventCompletionSerializer(serializers.ModelSerializer):
    class Meta:
        model = EventCompletion
        fields = ('event', 'occurrence_date', 'done_at')
        read_only_fields = ('done_at',)


class PayloadTooLarge(APIException):
    status_code = 413
    default_detail = 'Файл слишком большой.'
    default_code = 'payload_too_large'


class FeedbackSerializer(serializers.ModelSerializer):
    """Обращение из «Помощь и обратная связь»: скриншот (до 10 МБ) и журнал приложения
    (gzip, до 2 МБ, только на запись) необязательны."""
    message = serializers.CharField(max_length=2000, trim_whitespace=True)
    screenshot = serializers.ImageField(required=False, allow_null=True)
    logs = serializers.FileField(required=False, allow_null=True, write_only=True)

    class Meta:
        model = Feedback
        fields = [
            'id', 'topic', 'message', 'screenshot', 'logs',
            'app_version', 'build_number', 'platform', 'os_version', 'device_model',
        ]
        read_only_fields = ['id']
        extra_kwargs = {
            'app_version': {'required': False},
            'build_number': {'required': False},
            'platform': {'required': False},
            'os_version': {'required': False},
            'device_model': {'required': False},
        }

    def validate_screenshot(self, value):
        if value is not None and value.size > MAX_SCREENSHOT_BYTES:
            raise serializers.ValidationError('Файл слишком большой. Максимум — 10 МБ.')
        return value

    def validate_logs(self, value):
        if value is None:
            return value
        if value.size > settings.FEEDBACK_LOGS_MAX_BYTES:
            raise PayloadTooLarge('Журнал слишком большой.')
        try:
            feedback_logs.check_gzip(value, settings.FEEDBACK_LOGS_MAX_UNPACKED_BYTES)
        except feedback_logs.LogsTooLarge:
            raise PayloadTooLarge('Журнал слишком большой после распаковки.')
        except feedback_logs.InvalidLogsArchive:
            raise serializers.ValidationError('Журнал должен быть корректным gzip-файлом.')
        return value


class NotificationSerializer(serializers.ModelSerializer):
    """Элемент «Центра уведомлений». ``occurrence_time`` — в UTC (в БД хранится локальным)."""
    is_read = serializers.SerializerMethodField()
    occurrence_time = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = (
            'id', 'kind', 'title', 'body', 'created_at', 'is_read',
            'pet_id', 'event_id', 'event_type', 'notification_type',
            'occurrence_date', 'occurrence_time',
        )
        read_only_fields = fields

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_is_read(self, obj):
        return obj.read_at is not None

    # allow_null: у объявления и у события с одним временем в день слота нет — в ответе null
    @extend_schema_field({'type': 'string', 'nullable': True})
    def get_occurrence_time(self, obj):
        if obj.occurrence_time is None:
            return None
        offset = obj.event.timezone_offset if obj.event_id else 0
        return time_to_wire(obj.occurrence_time, offset).strftime('%H:%M')
