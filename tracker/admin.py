from django.contrib import admin
from tracker.models import (
    User, Breed, Pet, RecurrenceRule, Event,
    EventNotificationLog, EventCompletion
)


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('phone_number', 'is_active', 'is_verified', 'is_staff', 'created_at')
    search_fields = ('phone_number',)
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
    list_display = ('frequency', 'interval', 'end_date')
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
