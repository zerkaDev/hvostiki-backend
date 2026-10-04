import base64
import binascii
import random
import logging
import uuid
from collections import defaultdict
from datetime import timedelta
import jwt
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils.dateparse import parse_date, parse_datetime, parse_time
from django.utils import timezone
from django.core.cache import cache
from rest_framework import status, permissions, viewsets, generics
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, ValidationError, Throttled
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import RefreshToken
from drf_spectacular.utils import extend_schema_view

from tracker.models import User, Pet, Breed, PetType, Event, EventCompletion, FCMDevice, Notification
from tracker.serializers import (
    PhoneNumberSerializer, VerifyCodeSerializer, UserSerializer, 
    PetSerializer, PetCreateSerializer, BreedSerializer, EventSerializer,
    DeviceRegistrationSerializer, DeviceUnregistrationSerializer, DeleteAccountSerializer, FeedbackSerializer,
    NotificationSettingsSerializer, NotificationSerializer
)
from tracker.tasks import send_confirmation_code, deliver_feedback
from tracker.services import account_deletion
from tracker.recurrence import event_slots
from tracker.event_time import time_to_stored
from tracker.utils import generate_occurrences
from tracker import schemas

logger = logging.getLogger(__name__)

# Окно «ближайших событий» по умолчанию (в днях) и допустимый максимум
DEFAULT_UPCOMING_DAYS = 14
MAX_PERIOD_DAYS = 400
MAX_OCCURRENCES_PER_RESPONSE = 10_000
MAX_UPCOMING_DAYS = 60


class OccurrenceLimitError(Exception):
    """Запрошено слишком много вхождений за один ответ."""


def _in_window(events, date_from, date_to):
    """SQL-предфильтр: события, у которых вообще может быть вхождение в окне."""
    return events.filter(start_date__lte=date_to).filter(
        Q(is_recurring=False, start_date__gte=date_from)
        | Q(is_recurring=True) & (
            Q(recurrence__until__isnull=True, recurrence__end_date__isnull=True)
            | Q(recurrence__until__gte=date_from)
            | Q(recurrence__until__isnull=True, recurrence__end_date__gte=date_from)
        )
    )


def group_occurrences(events, date_from, date_to, serializer_class, serializer_context=None):
    """Разворачивает события в вхождения и группирует их по датам.

    Возвращает словарь вида ``{'2026-09-30': [событие, ...]}``: ключи
    отсортированы по дате, внутри даты события отсортированы по локальному времени.
    Используется и в ``/event_schedule/period/``, и в ``/pets/{id}/upcoming/``.
    Бросает :class:`OccurrenceLimitError`, если вхождений больше ``MAX_OCCURRENCES_PER_RESPONSE``.
    """
    base_context = dict(serializer_context or {})
    grouped = defaultdict(list)

    if hasattr(events, 'filter'):
        events = _in_window(events, date_from, date_to)
    events = list(events)

    # Отметки выполнения и данные питомцев — одним запросом/сериализацией, а не на каждое вхождение
    base_context['completed'] = set(
        EventCompletion.objects
        .filter(event_id__in=[e.id for e in events], occurrence_date__gte=date_from, occurrence_date__lte=date_to)
        .values_list('event_id', 'occurrence_date', 'occurrence_time')
    )
    base_context['include_pet_obj'] = False

    total = 0
    for event in events:
        slots = event_slots(event)
        multi_slot = len(slots) > 1
        occurrence_days = generate_occurrences(event, date_from, date_to)
        for slot in slots:
            # Порядок внутри дня — по моменту события: локальное время минус offset, в секундах.
            # Не по строке time из ответа: в режиме utc оно сдвинуто на offset и «заворачивается»
            # через полночь, из-за чего 23:30 и 01:00 местного времени менялись местами.
            # События без времени («весь день») идут первыми.
            sort_key = (
                slot.hour * 3600 + slot.minute * 60 + slot.second - event.timezone_offset * 60
                if slot
                else float('-inf')
            )
            for occurrence in occurrence_days:
                total += 1
                if total > MAX_OCCURRENCES_PER_RESPONSE:
                    logger.warning(
                        'occurrence_limit_exceeded date_from=%s date_to=%s limit=%s',
                        date_from, date_to, MAX_OCCURRENCES_PER_RESPONSE,
                    )
                    raise OccurrenceLimitError
                serializer = serializer_class(
                    event,
                    context={
                        **base_context,
                        'occurrence_date': occurrence,
                        'slot_time': slot,
                        'occurrence_time': slot if multi_slot else None,
                    },
                )
                data = dict(serializer.data)
                # Дата конкретного вхождения, а не start_date события
                data['start_date'] = occurrence.isoformat()
                grouped[data['start_date']].append((sort_key, data))

    for items in grouped.values():
        items.sort(key=lambda item: item[0])

    return {date_key: [data for _, data in grouped[date_key]] for date_key in sorted(grouped)}


