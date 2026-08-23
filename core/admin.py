from django.contrib import admin
from django.utils.html import format_html
from django import forms
from django.core.exceptions import ValidationError
# pyrefly: ignore [missing-import]
from .models import Category, SubCategory, MenuItem, Order, OrderItem, RestaurantSettings, RestaurantLanding, GalleryImage, TeamMember, Review, Coupon, InventoryItem, MenuItemIngredient, InventoryTransaction, BlogPost, BlogComment


class SubCategoryInline(admin.TabularInline):
    model = SubCategory
    extra = 1
    min_num = 1
    show_change_link = True


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'subcategory_count', 'menu_item_count')
    search_fields = ('name',)
    inlines = [SubCategoryInline]
    list_per_page = 20

    def subcategory_count(self, obj):
        return obj.subcategories.count()
    subcategory_count.short_description = 'Subcategories'
    
    def menu_item_count(self, obj):
        return MenuItem.objects.filter(subcategory__category=obj).count()
    menu_item_count.short_description = 'Menu Items'


@admin.register(SubCategory)
class SubCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'menu_item_count')
    list_filter = ('category',)
    search_fields = ('name', 'category__name')
    list_per_page = 20
    
    def menu_item_count(self, obj):
        return obj.menu_items.count()
    menu_item_count.short_description = 'Menu Items'


class PriceRangeFilter(admin.SimpleListFilter):
    title = 'price range'
    parameter_name = 'price_range'

    def lookups(self, request, model_admin):
        return (
            ('0-10', 'Under $10'),
            ('10-20', '$10 to $20'),
            ('20-50', '$20 to $50'),
            ('50+', 'Over $50'),
        )

    def queryset(self, request, queryset):
        if self.value() == '0-10':
            return queryset.filter(price__lte=10)
        if self.value() == '10-20':
            return queryset.filter(price__gt=10, price__lte=20)
        if self.value() == '20-50':
            return queryset.filter(price__gt=20, price__lte=50)
        if self.value() == '50+':
            return queryset.filter(price__gt=50)
        return queryset


