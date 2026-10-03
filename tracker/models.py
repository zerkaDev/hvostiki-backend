import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator, MinValueValidator, FileExtensionValidator
from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.utils import timezone
import uuid


class UserManager(BaseUserManager):
    def create_user(self, phone_number, password=None, **extra_fields):
        if not phone_number:
            raise ValueError('Phone number is required')

        user = self.model(phone_number=phone_number, **extra_fields)

        if password:
            user.set_password(password)  # Устанавливаем пароль
        else:
            user.set_unusable_password()  # Без пароля (не для суперпользователей!)

        user.save(using=self._db)
        return user

    def create_superuser(self, phone_number, password=None, **extra_fields):
        """
        Создает и возвращает суперпользователя.
        ВАЖНО: password должен быть обязательно для Django Admin!
        """
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)
        extra_fields.setdefault('is_verified', True)

        # Проверяем оба флага
        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')

        # Убедимся, что суперпользователь всегда имеет пароль для админки
        if password is None:
            # Можно сгенерировать случайный пароль или использовать дефолтный
            import secrets
            password = secrets.token_urlsafe(12)  # Генерация случайного пароля
            print(f'⚠️  Warning: No password provided for superuser. Generated: {password}')

        # Создаем пользователя с паролем
        user = self.create_user(
            phone_number=phone_number,
            password=password,  # Всегда передаем пароль!
            **extra_fields
        )

        return user

# Валидатор для номера телефона
phone_validator = RegexValidator(
    regex=r'^7\d{10}$',
    message='Номер телефона должен начинаться с 7 и содержать 11 цифр. Пример: 79051234567',
    code='invalid_phone'
)


def user_avatar_path(instance, filename):
    """Путь аватара: ``avatars/<user_uuid>/<uuid>.jpg`` (без персональных данных в имени)."""
    return f'avatars/{instance.pk}/{uuid.uuid4().hex}.jpg'


