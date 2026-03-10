from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiExample, OpenApiParameter
from drf_spectacular.types import OpenApiTypes
from rest_framework import status

from tracker.models import PetType
from tracker.serializers import (
    PhoneNumberSerializer, VerifyCodeSerializer, PetSerializer, 
    PetCreateSerializer, RefreshTokenSerializer, TokenResponseSerializer, 
    ErrorResponseSerializer, BreedSerializer, EventSerializer
)

# --- Authentication ---

SEND_CODE_SCHEMA = extend_schema(
    tags=['Аутентификация'],
    summary='Отправка кода подтверждения',
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
            description='Код успешно отправлен',
            examples=[
                OpenApiExample(
                    'Успешная отправка',
                    value={
                        'detail': 'Код подтверждения отправлен',
                        'phone_number': '+79991234567',
                        'resend_timeout': 60
                    }
                )
            ]
        ),
        400: OpenApiResponse(
            response=OpenApiTypes.OBJECT,
            description='Неверный формат номера телефона',
        ),
        429: OpenApiResponse(
            response=OpenApiTypes.OBJECT,
            description='Слишком много запросов',
        )
    },
    examples=[
        OpenApiExample(
            'Пример запроса',
            value={'phone_number': '+79991234567'},
            request_only=True
        )
    ]
)

VERIFY_CODE_SCHEMA = extend_schema(
    tags=['Аутентификация'],
    summary='Подтверждение кода',
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
            description='Код подтвержден, токены получены',
            examples=[
                OpenApiExample(
                    'Успешная аутентификация',
                    value={
                        'refresh': 'eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...',
                        'access': 'eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...',
                        'access_expires': 1736000000,
                        'refresh_expires': 1736000000
                    }
                )
            ]
        ),
        400: OpenApiResponse(
            response=OpenApiTypes.OBJECT,
            description='Неверный код или номер',
        ),
        403: OpenApiResponse(
            response=OpenApiTypes.OBJECT,
            description='Аккаунт заблокирован',
        )
    },
    examples=[
        OpenApiExample(
            'Пример запроса',
            value={
                'phone_number': '+79991234567',
                'code': '1234'
            },
            request_only=True
        )
    ]
)

REFRESH_TOKEN_SCHEMA = extend_schema(
    tags=['Аутентификация'],
    summary='Обновление токенов доступа',
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
            description='Токены успешно обновлены',
        ),
        400: OpenApiResponse(
            response=ErrorResponseSerializer,
            description='Отсутствует refresh token',
        ),
        401: OpenApiResponse(
            response=ErrorResponseSerializer,
            description='Неверный или просроченный refresh token',
        )
    }
)

LOGOUT_SCHEMA = extend_schema(
    tags=['Аутентификация'],
    summary='Выход из системы',
)

# --- Pets ---

PET_VIEWSET_SCHEMAS = {
    'list': extend_schema(
        summary='Получить список питомцев',
        description='Возвращает список всех питомцев текущего пользователя',
        tags=['pets'],
        responses={200: PetSerializer(many=True)}
    ),
    'create': extend_schema(
        summary='Создать нового питомца',
        description='Создание нового питомца с привязкой к текущему пользователю',
        tags=['pets'],
        request=PetCreateSerializer,
        responses={201: PetSerializer},
        examples=[
            OpenApiExample(
                'Успешное создание',
                value={
                    'id': 'uuid',
                    'name': 'Барсик',
                    'pet_type': 'cat',
                    'breed': 1,
                    'weight': '4.50',
                    'birthday': '2024-01-15',
                    'color': 'Grey',
                    'image': None,
                    'created_at': '2024-01-15T10:30:00Z',
                    'updated_at': '2024-01-15T10:30:00Z'
                },
                response_only=True,
                status_codes=['201'],
            ),
        ]
    ),
    'retrieve': extend_schema(
        summary='Получить информацию о питомце',
        tags=['pets'],
        parameters=[
            OpenApiParameter(name='id', type=OpenApiTypes.INT, location=OpenApiParameter.PATH, description='ID питомца'),
        ],
        responses={200: PetSerializer, 404: OpenApiTypes.OBJECT}
    ),
    'update': extend_schema(
        summary='Обновить информацию о питомце',
        tags=['pets'],
        request=PetCreateSerializer,
        responses={200: PetSerializer}
    ),
    'partial_update': extend_schema(
        summary='Частично обновить информацию о питомце',
        tags=['pets'],
        request=PetCreateSerializer,
        responses={200: PetSerializer}
    ),
    'destroy': extend_schema(
        summary='Удалить питомца',
        tags=['pets'],
        responses={204: None}
    ),
}

