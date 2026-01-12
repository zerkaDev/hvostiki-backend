from django.contrib import admin

from tracker.models import User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    ...