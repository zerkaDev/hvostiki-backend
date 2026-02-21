from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    SendCodeView,
    VerifyCodeView,
    ProfileView,
    LogoutView,
    PetViewSet, RefreshTokenView, BreedListAPIView, EventViewSet,
)

router = DefaultRouter()
router.register(r'pets', PetViewSet, basename='pet')
router.register(r'event_schedule', EventViewSet, basename='event_schedule')

urlpatterns = [
    path('auth/send-code/', SendCodeView.as_view(), name='send_code'),
    path('auth/verify-code/', VerifyCodeView.as_view(), name='verify_code'),
    path('auth/token/refresh/', RefreshTokenView.as_view(), name='token-refresh'),
    path('auth/logout/', LogoutView.as_view(), name='logout'),
    # Профиль
    path('profile/', ProfileView.as_view(), name='profile'),
    path('breeds/', BreedListAPIView.as_view(), name='breed-list'),
    path('', include(router.urls)),
]