# --- Breeds ---

BREED_LIST_SCHEMA = extend_schema(
    summary='Список пород по типу животного',
    description="""
        Возвращает список пород животных.

        **Параметр `type` обязателен**:
        - `dog` — породы собак
        - `cat` — породы кошек
        """,
    parameters=[
        OpenApiParameter(
            name='type',
            description='Тип животного',
            required=True,
            type=OpenApiTypes.STR,
            enum=[PetType.DOG, PetType.CAT],
            location=OpenApiParameter.QUERY,
        )
    ],
    responses={200: BreedSerializer(many=True)},
    tags=['Breeds'],
)

# --- Events ---

EVENT_VIEWSET_SCHEMAS = {
    'create': extend_schema(
        summary='Создать событие',
        description='Создает одноразовое или повторяющееся событие',
        request=EventSerializer,
        responses={201: EventSerializer},
        examples=[
            OpenApiExample(
                name='Recurring weekly example',
                value={
                    'pet': 1,
                    'type': 'feeding',
                    'title': 'Дать таблетку',
                    'description': 'После еды',
                    'start_date': '2026-02-20',
                    'time': '17:00',
                    'timezone_offset': 60,
                    'is_recurring': True,
                    'recurrence': {
                        'frequency': 'weekly',
                        'interval': 1,
                        'week_days': [1, 4]
                    }
                },
                request_only=True,
            )
        ],
    ),
    'period': extend_schema(
        summary='Получить события за период',
        description="""
            Возвращает словарь событий, сгруппированный по датам. 
            Ключ — дата в формате YYYY-MM-DD, значение — список событий в эту дату.
            Словарь отсортирован по датам, события внутри даты — по времени.
            """,
        parameters=[
            OpenApiParameter(name='date_from', type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY, required=True),
            OpenApiParameter(name='date_to', type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY, required=True),
            OpenApiParameter(name='pet_id', type=OpenApiTypes.INT, location=OpenApiParameter.QUERY, required=False),
        ],
        responses={
            200: OpenApiResponse(
                response=OpenApiTypes.OBJECT,
                description='Сгруппированный список событий',
                examples=[
                    OpenApiExample(
                        'Пример сгруппированного ответа',
                        value={
                            '2026-03-06': [
                                {'id': 'uuid', 'title': 'Утреннее кормление', 'time': '08:00', 'done': False},
                                {'id': 'uuid', 'title': 'Прогулка', 'time': '12:00', 'done': True}
                            ],
                            '2026-03-07': [
                                {'id': 'uuid', 'title': 'Визит к ветеринару', 'time': '10:00', 'done': False}
                            ]
                        }
                    )
                ]
            )
        },
    ),
    'mark_done': extend_schema(
        summary='Отметить событие выполненным',
        description="""
                Отмечает конкретный occurrence события как выполненный.

                **Важно:**
                - Для одноразового события передавайте его start_date
                - Для повторяющегося — дату конкретного occurrence
                - Повторный вызов безопасен (idempotent)
                """,
        request=OpenApiTypes.OBJECT,
        responses={
            200: OpenApiResponse(
                response=OpenApiTypes.OBJECT,
                description='Событие успешно отмечено выполненным',
                examples=[OpenApiExample(name='Success', value={'done': True})],
            ),
        },
        examples=[
            OpenApiExample(name='Mark occurrence done', request_only=True, value={'date': '2026-02-20'}),
        ],
        tags=['events'],
    )
}
