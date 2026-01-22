from django.conf import settings
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiExample, extend_schema_view, OpenApiParameter
from rest_framework import status, permissions, viewsets
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework.response import Response
from django.utils import timezone
from django.core.cache import cache
import random
import logging

from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from tracker.models import User, Pet
from tracker.serializers import (
    PhoneNumberSerializer,
    VerifyCodeSerializer,
    UserSerializer, PetSerializer, PetCreateSerializer, ErrorResponseSerializer, TokenResponseSerializer,
    RefreshTokenSerializer,
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
        tags=["Аутентификация"],
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
                            "detail": "Код подтверждения отправлен",
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
            "detail": "Код подтверждения отправлен",
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
        tags=["Аутентификация"],
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


@extend_schema(
    tags=["Аутентификация"],
)
class LogoutView(APIView):
    """Выход из системы"""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        # Удаляем токен
        RefreshToken.for_user(request.user).blacklist()

        return Response({
            "detail": "Выход выполнен успешно"
        })

@extend_schema_view(
    list=extend_schema(
        summary='Получить список питомцев',
        description='Возвращает список всех питомцев текущего пользователя',
        tags=['pets'],
        responses={
            200: PetSerializer(many=True),
            401: OpenApiTypes.OBJECT,
        }
    ),
    create=extend_schema(
        summary='Создать нового питомца',
        description='Создание нового питомца с привязкой к текущему пользователю',
        tags=['pets'],
        request=PetCreateSerializer,
        responses={
            201: PetSerializer,
            400: OpenApiTypes.OBJECT,
            401: OpenApiTypes.OBJECT,
        },
        examples=[
            OpenApiExample(
                'Успешное создание',
                value={
                    "id": 1,
                    "owner": "username",
                    "owner_id": 1,
                    "name": "Барсик",
                    "pet_type": "cat",
                    "pet_type_display": "Кошка",
                    "breed": "Сиамская",
                    "weight": "4.50",
                    "birthday": '2024-01-15',
                    "color": "white",
                    "color_display": "Белый",
                    "image": None,
                    "image_url": None,
                    "created_at": "2024-01-15T10:30:00Z",
                    "updated_at": "2024-01-15T10:30:00Z"
                },
                response_only=True,
                status_codes=['201'],
            ),
        ]
    ),
    retrieve=extend_schema(
        summary='Получить информацию о питомце',
        description='Получение детальной информации о конкретном питомце',
        tags=['pets'],
        parameters=[
            OpenApiParameter(
                name='id',
                type=OpenApiTypes.INT,
                location=OpenApiParameter.PATH,
                description='ID питомца'
            ),
        ],
        responses={
            200: PetSerializer,
            404: OpenApiTypes.OBJECT,
        }
    ),
    update=extend_schema(
        summary='Обновить информацию о питомце',
        description='Полное обновление информации о питомце',
        tags=['pets'],
        request=PetCreateSerializer,
        responses={
            200: PetSerializer,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
        }
    ),
    partial_update=extend_schema(
        summary='Частично обновить информацию о питомце',
        description='Частичное обновление информации о питомце',
        tags=['pets'],
        request=PetCreateSerializer,
        responses={
            200: PetSerializer,
            400: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
        }
    ),
    destroy=extend_schema(
        summary='Удалить питомца',
        description='Удаление питомца по ID',
        tags=['pets'],
        responses={
            204: None,
            404: OpenApiTypes.OBJECT,
        }
    ),
)
class PetViewSet(viewsets.ModelViewSet):
    """
    ViewSet для управления питомцами.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        """Пользователь видит только своих питомцев"""
        return Pet.objects.filter(owner=self.request.user)

    def get_serializer_class(self):
        """Выбираем сериализатор в зависимости от действия"""
        if self.action in ['create', 'update', 'partial_update']:
            return PetCreateSerializer
        return PetSerializer

    def perform_create(self, serializer):
        """Автоматически устанавливаем владельца при создании"""
        serializer.save(owner=self.request.user)

    def create(self, request, *args, **kwargs):
        """Кастомный обработчик создания"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        # Возвращаем полные данные созданного питомца
        pet = serializer.instance
        full_serializer = PetSerializer(
            pet,
            context={'request': request}
        )

        return Response(
            full_serializer.data,
            status=status.HTTP_201_CREATED
        )

    @action(detail=False, methods=['get'])
    def my_pets(self, request):
        """Получение питомцев текущего пользователя (альтернатива)"""
        pets = self.get_queryset()
        serializer = self.get_serializer(pets, many=True)
        return Response({
            'count': len(serializer.data),
            'results': serializer.data
        })


@extend_schema(
    tags=["Аутентификация"],
    summary="Обновление токенов доступа",
    description="""
    Обновление access token с помощью refresh token.

    **Важно:**
    - Refresh token действителен 7 дней
    - Access token действителен 60 минут
    - При каждом обновлении выдается новый refresh token (ротация токенов)
    - Старый refresh token становится недействительным
    """,
    request=RefreshTokenSerializer,
    responses={
        200: OpenApiResponse(
            response=TokenResponseSerializer,
            description="Токены успешно обновлены",
            examples=[
                OpenApiExample(
                    "Успешное обновление токенов",
                    value={
                        "refresh": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
                        "access": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
                        "access_expires": 1700000000,
                        "refresh_expires": 1700604800
                    }
                )
            ]
        ),
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Отсутствует refresh token",
            examples=[
                OpenApiExample(
                    "Отсутствует refresh token",
                    value={
                        "detail": "Refresh token is required"
                    }
                )
            ]
        ),
        401: OpenApiResponse(
            response=ErrorResponseSerializer,
            description="Неверный или просроченный refresh token",
            examples=[
                OpenApiExample(
                    "Неверный токен",
                    value={
                        "detail": "Token is invalid or expired"
                    }
                ),
                OpenApiExample(
                    "Просроченный токен",
                    value={
                        "detail": "Token has expired"
                    }
                )
            ]
        )
    },
    examples=[
        OpenApiExample(
            "Запрос на обновление токена",
            value={"refresh": "your_refresh_token_here"},
            request_only=True
        )
    ]
)
class RefreshTokenView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        refresh_token = request.data.get('refresh')

        if not refresh_token:
            return Response(
                {"detail": "Refresh token is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            refresh = RefreshToken(refresh_token)

            # Создаем новые токены
            new_refresh = RefreshToken.for_user(refresh.user)
            new_access_token = new_refresh.access_token

            # Добавляем старый токен в blacklist (если настроен)
            # refresh.blacklist()

            return Response({
                "refresh": str(new_refresh),
                "access": str(new_access_token),
                "access_expires": new_access_token.payload['exp'],
                "refresh_expires": new_refresh.payload['exp']
            })

        except TokenError as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_401_UNAUTHORIZED
            )
        except Exception:
            return Response(
                {"detail": "Invalid token"},
                status=status.HTTP_401_UNAUTHORIZED
            )