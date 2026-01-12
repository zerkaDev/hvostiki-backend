from django.conf import settings
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiExample
from rest_framework import status, permissions
from rest_framework.views import APIView
from rest_framework.response import Response
from django.utils import timezone
from django.core.cache import cache
import random
import logging

from rest_framework_simplejwt.tokens import RefreshToken

from tracker.models import User
from tracker.serializers import (
    PhoneNumberSerializer,
    VerifyCodeSerializer,
    UserSerializer,
)
from tracker.tasks import send_confirmation_code

logger = logging.getLogger(__name__)


class SendCodeView(APIView):
    """
        Отправка кода подтверждения на номер телефона

        Пользователь вводит номер → отправляем код → ждем подтверждения

        **Логика работы:**
        1. Проверяет, не отправлялся ли код в последнюю минуту
        2. Генерирует 4-значный код
        3. Создает/обновляет пользователя с кодом
        4. Отправляет код через SMS (в продакшене)
        5. Устанавливает таймаут для повторной отправки
        """
    permission_classes = [permissions.AllowAny]

    @extend_schema(
        summary="Отправка кода подтверждения",
        description="""
            Отправляет SMS с кодом подтверждения на указанный номер телефона.

            **Ограничения:**
            - Код можно запрашивать не чаще 1 раза в минуту
            - В режиме DEBUG код возвращается в ответе
            - В продакшене код отправляется через SMS сервис
            """,
        request=PhoneNumberSerializer,
        responses={
            200: OpenApiResponse(
                response=OpenApiTypes.OBJECT,
                description="Код успешно отправлен",
                examples=[
                    OpenApiExample(
                        "Успешная отправка",
                        value={
                            "message": "Код подтверждения отправлен",
                            "phone_number": "+79991234567",
                            "resend_timeout": 60
                        }
                    )
                ]
            ),
            400: OpenApiResponse(
                response=OpenApiTypes.OBJECT,
                description="Неверный формат номера телефона",
                examples=[
                    OpenApiExample(
                        "Ошибка валидации",
                        value={
                            "phone_number": [
                                "Введите корректный номер телефона."
                            ]
                        }
                    )
                ]
            ),
            429: OpenApiResponse(
                response=OpenApiTypes.OBJECT,
                description="Слишком много запросов",
                examples=[
                    OpenApiExample(
                        "Повторная отправка",
                        value={
                            "error": "Код уже отправлен. Попробуйте через 1 минуту."
                        }
                    )
                ]
            )
        },
        examples=[
            OpenApiExample(
                "Пример запроса",
                value={"phone_number": "+79991234567"},
                request_only=True
            )
        ]
    )
    def post(self, request):
        serializer = PhoneNumberSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        phone_number = serializer.validated_data['phone_number']

        cache_key = f"code_sent_{phone_number}"
        if cache.get(cache_key):
            return Response({
                "error": "Код уже отправлен. Попробуйте через 1 минуту."
            }, status=status.HTTP_429_TOO_MANY_REQUESTS)

        code = str(random.randint(1000, 9999))

        user = User.objects.filter(phone_number=phone_number).first()
        if not user:
            # Создаем неактивированного пользователя
             User.objects.create_user(
                phone_number=phone_number,
                confirmation_code=code,
                code_sent_at=timezone.now(),
                is_verified=False
            )
        else:
            # Обновляем код у существующего пользователя
            user.confirmation_code = code
            user.code_sent_at = timezone.now()
            user.code_attempts = 0
            user.save(update_fields=['confirmation_code', 'code_sent_at', 'code_attempts'])

        if not settings.DEBUG:
            send_confirmation_code.delay(phone_number, code)

        logger.info(f"Код {code} отправлен на номер {phone_number}")

        cache.set(cache_key, True, timeout=60)

        return Response({
            "message": "Код подтверждения отправлен",
            "phone_number": phone_number,
            "resend_timeout": 60
        }, status=status.HTTP_200_OK)



class VerifyCodeView(APIView):
    """
    Проверка кода подтверждения

    Пользователь вводит код → создаем/авторизуем пользователя → возвращаем токен

    **Логика работы:**
    1. Проверяет код через VerifyCodeSerializer
    2. Активирует пользователя (если был неактивен)
    3. Генерирует JWT токены (access и refresh)
    4. Возвращает токены с информацией об истечении
    """
    permission_classes = [permissions.AllowAny]

    @extend_schema(
        summary="Подтверждение кода",
        description="""
        Проверяет код подтверждения и возвращает JWT токены для аутентификации.
        
        **Особенности:**
        - Код действителен 10 минут
        - После 5 неудачных попыток аккаунт блокируется на 15 минут
        - При успешной проверке пользователь активируется (is_verified=True)
        - Для дебага можно ввести 1234 чтобы пройти проверку
        """,
        request=VerifyCodeSerializer,
        responses={
            200: OpenApiResponse(
                response=OpenApiTypes.OBJECT,
                description="Код подтвержден, токены получены",
                examples=[
                    OpenApiExample(
                        "Успешная аутентификация",
                        value={
                            "refresh": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
                            "access": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
                            "access_expires": 1736000000,
                            "refresh_expires": 1736000000
                        }
                    )
                ]
            ),
            400: OpenApiResponse(
                response=OpenApiTypes.OBJECT,
                description="Неверный код или номер",
                examples=[
                    OpenApiExample(
                        "Неверный код",
                        value={
                            "code": ["Неверный код подтверждения."]
                        }
                    ),
                    OpenApiExample(
                        "Истек срок действия",
                        value={
                            "non_field_errors": ["Срок действия кода истек."]
                        }
                    )
                ]
            ),
            403: OpenApiResponse(
                response=OpenApiTypes.OBJECT,
                description="Аккаунт заблокирован",
                examples=[
                    OpenApiExample(
                        "Превышены попытки",
                        value={
                            "error": "Аккаунт заблокирован. Попробуйте через 15 минут."
                        }
                    )
                ]
            )
        },
        examples=[
            OpenApiExample(
                "Пример запроса",
                value={
                    "phone_number": "+79991234567",
                    "code": "1234"
                },
                request_only=True
            )
        ]
    )
    def post(self, request):
        serializer = VerifyCodeSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        user = serializer.validated_data['user']

        refresh = RefreshToken.for_user(user)

        return Response({
            "refresh": str(refresh),
            "access": str(refresh.access_token),
            "access_expires": refresh.access_token.payload['exp'],
            "refresh_expires": refresh.payload['exp']
        })


class ProfileView(APIView):
    """Работа с профилем пользователя"""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        """Получить данные текущего пользователя"""
        serializer = UserSerializer(request.user)
        return Response(serializer.data)


class LogoutView(APIView):
    """Выход из системы"""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        # Удаляем токен
        RefreshToken.for_user(request.user).blacklist()

        return Response({
            "message": "Выход выполнен успешно"
        })