def _limit_error_response():
    """400 с понятным ``detail``, когда вхождений в ответе больше допустимого."""
    return Response(
        {'detail': 'Слишком много событий в выбранном периоде. Сократите период.'},
        status=status.HTTP_400_BAD_REQUEST,
    )


# --- Authentication Views ---

class SendCodeView(APIView):
    """Отправка кода подтверждения на номер телефона"""
    permission_classes = [permissions.AllowAny]

    @schemas.SEND_CODE_SCHEMA
    def post(self, request):
        serializer = PhoneNumberSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        phone_number = serializer.validated_data['phone_number']

        cache_key = f'code_sent_{phone_number}'
        if cache.get(cache_key):
            return Response(
                {'error': 'Код уже отправлен. Попробуйте через 1 минуту.'}, 
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        code = str(random.randint(1000, 9999))
        user, created = User.objects.get_or_create(phone_number=phone_number)
        
        user.confirmation_code = code
        user.code_sent_at = timezone.now()
        user.code_attempts = 0
        user.save(update_fields=['confirmation_code', 'code_sent_at', 'code_attempts'])

        if not settings.DEBUG:
            send_confirmation_code.delay(phone_number, code)

        logger.info('Код подтверждения отправлен на номер %s', phone_number)
        cache.set(cache_key, True, timeout=60)

        return Response({
            'detail': 'Код подтверждения отправлен',
            'phone_number': phone_number,
            'resend_timeout': 60
        })


class VerifyCodeView(APIView):
    """Проверка кода подтверждения и выдача JWT"""
    permission_classes = [permissions.AllowAny]

    @schemas.VERIFY_CODE_SCHEMA
    def post(self, request):
        serializer = VerifyCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        user = serializer.validated_data['user']
        user.reset_code_attempts()
        user.is_verified = True
        user.last_login = timezone.now()
        user.save(update_fields=['is_verified', 'last_login', 'code_attempts'])

        refresh = RefreshToken.for_user(user)
        return Response({
            'refresh': str(refresh),
            'access': str(refresh.access_token),
            'access_expires': refresh.access_token.payload['exp'],
            'refresh_expires': refresh.payload['exp']
        })


class RefreshTokenView(APIView):
    """Issue a new access token from a valid, non-revoked refresh token.

    Refresh-token rotation is disabled in ``SIMPLE_JWT``. Consequently, the
    submitted refresh token is returned unchanged and remains valid until its
    original expiration time or until it is revoked by ``LogoutView``.
    """
    permission_classes = [permissions.AllowAny]

    @schemas.REFRESH_TOKEN_SCHEMA
    def post(self, request):
        refresh_token = request.data.get('refresh')
        if not refresh_token:
            return Response({'detail': 'Refresh token is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            refresh = RefreshToken(refresh_token)
            access = refresh.access_token
            return Response({
                'refresh': str(refresh),
                'access': str(access),
                'access_expires': access.payload['exp'],
                'refresh_expires': refresh.payload['exp']
            })
        except TokenError as e:
            return Response({'detail': str(e)}, status=status.HTTP_401_UNAUTHORIZED)


class RegisterDeviceView(APIView):
    """Регистрация FCM токена устройства"""
    permission_classes = [permissions.IsAuthenticated]

    @schemas.REGISTER_DEVICE_SCHEMA
    def post(self, request):
        serializer = DeviceRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        fcm_token = serializer.validated_data['fcm_token']
        platform = serializer.validated_data.get('platform')

        # Если токен уже у кого-то есть, обновляем владельца; платформу не затираем, если не прислали
        defaults = {'user': request.user}
        if platform:
            defaults['platform'] = platform
        FCMDevice.objects.update_or_create(fcm_token=fcm_token, defaults=defaults)

        return Response({'detail': 'Токен успешно зарегистрирован'}, status=status.HTTP_200_OK)


class UnregisterDeviceView(APIView):
    """Отвязка FCM токена (выход из аккаунта): уведомления на устройство перестают приходить.

    Идемпотентно: удаляется только токен текущего пользователя, чужой или неизвестный токен
    не считается ошибкой (и не раскрывает, кому он принадлежит).
    """
    permission_classes = [permissions.IsAuthenticated]

    @schemas.UNREGISTER_DEVICE_SCHEMA
    def post(self, request):
        serializer = DeviceUnregistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        FCMDevice.objects.filter(
            user=request.user, fcm_token=serializer.validated_data['fcm_token']
        ).delete()

        return Response({'detail': 'Токен отвязан'}, status=status.HTTP_200_OK)


def _token_jti(raw_token: str) -> str | None:
    """Возвращает ``jti`` токена без проверки подписи.

    Нужен только для того, чтобы отличить повторный выход уже отозванным
    токеном от действительно невалидного токена.
    """
    try:
        payload = jwt.decode(raw_token, options={'verify_signature': False})
    except Exception:
        return None
    return payload.get(api_settings.JTI_CLAIM)


def _is_blacklisted(raw_token: str) -> bool:
    """Проверяет, был ли токен уже отозван (logout)."""
    jti = _token_jti(raw_token)
    if not jti:
        return False
    return BlacklistedToken.objects.filter(token__jti=jti).exists()


class LogoutView(APIView):
    """Revoke the refresh token that represents the current client session.

    The refresh token must be supplied in the request body and must belong to
    the authenticated user: creating a new token for the current user and
    blacklisting it would not invalidate the token stored by the client.

    The endpoint is idempotent: a repeated call with an already revoked token
    is not an error, the desired state has already been reached.
    """
    permission_classes = [permissions.IsAuthenticated]

    @schemas.LOGOUT_SCHEMA
    def post(self, request):
        refresh_token = request.data.get('refresh')
        if not refresh_token:
            return Response(
                {'detail': 'Refresh token is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            token = RefreshToken(refresh_token)
        except TokenError as e:
            if _is_blacklisted(refresh_token):
                # Повторный выход тем же токеном: цель уже достигнута.
                return Response({'detail': 'Выход выполнен успешно'})
            return Response({'detail': str(e)}, status=status.HTTP_401_UNAUTHORIZED)

        if str(token[api_settings.USER_ID_CLAIM]) != str(request.user.pk):
            return Response(
                {'detail': 'Refresh token does not belong to the current user'},
                status=status.HTTP_403_FORBIDDEN,
            )

        token.blacklist()
        return Response({'detail': 'Выход выполнен успешно'})


# --- Profile & Pets ---

@extend_schema_view(
    get=schemas.PROFILE_SCHEMA_GET,
    patch=schemas.PROFILE_SCHEMA_PATCH,
)
class ProfileView(generics.RetrieveUpdateAPIView):
    """Профиль текущего пользователя: просмотр и обновление имени и фото.

    Номер телефона изменить нельзя; PUT не поддерживается — только PATCH.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = UserSerializer
    http_method_names = ['get', 'patch', 'head', 'options']

    def get_object(self):
        return self.request.user


@extend_schema_view(
    get=schemas.NOTIFICATION_SETTINGS_GET_SCHEMA,
    patch=schemas.NOTIFICATION_SETTINGS_PATCH_SCHEMA,
)
class NotificationSettingsView(generics.GenericAPIView):
    """Настройки уведомлений по категориям (переключатели применяются сразу)."""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = NotificationSettingsSerializer

    def get(self, request):
        return Response(self.get_serializer(request.user).data)

    def patch(self, request):
        serializer = self.get_serializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.update(request.user, serializer.validated_data)
        return Response(self.get_serializer(request.user).data)


class ProfileAvatarView(APIView):
    """Удаление фото профиля (идемпотентно)."""
    permission_classes = [permissions.IsAuthenticated]

    @schemas.PROFILE_AVATAR_DELETE_SCHEMA
    def delete(self, request):
        user = request.user
        if user.avatar:
            old_name = user.avatar.name
            storage = user.avatar.storage
            user.avatar = None
            user.save(update_fields=['avatar', 'updated_at'])
            transaction.on_commit(lambda: storage.delete(old_name))
        return Response(status=status.HTTP_204_NO_CONTENT)


class DeleteAccountSendCodeView(APIView):
    """Шаг 1 удаления аккаунта: отправка кода звонком на номер пользователя."""
    permission_classes = [permissions.IsAuthenticated]

    @schemas.DELETE_ACCOUNT_SEND_CODE_SCHEMA
    def post(self, request):
        try:
            account_deletion.send_deletion_code(request.user)
        except account_deletion.CodeAlreadySent:
            return Response(
                {'detail': 'Код уже отправлен. Повторите попытку через минуту.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        return Response({
            'detail': 'Код подтверждения отправлен',
            'resend_timeout': account_deletion.RESEND_TIMEOUT,
        })


class DeleteAccountView(APIView):
    """Шаг 2 удаления аккаунта: подтверждение кодом и безвозвратное удаление."""
    permission_classes = [permissions.IsAuthenticated]

    @schemas.DELETE_ACCOUNT_SCHEMA
    def post(self, request):
        serializer = DeleteAccountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            account_deletion.check_deletion_code(
                request.user, serializer.validated_data['code']
            )
        except account_deletion.InvalidDeletionCode as error:
            return Response({'detail': error.message}, status=status.HTTP_400_BAD_REQUEST)

        account_deletion.delete_account(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class FeedbackView(generics.CreateAPIView):
    """Обращение в поддержку: тема, текст, необязательный скриншот и данные устройства."""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = FeedbackSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'feedback'

    def throttled(self, request, wait):
        raise Throttled(wait, detail='Слишком много обращений. Попробуйте позже.')

    @schemas.FEEDBACK_SCHEMA
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)

    def perform_create(self, serializer):
        feedback = serializer.save(user=self.request.user)
        transaction.on_commit(lambda: deliver_feedback.delay(feedback.pk))


@extend_schema_view(**schemas.PET_VIEWSET_SCHEMAS)
class PetViewSet(viewsets.ModelViewSet):
    """Управление питомцами"""
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Pet.objects.filter(owner=self.request.user).select_related('breed')

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return PetCreateSerializer
        return PetSerializer

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        
        # Возвращаем полные данные через PetSerializer
        full_data = PetSerializer(serializer.instance, context={'request': request}).data
        return Response(full_data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'])
    def upcoming(self, request, pk=None):
        """Ближайшие события питомца (по умолчанию на 14 дней)"""
        pet = self.get_object()  # 404, если питомец не принадлежит пользователю

        raw_days = request.query_params.get('days', DEFAULT_UPCOMING_DAYS)
        try:
            days = int(raw_days)
        except (TypeError, ValueError):
            return Response(
                {'detail': 'days must be an integer'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not 1 <= days <= MAX_UPCOMING_DAYS:
            return Response(
                {'detail': f'days must be between 1 and {MAX_UPCOMING_DAYS}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Клиент может передать своё «сегодня» (локальная дата), иначе берём дату сервера
        date_from = parse_date(request.query_params.get('date_from') or '') or timezone.localdate()
        date_to = date_from + timedelta(days=days)

        events = (
            Event.objects
            .filter(user=request.user, pet=pet)
            .select_related('recurrence')
        )

        try:
            return Response(
                group_occurrences(
                    events,
                    date_from,
                    date_to,
                    EventSerializer,
                    self.get_serializer_context(),
                )
            )
        except OccurrenceLimitError:
            return _limit_error_response()


class BreedListAPIView(generics.ListAPIView):
    """Список пород по типу животного"""
    serializer_class = BreedSerializer
    pagination_class = None

    @schemas.BREED_LIST_SCHEMA
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        pet_type = self.request.query_params.get('type')
        if pet_type not in PetType.values:
            raise ValidationError({'detail': "Параметр 'type' обязателен ('dog' или 'cat')"})
        return Breed.objects.filter(type=pet_type)


# --- Events ---

@extend_schema_view(**schemas.EVENT_VIEWSET_SCHEMAS)
class EventViewSet(viewsets.ModelViewSet):
    """Управление событиями"""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = EventSerializer

    def get_queryset(self):
        pet_id = self.request.query_params.get('pet_id')
        queryset = (
            Event.objects
            .filter(user=self.request.user)
            .select_related('recurrence', 'pet', 'pet__breed')
        )
        if pet_id:
            queryset = queryset.filter(pet_id=pet_id)
        return queryset

    def list(self, request, *args, **kwargs):
        raise MethodNotAllowed('GET', detail='Используйте /events/period/ для получения списка')

    @action(detail=False, methods=['get'])
    def period(self, request):
        """События за период (сгруппированные по датам)"""
        date_from = parse_date(request.query_params.get('date_from'))
        date_to = parse_date(request.query_params.get('date_to'))

        if not date_from or not date_to:
            return Response({'detail': 'date_from and date_to are required'}, status=400)

        if date_from > date_to:
            return Response({'detail': 'date_from must not be after date_to'}, status=400)

        if (date_to - date_from).days > MAX_PERIOD_DAYS:
            return Response({'detail': f'Период не может быть больше {MAX_PERIOD_DAYS} дней'}, status=400)

        try:
            return Response(
                group_occurrences(
                    self.get_queryset(),
                    date_from,
                    date_to,
                    self.get_serializer_class(),
                    self.get_serializer_context(),
                )
            )
        except OccurrenceLimitError:
            return _limit_error_response()


    def _completion_target(self, request, event):
        """Дата и слот отметки. Возвращает ``(date, slot, error_response)``.

        Слот нужен только при нескольких временах в день: ``time`` обязателен и должен совпадать
        с одним из слотов (формат — по контракту времени запроса). Иначе слот ``None`` (как раньше).
        """
        occurrence_date = parse_date(str(request.data.get('date') or ''))
        if not occurrence_date:
            return None, None, Response({'detail': 'date is required'}, status=400)

        slots = event_slots(event)
        if len(slots) <= 1:
            return occurrence_date, None, None

        raw_time = request.data.get('time')
        if not raw_time:
            return None, None, Response(
                {'detail': 'time is required: у события несколько времён в день'}, status=400,
            )
        parsed = parse_time(str(raw_time))
        if parsed is None:
            return None, None, Response({'detail': 'time has invalid format'}, status=400)
        slot = time_to_stored(parsed, event.timezone_offset).replace(second=0, microsecond=0)
        if slot not in slots:
            return None, None, Response({'detail': 'time does not match any slot of the event'}, status=400)
        return occurrence_date, slot, None

    @action(detail=True, methods=['post'])
    def mark_done(self, request, pk=None):
        """Отметить событие выполненным на дату (при нескольких временах в день — на слот `time`)"""
        event = self.get_object()
        occurrence_date, slot, error = self._completion_target(request, event)
        if error:
            return error

        EventCompletion.objects.get_or_create(event=event, occurrence_date=occurrence_date, occurrence_time=slot)
        return Response({'done': True})

    @action(detail=True, methods=['post'])
    def mark_undone(self, request, pk=None):
        """Отменить выполнение события на дату (при нескольких временах в день — на слот `time`)"""
        event = self.get_object()
        occurrence_date, slot, error = self._completion_target(request, event)
        if error:
            return error

        completions = EventCompletion.objects.filter(event=event, occurrence_date=occurrence_date)
        if slot is None:
            pass  # одно время / весь день: как раньше, снимаем все отметки даты
        elif slot == event.time:
            # старая отметка без слота относится к первому слоту
            completions = completions.filter(Q(occurrence_time=slot) | Q(occurrence_time__isnull=True))
        else:
            completions = completions.filter(occurrence_time=slot)
        completions.delete()
        return Response({'done': False})


# --- Центр уведомлений ---

NOTIFICATIONS_DEFAULT_LIMIT = 30
NOTIFICATIONS_MAX_LIMIT = 100


def _encode_notification_cursor(notification) -> str:
    raw = f'{notification.created_at.isoformat()}|{notification.id}'
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_notification_cursor(cursor: str):
    """Курсор → ``(created_at, id)``; на любой мусор поднимает ``ValueError``."""
    try:
        created_raw, id_raw = base64.urlsafe_b64decode(cursor.encode()).decode().split('|')
        created_at = parse_datetime(created_raw)
        notification_id = uuid.UUID(id_raw)
    except (ValueError, binascii.Error):  # UnicodeDecodeError — подкласс ValueError
        raise ValueError('Некорректный курсор')
    if created_at is None:
        raise ValueError('Некорректный курсор')
    return created_at, notification_id


class NotificationListView(APIView):
    """Список уведомлений пользователя (от новых к старым), порциями по курсору."""
    permission_classes = [permissions.IsAuthenticated]

    @schemas.NOTIFICATION_LIST_SCHEMA
    def get(self, request):
        try:
            limit = int(request.query_params.get('limit', NOTIFICATIONS_DEFAULT_LIMIT))
        except ValueError:
            limit = 0
        if not 1 <= limit <= NOTIFICATIONS_MAX_LIMIT:
            return Response(
                {'detail': f'limit должен быть числом от 1 до {NOTIFICATIONS_MAX_LIMIT}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        queryset = Notification.objects.filter(user=request.user).select_related('event')
        if request.query_params.get('unread', '').lower() in ('1', 'true'):
            queryset = queryset.filter(read_at__isnull=True)

        cursor = request.query_params.get('cursor')
        if cursor:
            try:
                created_at, notification_id = _decode_notification_cursor(cursor)
            except ValueError as exc:
                return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            queryset = queryset.filter(
                Q(created_at__lt=created_at) | Q(created_at=created_at, id__lt=notification_id)
            )

        page = list(queryset.order_by('-created_at', '-id')[:limit + 1])
        has_more = len(page) > limit
        page = page[:limit]

        return Response({
            'results': NotificationSerializer(page, many=True).data,
            'next_cursor': _encode_notification_cursor(page[-1]) if has_more else None,
            'unread_count': _unread_notifications(request.user),
        })


def _unread_notifications(user) -> int:
    return Notification.objects.filter(user=user, read_at__isnull=True).count()


class NotificationUnreadCountView(APIView):
    """Число непрочитанных уведомлений (для бейджа на колокольчике)."""
    permission_classes = [permissions.IsAuthenticated]

    @schemas.NOTIFICATION_UNREAD_COUNT_SCHEMA
    def get(self, request):
        return Response({'unread_count': _unread_notifications(request.user)})


class NotificationReadView(APIView):
    """Отметить уведомление прочитанным (идемпотентно)."""
    permission_classes = [permissions.IsAuthenticated]

    @schemas.NOTIFICATION_READ_SCHEMA
    def post(self, request, pk):
        notifications = Notification.objects.filter(user=request.user, pk=pk)
        if not notifications.exists():
            return Response({'detail': 'Уведомление не найдено'}, status=status.HTTP_404_NOT_FOUND)
        notifications.filter(read_at__isnull=True).update(read_at=timezone.now())
        return Response({'unread_count': _unread_notifications(request.user)})


class NotificationReadAllView(APIView):
    """Отметить все уведомления прочитанными."""
    permission_classes = [permissions.IsAuthenticated]

    @schemas.NOTIFICATION_READ_ALL_SCHEMA
    def post(self, request):
        Notification.objects.filter(user=request.user, read_at__isnull=True).update(read_at=timezone.now())
        return Response({'unread_count': 0})
