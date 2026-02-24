import datetime

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
            print(f"⚠️  Warning: No password provided for superuser. Generated: {password}")

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


class User(AbstractBaseUser, PermissionsMixin):
    """Основная модель пользователя со всеми данными"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    phone_number = models.CharField(max_length=20, unique=True, validators=[phone_validator])

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
        validators=[MinValueValidator(0.01)],
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

    @property
    def age_with_label(self):
        """Возвращает возраст с правильным окончанием"""
        if 10 <= self.age % 100 <= 20:
            years_label = 'лет'
        elif self.age % 10 == 1:
            years_label = 'год'
        elif 2 <= self.age % 10 <= 4:
            years_label = 'года'
        else:
            years_label = 'лет'
        return f'{self.age} {years_label}'

    def __str__(self):
        return f'{self.get_pet_type_display()} {self.name}'

    class Meta:
        verbose_name = 'Питомец'
        verbose_name_plural = 'Питомцы'
        ordering = ['-created_at']


class RecurrenceFrequency(models.TextChoices):
    DAILY = "daily", "Daily"
    WEEKLY = "weekly", "Weekly"
    MONTHLY = "monthly", "Monthly"


class RecurrenceRule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    frequency = models.CharField(
        max_length=10,
        choices=RecurrenceFrequency.choices,
    )
    interval = models.PositiveIntegerField(default=1)

    week_days = models.JSONField(blank=True, null=True)   # [1,4]
    month_days = models.JSONField(blank=True, null=True)  # [5,20]

    end_date = models.DateField(blank=True, null=True)

    def clean(self):
        if self.frequency == RecurrenceFrequency.WEEKLY and not self.week_days:
            raise ValidationError("week_days required for weekly recurrence")

        if self.frequency == RecurrenceFrequency.MONTHLY and not self.month_days:
            raise ValidationError("month_days required for monthly recurrence")

    def __str__(self):
        return f"{self.frequency}"

    class Meta:
        verbose_name = 'Правило расписания'
        verbose_name_plural = 'Правила расписаний'


class Event(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(User, related_name='events', on_delete=models.CASCADE, verbose_name='Пользователь')

    pet = models.ForeignKey(
        Pet,
        on_delete=models.CASCADE,
        related_name="events",
    )

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    start_date = models.DateField()
    time = models.TimeField()
    timezone_offset = models.IntegerField(default=0)

    is_recurring = models.BooleanField(default=False)

    recurrence = models.ForeignKey(
        RecurrenceRule,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="events",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.is_recurring and not self.recurrence:
            raise ValidationError("Recurring event must have recurrence rule")

        if not self.is_recurring and self.recurrence:
            raise ValidationError("Non-recurring event must not have recurrence rule")

    def __str__(self):
        return self.title


class EventNotificationLog(models.Model):
    event = models.ForeignKey("Event", on_delete=models.CASCADE)
    occurrence_date = models.DateField()
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("event", "occurrence_date")
