from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    SendCodeView,
    VerifyCodeView,
    ProfileView,
    ProfileAvatarView,
    DeleteAccountSendCodeView,
    DeleteAccountView,
    FeedbackView,
    NotificationSettingsView,
    LogoutView,
    PetViewSet, RefreshTokenView, BreedListAPIView, EventViewSet,
    RegisterDeviceView, UnregisterDeviceView,
    NotificationListView, NotificationUnreadCountView, NotificationReadView, NotificationReadAllView,
)

router = DefaultRouter()
router.register(r'pets', PetViewSet, basename='pet')
router.register(r'event_schedule', EventViewSet, basename='event_schedule')

urlpatterns = [
    path('auth/send-code/', SendCodeView.as_view(), name='send_code'),
    path('auth/verify-code/', VerifyCodeView.as_view(), name='verify_code'),
    path('auth/token/refresh/', RefreshTokenView.as_view(), name='token-refresh'),
    path('auth/logout/', LogoutView.as_view(), name='logout'),
    path('devices/register/', RegisterDeviceView.as_view(), name='register_device'),
    path('devices/unregister/', UnregisterDeviceView.as_view(), name='unregister_device'),
    # Центр уведомлений
    path('notifications/', NotificationListView.as_view(), name='notification-list'),
    path('notifications/unread-count/', NotificationUnreadCountView.as_view(), name='notification-unread-count'),
    path('notifications/read-all/', NotificationReadAllView.as_view(), name='notification-read-all'),
    path('notifications/<uuid:pk>/read/', NotificationReadView.as_view(), name='notification-read'),
    # Профиль
    path('profile/', ProfileView.as_view(), name='profile'),
    path('profile/notification-settings/', NotificationSettingsView.as_view(), name='profile-notification-settings'),
    path('profile/avatar/', ProfileAvatarView.as_view(), name='profile-avatar'),
    path('profile/delete/send-code/', DeleteAccountSendCodeView.as_view(), name='profile-delete-send-code'),
    path('profile/delete/', DeleteAccountView.as_view(), name='profile-delete'),
    path('feedback/', FeedbackView.as_view(), name='feedback'),
    path('breeds/', BreedListAPIView.as_view(), name='breed-list'),
    path('', include(router.urls)),
]