from django.conf import settings
from django.contrib import admin
from django.http import JsonResponse
from django.urls import path, include, re_path
from django.views.static import serve
from drf_spectacular.utils import extend_schema
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView


@extend_schema(
    tags=['Служебные'],
    summary='Проверка живости',
    description='Отвечает 200, пока процесс принимает запросы. Используется healthcheck\'ом и мониторингом.',
    responses={200: None},
    auth=[],
)
def healthz(request):
    """Liveness-проба: БД и Redis не трогаем, важен только ответ процесса."""
    return JsonResponse({'status': 'ok'})


urlpatterns = [
    path('healthz', healthz, name='healthz'),
    path('admin/', admin.site.urls),
    path('', include('tracker.urls')),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]

if settings.SERVE_MEDIA:
    urlpatterns += [
        re_path(
            r'^%s(?P<path>.*)$' % settings.MEDIA_URL.lstrip('/'),
            serve,
            {'document_root': settings.MEDIA_ROOT},
        ),
    ]