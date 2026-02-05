from django.contrib import admin

from tracker.models import User, Breed


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    ...


@admin.register(Breed)
class BreedAdmin(admin.ModelAdmin):
    ...
