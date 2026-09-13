from django.contrib import admin
# pyrefly: ignore [missing-import]
from .models import Restaurant, RestaurantUser, JobPosting, JobApplication


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


@admin.register(JobPosting)
class JobPostingAdmin(admin.ModelAdmin):
    list_display = ("title", "restaurant", "branch", "department", "experience_level", "is_active", "created_at")
    list_filter = ("restaurant", "branch", "department", "job_type", "is_active")
    search_fields = ("title", "department", "location", "roles_responsibilities")
    prepopulated_fields = {"slug": ("title",)}


@admin.register(JobApplication)
class JobApplicationAdmin(admin.ModelAdmin):
    list_display = ("full_name", "job", "restaurant", "branch", "email", "phone", "has_experience", "status", "created_at")
    list_filter = ("status", "restaurant", "branch", "has_experience", "is_read")
    search_fields = ("full_name", "email", "phone", "current_organization", "reason_to_join")