class MissingSubcategoryFilter(admin.SimpleListFilter):
    title = 'data issues'
    parameter_name = 'data_issue'

    def lookups(self, request, model_admin):
        return (
            ('missing_subcategory', 'Missing subcategory'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'missing_subcategory':
            return queryset.filter(subcategory__isnull=True)
        return queryset

class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 1
    min_num = 1
    autocomplete_fields = ['menu_item']
    readonly_fields = ['subtotal']


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'customer_name', 'table_number', 'order_time', 'status_badge', 'total_amount')
    list_filter = ('status', 'order_time')
    search_fields = ('customer_name', 'phone', 'table_number')
    inlines = [OrderItemInline]
    readonly_fields = ('total_amount', 'order_time')
    date_hierarchy = 'order_time'
    list_per_page = 20
    actions = ['mark_as_served', 'mark_as_cancelled']
    
    fieldsets = (
        ('Order Information', {
            'fields': ('customer_name', 'phone', 'table_number', 'status', 'coupon', 'discount_amount', 'total_amount', 'notes')
        }),
        ('Timestamps', {
            'fields': ('order_time',),
            'classes': ('collapse',)
        }),
    )
    
    def status_badge(self, obj):
        status_colors = {
            'P': 'bg-warning',
            'S': 'bg-success',
            'C': 'bg-danger'
        }
        return format_html(
            '<span class="badge {}">{}</span>',
            status_colors.get(obj.status, 'bg-secondary'),
            obj.get_status_display()
        )
    status_badge.short_description = 'Status'
    status_badge.admin_order_field = 'status'

    def mark_as_served(self, request, queryset):
        updated = queryset.update(status=Order.OrderStatus.SERVED)
        self.message_user(request, f'{updated} orders marked as served.', 'success')
    mark_as_served.short_description = 'Mark selected orders as served'

    def mark_as_cancelled(self, request, queryset):
        updated = queryset.update(status=Order.OrderStatus.CANCELLED)
        self.message_user(request, f'{updated} orders marked as cancelled.', 'warning')
    mark_as_cancelled.short_description = 'Mark selected orders as cancelled'


@admin.register(MenuItem)
class MenuItemAdmin(admin.ModelAdmin):
    class MenuItemAdminForm(forms.ModelForm):
        category = forms.ModelChoiceField(
            queryset=Category.objects.all(),
            required=True,
            label='Category'
        )
        # Subcategory is optional
        subcategory = forms.ModelChoiceField(
            queryset=SubCategory.objects.none(),
            required=False,
            label='Subcategory (optional)'
        )

        class Meta:
            model = MenuItem
            fields = ['name', 'description', 'category', 'subcategory', 'price', 'image', 'availability']

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # Set initial category from instance's subcategory
            if self.instance and self.instance.pk and self.instance.subcategory:
                self.fields['category'].initial = self.instance.subcategory.category
            # Filter subcategory choices by selected category if present
            selected_cat_id = None
            if self.data.get('category'):
                try:
                    selected_cat_id = int(self.data.get('category'))
                except (TypeError, ValueError):
                    selected_cat_id = None
            elif self.fields['category'].initial:
                selected_cat_id = self.fields['category'].initial.id
            if selected_cat_id:
                self.fields['subcategory'].queryset = SubCategory.objects.filter(category_id=selected_cat_id)
            else:
                self.fields['subcategory'].queryset = SubCategory.objects.none()

        def clean(self):
            cleaned = super().clean()
            category = cleaned.get('category')
            subcategory = cleaned.get('subcategory')
            if category and subcategory and subcategory.category_id != category.id:
                raise ValidationError({'subcategory': 'Selected subcategory does not belong to the chosen category.'})
            if not category:
                raise ValidationError({'category': 'Please select a category.'})
            return cleaned

    list_display = ('item_thumbnail', 'name', 'get_category', 'get_subcategory', 'price', 'availability_badge', 'created_at')
    list_filter = (PriceRangeFilter, MissingSubcategoryFilter, 'availability', 'subcategory__category', 'subcategory')
    search_fields = ('name', 'description', 'subcategory__name', 'subcategory__category__name')
    list_editable = ('price',)
    list_per_page = 20
    date_hierarchy = 'created_at'
    readonly_fields = ('created_at', 'updated_at', 'item_preview')
    list_select_related = ('subcategory__category',)
    form = MenuItemAdminForm
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'description', 'category', 'subcategory', 'price', 'image', 'item_preview')
        }),
        ('Status', {
            'fields': ('availability',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def get_category(self, obj):
        if getattr(obj, 'category', None):
            return obj.category.name
        if obj.subcategory and obj.subcategory.category:
            return obj.subcategory.category.name
        return "-"
    get_category.short_description = 'Category'
    get_category.admin_order_field = 'subcategory__category__name'

    def get_subcategory(self, obj):
        if obj.subcategory:
            return obj.subcategory.name
        return "-"
    get_subcategory.short_description = 'Subcategory'
    get_subcategory.admin_order_field = 'subcategory__name'
    
    def item_thumbnail(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" style="width: 50px; height: 50px; object-fit: cover;" />',
                obj.image.url
            )
        return "No Image"
    item_thumbnail.short_description = 'Image'
    item_thumbnail.allow_tags = True
    
    def item_preview(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" style="max-width: 200px; max-height: 200px;" />',
                obj.image.url
            )
        return "No Image"
    item_preview.short_description = 'Preview'
    item_preview.allow_tags = True
    
    def availability_badge(self, obj):
        return format_html(
            '<span class="badge {}">{}</span>',
            'bg-success' if obj.availability else 'bg-danger',
            'Available' if obj.availability else 'Unavailable'
        )
    availability_badge.short_description = 'Status'
    availability_badge.admin_order_field = 'availability'


@admin.register(RestaurantSettings)
class RestaurantSettingsAdmin(admin.ModelAdmin):
    list_display = ('cgst_percentage', 'sgst_percentage', 'service_charge_percentage', 'updated_at')
    readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        ('Tax Settings', {
            'fields': ('cgst_percentage', 'sgst_percentage', 'service_charge_percentage')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )

    def has_add_permission(self, request):
        # Only allow a single settings row
        if RestaurantSettings.objects.exists():
            return False
        return super().has_add_permission(request)


class GalleryImageInline(admin.TabularInline):
    model = GalleryImage
    extra = 1
    fields = ( 'image', 'caption', 'display_order')


class TeamMemberInline(admin.TabularInline):
    model = TeamMember
    extra = 1
    fields = ('photo', 'name', 'position', 'display_order', 'active')


class ReviewInline(admin.TabularInline):
    model = Review
    extra = 1
    fields = ('name', 'stars', 'description', 'display_order', 'active')


@admin.register(RestaurantLanding)
class RestaurantLandingAdmin(admin.ModelAdmin):
    list_display = ('restaurant', 'hero_title', 'updated_at')
    search_fields = ('restaurant__name', 'hero_title')
    inlines = [GalleryImageInline, TeamMemberInline, ReviewInline]
    fieldsets = (
        ('Brand', {
            'fields': ('restaurant', 'logo')
        }),
        ('Hero Section', {
            'fields': ('hero_title', 'hero_subtitle', 'hero_image','hero_video', 'cta_text', 'cta_link')
        }),
        ('About Section', {
            'fields': ('about_heading', 'about_body', 'about_image')
        }),
        ('Contact', {
            'fields': ('address', 'phone', 'opening_hours')
        }),
        ('SEO', {
            'fields': ('seo_title', 'seo_description')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = ('code', 'discount_type', 'value', 'min_spend', 'active', 'used_count', 'max_uses', 'restaurant')
    list_filter = ('active', 'discount_type', 'restaurant')
    search_fields = ('code', 'description')
    readonly_fields = ('used_count', 'created_at', 'updated_at')
    fieldsets = (
        ('Coupon', {
            'fields': ('code', 'description', 'restaurant')
        }),
        ('Discount', {
            'fields': ('discount_type', 'value', 'min_spend')
        }),
        ('Validity', {
            'fields': ('active', 'start_at', 'end_at', 'max_uses', 'used_count')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )


@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = ('name', 'sku', 'unit', 'quantity', 'reorder_level', 'price_per_unit', 'is_active', 'restaurant')
    list_filter = ('is_active', 'restaurant')
    search_fields = ('name', 'sku')
    list_per_page = 25


@admin.register(MenuItemIngredient)
class MenuItemIngredientAdmin(admin.ModelAdmin):
    list_display = ('menu_item', 'inventory_item', 'quantity_needed')
    list_filter = ('menu_item__restaurant', 'inventory_item')
    search_fields = ('menu_item__name', 'inventory_item__name')


@admin.register(InventoryTransaction)
class InventoryTransactionAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'inventory_item', 'transaction_type', 'delta', 'order', 'performed_by')
    list_filter = ('transaction_type', 'inventory_item__restaurant')
    search_fields = ('inventory_item__name', 'order__id')
    readonly_fields = ('created_at',)


@admin.register(BlogPost)
class BlogPostAdmin(admin.ModelAdmin):
    list_display = ('title', 'restaurant', 'category', 'author', 'shape_pattern', 'is_published', 'created_at')
    list_filter = ('restaurant', 'is_published', 'category', 'shape_pattern')
    search_fields = ('title', 'excerpt', 'category', 'author', 'tags')
    prepopulated_fields = {'slug': ('title',)}
    list_per_page = 20


@admin.register(BlogComment)
class BlogCommentAdmin(admin.ModelAdmin):
    list_display = ('author_name', 'post', 'is_approved', 'created_at')
    list_filter = ('is_approved', 'created_at')
    search_fields = ('author_name', 'author_email', 'content', 'post__title')