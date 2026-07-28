from django.contrib import admin
# pyrefly: ignore [missing-import]
from .models import Restaurant, RestaurantUser


@admin.register(Restaurant)
class RestaurantAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "is_active", "created_at")
    search_fields = ("name", "phone")
    list_filter = ("is_active",)
    prepopulated_fields = {"slug": ("name",)}


@admin.register(RestaurantUser)
class RestaurantUserAdmin(admin.ModelAdmin):
    list_display = ("user", "restaurant", "is_primary_admin", "created_at")
    search_fields = ("user__username", "user__email", "restaurant__name")
    list_filter = ("is_primary_admin", "restaurant")
