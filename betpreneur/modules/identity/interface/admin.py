from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from betpreneur.modules.identity.models import User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ("username", "email", "is_email_verified", "is_staff", "is_active")
    list_filter = ("is_email_verified", "is_staff", "is_active")