class User(AbstractBaseUser, PermissionsMixin):
    """Основная модель пользователя со всеми данными"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    phone_number = models.CharField(max_length=20, unique=True, validators=[phone_validator])

    # Профиль
    name = models.CharField('Имя', max_length=50, blank=True, default='')
    avatar = models.ImageField('Фото профиля', upload_to=user_avatar_path, null=True, blank=True)

    # Статусы
    is_active = models.BooleanField(default=True)
    is_verified = models.BooleanField(default=False)  # Подтвержден ли номер
    is_staff = models.BooleanField(default=False)  # Подтвержден ли номер

    # Код подтверждения
    confirmation_code = models.CharField(max_length=6, blank=True)
    code_sent_at = models.DateTimeField(null=True, blank=True)
    code_attempts = models.IntegerField(default=0)  # Количество попыток ввода кода

    # Даты
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_login = models.DateTimeField(null=True, blank=True)

    objects = UserManager()

    USERNAME_FIELD = 'phone_number'
    REQUIRED_FIELDS = []

    def __str__(self):
        return self.phone_number

    @property
    def is_code_expired(self):
        """Проверяет, не истек ли срок действия кода (5 минут)"""
        if not self.code_sent_at:
            return True
        expiration_time = self.code_sent_at + datetime.timedelta(minutes=5)
        return timezone.now() > expiration_time

    def increment_code_attempts(self):
        """Увеличивает счетчик попыток ввода кода"""
        self.code_attempts += 1
        self.save(update_fields=['code_attempts'])

    def reset_code_attempts(self):
        """Сбрасывает счетчик попыток"""
        self.code_attempts = 0
        self.save(update_fields=['code_attempts'])


class GenderChoices(models.TextChoices):
    MALE = ('M', 'Male')
    FEMALE = ('F', 'Female')


class PetType(models.TextChoices):
    CAT = 'cat', 'Кошка'
    DOG = 'dog', 'Собака'


class EventTypeChoices(models.TextChoices):
    DEWORMING = 'deworming', 'Deworming'
    YEARLY_VACCINATION = 'yearlyVaccination', 'Yearly vaccination'
    RABIES_VACCINATION = 'rabiesVaccination', 'Rabies vaccination'
    WEEKLY_PILLS = 'weeklyPills', 'Weekly pills'
    DAILY_PILLS = 'dailyPills', 'Daily pills'
    GROOMING = 'grooming', 'Grooming'
    BATHING = 'bathing', 'Bathing'
    WALKING = 'walking', 'Walking'
    FEEDING = 'feeding', 'Feeding'
    NAIL_TRIMMING = 'nailTrimming', 'Nail trimming'
    FLEA_TREATMENT = 'fleaTreatment', 'Flea treatment'
    VET_VISIT = 'vetVisit', 'Vet visit'
    CUSTOM = 'custom', 'Custom'


class EventNotificationType(models.TextChoices):
    STANDARD = 'standard', 'Standard'
    REMINDER = 'reminder', 'Reminder (day before)'
    FINAL = 'final', 'Final (day of)'


class Breed(models.Model):
    name = models.CharField(max_length=200, verbose_name='Название')
    type = models.CharField(
        max_length=10,
        choices=PetType.choices,
        verbose_name='Вид животного'
    )

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = 'Порода'
        verbose_name_plural = 'Породы'


class Pet(models.Model):
    """Модель для питомца"""

    owner = models.ForeignKey(User, related_name='pets', on_delete=models.CASCADE, verbose_name='Хозяин')

    name = models.CharField(
        max_length=100,
        verbose_name='Кличка',
        help_text='Введите кличку питомца'
    )

    pet_type = models.CharField(
        max_length=10,
        choices=PetType.choices,
        verbose_name='Вид животного'
    )

    breed = models.ForeignKey(Breed, related_name='pets', on_delete=models.CASCADE)

    weight = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
        verbose_name='Вес (кг)',
        help_text='Вес в килограммах'
    )

    birthday = models.DateField(blank=True, null=True)

    color = models.CharField(
        max_length=50,
        verbose_name='Окрас'
    )

    gender = models.CharField(choices=GenderChoices.choices, max_length=1, null=True, blank=True)
    has_castration = models.BooleanField(default=False)

    image = models.ImageField(
        upload_to='pets/%Y/%m/%d/',
        verbose_name='Фотография',
        blank=True,
        null=True,
        help_text='Загрузите фото питомца',
        validators=[FileExtensionValidator(
            allowed_extensions=['jpg', 'jpeg', 'png', 'heic', 'heif']
        )]
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.get_pet_type_display()} {self.name}'

    class Meta:
        verbose_name = 'Питомец'
        verbose_name_plural = 'Питомцы'
        ordering = ['-created_at']


class RecurrenceFrequency(models.TextChoices):
    DAILY = 'daily', 'Daily'
    WEEKLY = 'weekly', 'Weekly'
    MONTHLY = 'monthly', 'Monthly'
    YEARLY = 'yearly', 'Yearly'


class RecurrenceRule(models.Model):
    """Правило повторения события (семантика вхождений — в :mod:`tracker.recurrence`).

    Заполняются только поля выбранного периода; остальные ``NULL`` (сериализатор их обнуляет).
    Окончание — ``end_date`` либо ``end_count`` (не оба); ``until`` — вычисляемая последняя дата
    (для ``end_count`` — дата N-го вхождения-дня), нужна SQL-предфильтрам выдачи и рассылки.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    frequency = models.CharField(
        max_length=10,
        choices=RecurrenceFrequency.choices,
    )
    interval = models.PositiveIntegerField(default=1)

    week_days = models.JSONField(blank=True, null=True)   # [1,4]
    month_days = models.JSONField(blank=True, null=True)  # [5,20], -1 = последний день месяца
    year_dates = models.JSONField(blank=True, null=True)  # [{"month": 3, "day": 15}]
    # Несколько времён в день (только daily, ≥2 значений): ["08:00", "14:00"], локальное время события.
    # При одном времени поле пустое, время хранится в Event.time.
    times = models.JSONField(blank=True, null=True)

    # Окончание: «до даты» (end_date) либо «после N повторений» (end_count); не оба сразу.
    end_date = models.DateField(blank=True, null=True)
    end_count = models.PositiveIntegerField(blank=True, null=True)
    # Эффективная последняя дата: end_date либо дата N-го вхождения-дня. Кэш для SQL-предфильтра
    # (рассылки) — пересчитывается при каждом сохранении правила/даты старта события.
    until = models.DateField(blank=True, null=True, db_index=True)

    def clean(self):
        """Проверка инвариантов правила (админка/shell обходят DRF-сериализатор)."""
        from tracker.recurrence import RuleError, normalize_rule
        try:
            normalize_rule({
                'frequency': self.frequency, 'interval': self.interval,
                'week_days': self.week_days, 'month_days': self.month_days,
                'year_dates': self.year_dates, 'end_date': self.end_date, 'end_count': self.end_count,
            })
        except RuleError as exc:
            raise ValidationError(exc.message)

    def __str__(self):
        return f'{self.frequency}'

    class Meta:
        verbose_name = 'Правило расписания'
        verbose_name_plural = 'Правила расписаний'


