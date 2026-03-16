import random
import logging
from collections import defaultdict
from django.conf import settings
from django.utils.dateparse import parse_date
from django.utils import timezone
from django.core.cache import cache
from rest_framework import status, permissions, viewsets, generics
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed, ValidationError
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from drf_spectacular.utils import extend_schema_view

from tracker.models import User, Pet, Breed, PetType, Event, EventCompletion
from tracker.serializers import (
    PhoneNumberSerializer, VerifyCodeSerializer, UserSerializer, 
    PetSerializer, PetCreateSerializer, BreedSerializer, EventSerializer
)
from tracker.tasks import send_confirmation_code
from tracker.utils import generate_occurrences
from tracker import schemas

logger = logging.getLogger(__name__)

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

        logger.info(f'Код {code} отправлен на номер {phone_number}')
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
    """Обновление access token"""
    permission_classes = [permissions.AllowAny]

    @schemas.REFRESH_TOKEN_SCHEMA
    def post(self, request):
        refresh_token = request.data.get('refresh')
        if not refresh_token:
            return Response({'detail': 'Refresh token is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            refresh = RefreshToken(refresh_token)
            new_refresh = RefreshToken.for_user(refresh.user)
            return Response({
                'refresh': str(new_refresh),
                'access': str(new_refresh.access_token),
                'access_expires': new_refresh.access_token.payload['exp'],
                'refresh_expires': new_refresh.payload['exp']
            })
        except (TokenError, Exception) as e:
            return Response({'detail': str(e)}, status=status.HTTP_401_UNAUTHORIZED)


class LogoutView(APIView):
    """Выход из системы"""
    permission_classes = [permissions.IsAuthenticated]

    @schemas.LOGOUT_SCHEMA
    def post(self, request):
        try:
            RefreshToken.for_user(request.user).blacklist()
        except Exception:
            pass
        return Response({'detail': 'Выход выполнен успешно'})


# --- Profile & Pets ---

class ProfileView(generics.RetrieveAPIView):
    """Профиль текущего пользователя"""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user


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
        queryset = Event.objects.filter(user=self.request.user).select_related('recurrence')
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

        events = self.get_queryset()
        grouped_result = defaultdict(list)

        for event in events:
            for d in generate_occurrences(event, date_from, date_to):
                serializer = self.get_serializer(event, context={'request': request, 'occurrence_date': d})
                data = dict(serializer.data)
                
                # Обновляем дату начала для конкретного вхождения
                data['start_date'] = d.isoformat()
                
                date_key = data['start_date']
                grouped_result[date_key].append(data)

        # Сортируем события внутри каждой даты по времени
        for date_key in grouped_result:
            grouped_result[date_key].sort(key=lambda x: x.get('time') or '')

        # Возвращаем словарь, отсортированный по ключам-датам
        sorted_keys = sorted(grouped_result.keys())
        final_result = {key: grouped_result[key] for key in sorted_keys}

        return Response(final_result)

    @action(detail=True, methods=['post'])
    def mark_done(self, request, pk=None):
        """Отметить событие выполненным на дату"""
        event = self.get_object()
        occurrence_date = parse_date(request.data.get('date'))
        if not occurrence_date:
            return Response({'detail': 'date is required'}, status=400)

        EventCompletion.objects.get_or_create(event=event, occurrence_date=occurrence_date)
        return Response({'done': True})

    @action(detail=True, methods=['post'])
    def mark_undone(self, request, pk=None):
        """Отменить выполнение события на дату"""
        event = self.get_object()
        occurrence_date = parse_date(request.data.get('date'))
        if not occurrence_date:
            return Response({'detail': 'date is required'}, status=400)

        EventCompletion.objects.filter(event=event, occurrence_date=occurrence_date).delete()
        return Response({'done': False})
