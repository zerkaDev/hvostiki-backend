from django.contrib import admin

from tracker.models import User, Breed, Pet


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    ...


@admin.register(Breed)
class BreedAdmin(admin.ModelAdmin):
    ...


@admin.register(Pet)
class PetAdmin(admin.ModelAdmin):
    ...