class Event(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(User, related_name='events', on_delete=models.CASCADE, verbose_name='Пользователь')

    pet = models.ForeignKey(
        Pet,
        on_delete=models.CASCADE,
        related_name='events',
    )

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    start_date = models.DateField()
    time = models.TimeField(null=True, blank=True)
    timezone_offset = models.IntegerField(default=0)

    is_recurring = models.BooleanField(default=False)

    recurrence = models.ForeignKey(
        RecurrenceRule,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='events',
    )

    done = models.BooleanField(default=False)
    type = models.CharField(
        max_length=32,
        choices=EventTypeChoices.choices,
        default=EventTypeChoices.CUSTOM,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.is_recurring and not self.recurrence:
            raise ValidationError('Recurring event must have recurrence rule')

        if not self.is_recurring and self.recurrence:
            raise ValidationError('Non-recurring event must not have recurrence rule')

    def __str__(self):
        return self.title


class EventNotificationLog(models.Model):
    """Журнал отправленных уведомлений: защита от дублей по ``(событие, дата, слот, тип)``."""
    event = models.ForeignKey('Event', on_delete=models.CASCADE)
    occurrence_date = models.DateField()
    notification_type = models.CharField(
        max_length=16,
        choices=EventNotificationType.choices,
        default=EventNotificationType.STANDARD
    )
    # Слот времени при нескольких временах в день; NULL — обычное событие (одно время / весь день)
    occurrence_time = models.TimeField(null=True, blank=True)
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['event', 'occurrence_date', 'occurrence_time', 'notification_type'],
                nulls_distinct=False,
                name='uniq_notification_slot',
            ),
        ]


class EventCompletion(models.Model):
    """Отметка «выполнено» на конкретное вхождение: дата и, при нескольких временах в день, слот.

    ``occurrence_time = NULL`` — одно время / весь день (как раньше); в многослотовом режиме такая
    отметка относится к первому слоту.
    """
    event = models.ForeignKey('Event', on_delete=models.CASCADE)
    occurrence_date = models.DateField()
    # Слот времени при нескольких временах в день; NULL — одно время / весь день (как раньше)
    occurrence_time = models.TimeField(null=True, blank=True)
    done_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['event', 'occurrence_date', 'occurrence_time'],
                nulls_distinct=False,
                name='uniq_completion_slot',
            ),
        ]


class FCMDevice(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='fcm_devices')
    fcm_token = models.TextField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'FCM устройство'
        verbose_name_plural = 'FCM устройства'


class NotificationSettings(models.Model):
    """Настройки уведомлений пользователя. Хранятся только отключённые категории:
    по умолчанию (и пока записи нет) включено всё, новые категории включаются сами."""
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='notification_settings'
    )
    disabled_categories = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Настройки уведомлений'
        verbose_name_plural = 'Настройки уведомлений'

    def __str__(self):
        return f'Настройки уведомлений {self.user_id}'


def feedback_screenshot_path(instance, filename):
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'jpg'
    return f'feedback/{timezone.now():%Y/%m/%d}/{uuid.uuid4().hex}.{ext}'


class FeedbackTopic(models.TextChoices):
    PROBLEM = 'problem', 'Проблема'
    IDEA = 'idea', 'Идея'
    QUESTION = 'question', 'Вопрос'


class Feedback(models.Model):
    """Обращение пользователя из раздела «Помощь и обратная связь»."""
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='feedbacks'
    )
    topic = models.CharField(max_length=20, choices=FeedbackTopic.choices)
    message = models.TextField()
    screenshot = models.ImageField(upload_to=feedback_screenshot_path, null=True, blank=True)

    # Техническая информация, добавляется приложением автоматически
    app_version = models.CharField(max_length=32, blank=True)
    build_number = models.CharField(max_length=32, blank=True)
    platform = models.CharField(max_length=20, blank=True)
    os_version = models.CharField(max_length=64, blank=True)
    device_model = models.CharField(max_length=100, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Обращение'
        verbose_name_plural = 'Обращения'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_topic_display()} от {self.created_at:%d.%m.%Y %H:%M}'
