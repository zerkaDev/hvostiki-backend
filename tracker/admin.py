import logging

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.urls import path, reverse
from django.utils.html import format_html
from tracker.models import (
    User, Breed, Pet, RecurrenceRule, Event,
    EventNotificationLog, EventCompletion, Feedback
)
from tracker.services import feedback_logs

logger = logging.getLogger(__name__)


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('phone_number', 'name', 'is_active', 'is_verified', 'is_staff', 'created_at')
    search_fields = ('phone_number', 'name')
    list_filter = ('is_active', 'is_verified', 'is_staff')


@admin.register(Breed)
class BreedAdmin(admin.ModelAdmin):
    list_display = ('name', 'type')
    list_filter = ('type',)
    search_fields = ('name',)


@admin.register(Pet)
class PetAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'pet_type', 'breed', 'gender', 'created_at')
    list_filter = ('pet_type', 'gender', 'has_castration')
    search_fields = ('name', 'owner__phone_number')


@admin.register(RecurrenceRule)
class RecurrenceRuleAdmin(admin.ModelAdmin):
    list_display = ('frequency', 'interval', 'end_date', 'end_count', 'until')
    list_filter = ('frequency',)


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'pet', 'start_date', 'time', 'is_recurring', 'type', 'done')
    list_filter = ('type', 'is_recurring', 'done', 'start_date')
    search_fields = ('title', 'user__phone_number', 'pet__name')


@admin.register(EventNotificationLog)
class EventNotificationLogAdmin(admin.ModelAdmin):
    list_display = ('event', 'occurrence_date', 'sent_at')
    list_filter = ('occurrence_date', 'sent_at')


@admin.register(EventCompletion)
class EventCompletionAdmin(admin.ModelAdmin):
    list_display = ('event', 'occurrence_date', 'done_at')
    list_filter = ('occurrence_date', 'done_at')


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'topic', 'user', 'platform', 'app_version', 'has_logs', 'created_at', 'delivered_at'
    )
    list_filter = ('topic', 'platform')
    search_fields = ('message', 'user__phone_number')
    # Поле logs не выводим формой: у приватного файла нет URL, скачивание — через logs_link.
    exclude = ('logs',)
    readonly_fields = ('created_at', 'delivered_at', 'logs_link')

    @admin.display(boolean=True, description='Журнал')
    def has_logs(self, obj):
        return bool(obj.logs)

    @admin.display(description='Журнал приложения')
    def logs_link(self, obj):
        if not obj.pk or not obj.logs:
            return '—'
        url = reverse('admin:tracker_feedback_logs', args=[obj.pk])
        return format_html('<a href="{}">Скачать (.log.gz)</a>', url)

    def get_urls(self):
        download = path(
            '<int:object_id>/logs/',
            self.admin_site.admin_view(self.download_logs),
            name='tracker_feedback_logs',
        )
        return [download, *super().get_urls()]

    def download_logs(self, request, object_id):
        """Отдаёт журнал приложения сотруднику с правом просмотра; каждое скачивание пишется в лог."""
        feedback = self.get_object(request, str(object_id))
        if feedback is None or not feedback.logs:
            raise Http404
        if not self.has_view_permission(request, feedback):
            raise PermissionDenied

        logger.info('Журнал обращения #%s скачал сотрудник %s', feedback.pk, request.user.pk)
        response = FileResponse(
            feedback.logs.open('rb'),
            as_attachment=True,
            filename=f'feedback-{feedback.pk}-logs.log.gz',
            content_type='application/gzip',
        )
        return response  # admin_view уже добавляет Cache-Control: no-store

    # Файл не удаляется вместе со строкой сам, поэтому чистим его при удалении из админки.
    def delete_model(self, request, obj):
        name = obj.logs.name if obj.logs else None
        super().delete_model(request, obj)
        feedback_logs.delete_files([name])

    def delete_queryset(self, request, queryset):
        names = list(
            queryset.exclude(logs__isnull=True).exclude(logs='').values_list('logs', flat=True)
        )
        super().delete_queryset(request, queryset)
        feedback_logs.delete_files(names)
