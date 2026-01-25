from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    SendCodeView,
    VerifyCodeView,
    ProfileView,
    LogoutView,
    PetViewSet, RefreshTokenView, CatBreedsView, DogBreedsView,
)

router = DefaultRouter()
router.register(r'pets', PetViewSet, basename='pet')

urlpatterns = [
    # Авторизация/регистрация
    path('auth/send-code/', SendCodeView.as_view(), name='send_code'),
    path('auth/verify-code/', VerifyCodeView.as_view(), name='verify_code'),

    path('auth/token/refresh/', RefreshTokenView.as_view(), name='token-refresh'),
    path('auth/logout/', LogoutView.as_view(), name='logout'),

    # Профиль
    path('profile/', ProfileView.as_view(), name='profile'),

    path('', include(router.urls)),
    path('breeds/cat/', CatBreedsView.as_view(), name='catbreeds'),
    path('breeds/dog/', DogBreedsView.as_view(), name='dogbreeds'),
]