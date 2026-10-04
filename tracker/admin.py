from django.contrib import admin
from tracker.models import (
    User, Breed, Pet, RecurrenceRule, Event,
    EventNotificationLog, EventCompletion, Feedback, Notification
)


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
    list_display = ('id', 'topic', 'user', 'platform', 'app_version', 'created_at', 'delivered_at')
    list_filter = ('topic', 'platform')
    search_fields = ('message', 'user__phone_number')
    readonly_fields = ('created_at', 'delivered_at')


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    """Просмотр истории; для объявлений (``kind=announcement``) достаточно заполнить пользователя, заголовок и текст."""
    list_display = ('title', 'user', 'kind', 'pet', 'created_at', 'read_at')
    list_filter = ('kind', 'created_at')
    search_fields = ('title', 'body', 'user__phone_number')
    raw_id_fields = ('user', 'pet', 'event', 'log')
