from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    SendCodeView,
    VerifyCodeView,
    ProfileView,
    LogoutView,
    PetViewSet,
)

router = DefaultRouter()
router.register(r'pets', PetViewSet, basename='pet')

urlpatterns = [
    # Авторизация/регистрация
    path('auth/send-code/', SendCodeView.as_view(), name='send_code'),
    path('auth/verify-code/', VerifyCodeView.as_view(), name='verify_code'),
    path('auth/logout/', LogoutView.as_view(), name='logout'),

    # Профиль
    path('profile/', ProfileView.as_view(), name='profile'),

    path('', include(router.urls)),
]