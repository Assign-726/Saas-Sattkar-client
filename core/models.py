from django.db import models
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator, MaxValueValidator
from decimal import Decimal


class Zone(models.Model):
    name = models.CharField(max_length=100)
    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='zones')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='zones')

    class Meta:
        verbose_name = 'Zone'
        verbose_name_plural = 'Zones'
        ordering = ['name']
        unique_together = ['restaurant', 'branch', 'name']

    def __str__(self):
        restaurant_name = self.restaurant.name if self.restaurant else "Unknown"
        return f"{restaurant_name} - {self.name}"


class Table(models.Model):
    class TableShape(models.TextChoices):
        ROUND = 'round', _('Round')
        SQUARE = 'square', _('Square')
        RECTANGLE = 'rectangle', _('Rectangle')

    name = models.CharField(max_length=100)
    table_number = models.PositiveIntegerField(default=1)
    capacity = models.PositiveIntegerField(default=4)
    shape = models.CharField(
        max_length=20,
        choices=TableShape.choices,
        default=TableShape.ROUND,
        blank=True,
        null=True
    )
    qr_code = models.ImageField(upload_to='table_qrs', blank=True, null=True)
    zone = models.ForeignKey(Zone, on_delete=models.PROTECT, related_name='tables', null=True, blank=True)
    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='tables', null=True, blank=True)
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='tables')
    is_active = models.BooleanField(default=True)
    is_placed = models.BooleanField(default=False, help_text="Whether table is placed on visual floor plan")
    position_x = models.IntegerField(default=0, null=True, blank=True)
    position_y = models.IntegerField(default=0, null=True, blank=True)
    rotation = models.IntegerField(default=0, help_text="Rotation in degrees (0, 90, 180, 270)")
    master_table = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='linked_tables',
        help_text="Primary master table if this table is merged/linked for a party or event"
    )
    party_name = models.CharField(max_length=150, blank=True, default='', help_text="Event or party name e.g. Sharma Family Party")
    party_host = models.CharField(max_length=150, blank=True, default='', help_text="Party host customer name")
    party_phone = models.CharField(max_length=30, blank=True, default='', help_text="Party host contact number")
    party_notes = models.TextField(blank=True, default='', help_text="Special instructions / notes for party")

    class Meta:
        unique_together = (
            ('restaurant', 'zone', 'table_number'),
            ('restaurant', 'zone', 'name'),
        )
        ordering = ['zone', 'table_number']

    def __str__(self):
        zone_name = self.zone.name if self.zone else "No Zone"
        return f"{self.name} (Zone: {zone_name})"

    @classmethod
    def get_frontend_base_url(cls):
        """
        Dynamically fetch the frontend base URL from .env or settings.
        Ensures any port/host changes in .env are picked up immediately.
        """
        import os
        from django.conf import settings

        base_domain = None
        try:
            env_file = os.path.join(settings.BASE_DIR, '.env')
            if os.path.exists(env_file):
                with open(env_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('FRONTEND_URL=') and not line.startswith('#'):
                            val = line.split('=', 1)[1].strip().strip('"').strip("'")
                            if val:
                                base_domain = val
                                break
        except Exception:
            pass

        if not base_domain:
            base_domain = getattr(settings, 'FRONTEND_URL', None) or os.environ.get('FRONTEND_URL') or os.environ.get('CUSTOM_FRONTEND_URL') or 'http://localhost:3000'

        return str(base_domain).rstrip('/')

    @classmethod
    def generate_table_token(cls, restaurant_slug, branch_slug, table_number, zone_name=""):
        """
        Cryptographic HMAC-SHA256 signature for a dining table QR code.
        Binds restaurant, branch, table number, and zone using TABLE_QR_SECRET_KEY.
        """
        import hmac
        import hashlib
        from django.conf import settings

        secret = getattr(settings, 'TABLE_QR_SECRET_KEY', 'table_qr_sec_4f9a8b1c7d2e3f5a6b0c8d9e1f2a3b4c')
        r_slug = str(restaurant_slug or '').strip().lower()
        b_slug = str(branch_slug or '').strip().lower()
        t_num = str(table_number or '').strip()
        z_name = str(zone_name or '').strip().lower()

        raw_payload = f"table:{r_slug}:{b_slug}:{t_num}:{z_name}"
        return hmac.new(secret.encode('utf-8'), raw_payload.encode('utf-8'), hashlib.sha256).hexdigest()[:16]

    def get_security_token(self):
        """
        Returns the signed token for this specific table instance.
        """
        r_slug = self.restaurant.slug if (self.restaurant and self.restaurant.slug) else ''
        b_obj = self.branch or (self.zone.branch if self.zone else None)
        if not b_obj and self.restaurant:
            b_obj = self.restaurant.branches.first()
        b_slug = b_obj.slug if (b_obj and b_obj.slug) else (b_obj.name if (b_obj and b_obj.name) else '')
        z_name = self.zone.name if self.zone else ''
        return self.generate_table_token(r_slug, b_slug, self.table_number, z_name)

    def get_target_menu_url(self):
        """
        Builds the target frontend menu URL for this specific table, including:
        - Base URL from .env (FRONTEND_URL)
        - Restaurant slug
        - Table number
        - Branch slug
        - Floor/Zone name
        - Signed HMAC token (tamper-proof)
        Format: http://<domain>/<restaurant-slug>/menu?table=<table_number>&zone=<zone_name>&branch=<branch_slug>&token=<token>
        """
        from urllib.parse import urlencode

        base_domain = self.get_frontend_base_url()
        restaurant_slug = self.restaurant.slug if (self.restaurant and self.restaurant.slug) else 'default'

        params = {
            'table': str(self.table_number)
        }

        # Resolve Floor / Zone name
        if self.zone and self.zone.name:
            params['zone'] = self.zone.name

        # Resolve Branch (table branch -> zone branch -> restaurant branch)
        branch_obj = self.branch or (self.zone.branch if self.zone else None)
        if not branch_obj and self.restaurant:
            branch_obj = self.restaurant.branches.first()

        if branch_obj:
            if branch_obj.slug:
                params['branch'] = branch_obj.slug
            elif branch_obj.name:
                params['branch'] = branch_obj.name
        elif self.restaurant and self.restaurant.slug:
            params['branch'] = self.restaurant.slug

        # Attach cryptographic tamper-proof token
        params['token'] = self.get_security_token()

        query_string = urlencode(params)
        return f"{base_domain}/{restaurant_slug}/menu?{query_string}"

    def generate_qr_code(self, force=False):
        """
        Generates and assigns a QR code pointing to get_target_menu_url().
        """
        if self.qr_code and not force:
            return

        try:
            import qrcode
            import uuid
            from io import BytesIO
            from django.core.files import File

            url = self.get_target_menu_url()

            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=10,
                border=4,
            )
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")

            canvas = BytesIO()
            # pyrefly: ignore [unexpected-keyword]
            img.save(canvas, format='PNG')
            rest_id = self.restaurant.id if self.restaurant else 0
            filename = f"rest_{rest_id}_table_{self.table_number}_{uuid.uuid4().hex[:8]}.png"
            self.qr_code.save(filename, File(canvas), save=False)
        except ImportError:
            pass
        except Exception:
            pass

    def save(self, *args, **kwargs):
        force_qr = kwargs.pop('force_qr', False)
        # Check if QR code needs regeneration
        regenerate_qr = force_qr or (not self.qr_code)
        if not regenerate_qr and self.pk:
            try:
                old_instance = Table.objects.filter(pk=self.pk).values('table_number', 'zone_id', 'branch_id', 'restaurant_id').first()
                if old_instance:
                    if (old_instance['table_number'] != self.table_number or
                        old_instance['zone_id'] != self.zone_id or
                        old_instance['branch_id'] != self.branch_id or
                        old_instance['restaurant_id'] != self.restaurant_id):
                        regenerate_qr = True
            except Exception:
                pass

        if regenerate_qr:
            self.generate_qr_code(force=True)

        super().save(*args, **kwargs)

    @staticmethod
    def active_orders_prefetch():
        """Prefetch for lists of tables: ``prefetch_related(Table.active_orders_prefetch())``
        lets ``current_status`` answer without a query per table."""
        from django.db.models import Prefetch
        from django.utils import timezone
        cutoff = timezone.now() - timezone.timedelta(hours=18)
        return Prefetch(
            'orders',
            to_attr='_active_orders',
            queryset=Order.objects.exclude(status__in=[Order.OrderStatus.CANCELLED, Order.OrderStatus.COMPLETED, Order.OrderStatus.DRAFT])
                                  .exclude(payment_status=Order.PaymentStatus.FAILED)
                                  .filter(order_time__gte=cutoff)
                                  .only('id', 'table_id', 'status', 'payment_status', 'ready_at', 'order_time')
        )

    @property
    def effective_party_name(self):
        return self.party_name or (self.master_table.party_name if self.master_table else '')

    @property
    def effective_party_host(self):
        return self.party_host or (self.master_table.party_host if self.master_table else '')

    @property
    def effective_party_phone(self):
        return self.party_phone or (self.master_table.party_phone if self.master_table else '')

    @property
    def current_status(self):
        # Determine live table status based on active dining orders
        from django.utils import timezone
        now = timezone.now()
        cutoff = now - timezone.timedelta(hours=18)

        active_orders = getattr(self, '_active_orders', None)
        if active_orders is None:
            active_orders = list(
                self.orders.exclude(status__in=[Order.OrderStatus.CANCELLED, Order.OrderStatus.COMPLETED, Order.OrderStatus.DRAFT])
                           .exclude(payment_status=Order.PaymentStatus.FAILED)
                           .filter(order_time__gte=cutoff)
                           .only('id', 'table_id', 'status', 'payment_status', 'ready_at', 'order_time')
            )

        # If this table is linked to a master table and has no active orders of its own, check master table
        if not active_orders and self.master_table_id:
            master = getattr(self, 'master_table', None)
            if master:
                master_orders = getattr(master, '_active_orders', None)
                if master_orders is None:
                    master_orders = list(
                        master.orders.exclude(status__in=[Order.OrderStatus.CANCELLED, Order.OrderStatus.COMPLETED, Order.OrderStatus.DRAFT])
                                     .exclude(payment_status=Order.PaymentStatus.FAILED)
                                     .filter(order_time__gte=cutoff)
                                     .only('id', 'table_id', 'status', 'payment_status', 'ready_at', 'order_time')
                    )
                if master_orders:
                    active_orders = master_orders

        # If no active orders
        if not active_orders:
            if self.party_name or self.master_table_id:
                return 'EVENT'
            return 'BLANK'

        # 1. Any order cooking in kitchen (Pending or Preparing) -> Yellow (RUNNING_KOT)
        if any(o.status in [Order.OrderStatus.PENDING, Order.OrderStatus.PREPARING, 'P', 'PR'] for o in active_orders):
            return 'RUNNING_KOT'

        # 2. Food served, awaiting bill settlement -> Green (PRINTED)
        if any(o.status in [Order.OrderStatus.SERVED, 'S'] and o.payment_status not in [Order.PaymentStatus.PAID, 'paid', 'P'] for o in active_orders):
            return 'PRINTED'

        # 3. Bill paid / settled, table finishing up -> Orange (PAID)
        if any(o.payment_status in [Order.PaymentStatus.PAID, 'paid', 'P'] for o in active_orders):
            return 'PAID'

        # 4. Out of kitchen / ready / active dining -> Blue (RUNNING)
        return 'RUNNING'

class Category(models.Model):
    name = models.CharField(max_length=100)
    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='categories', null=True, blank=True)
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, related_name='categories', null=True, blank=True)
    description = models.TextField(blank=True, null=True)
    image = models.ImageField(upload_to='category_images', blank=True, null=True)

    class Meta:
        verbose_name_plural = 'Categories'
        ordering = ['name']

    def __str__(self):
        return self.name


class SubCategory(models.Model):
    name = models.CharField(max_length=100)
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name='subcategories')

    class Meta:
        verbose_name_plural = 'Subcategories'
        ordering = ['name']

    def __str__(self):
        return f"{self.category.name} - {self.name}"


class MenuItem(models.Model):
    class TaxType(models.TextChoices):
        GST_FOOD = 'GST_FOOD', _('GST Food & Non-Alcoholic')
        VAT_BEER = 'VAT_BEER', _('VAT Beer (State Beer VAT Rate)')
        VAT_IMFL = 'VAT_IMFL', _('VAT IMFL / Spirits (State IMFL VAT Rate)')
        VAT_WINE = 'VAT_WINE', _('VAT Wine (State Wine VAT Rate)')
        VAT_LIQUOR = 'VAT_LIQUOR', _('VAT General Liquor (State VAT Rate)')

    class FoodType(models.TextChoices):
        VEG = 'veg', _('Veg')
        NON_VEG = 'non_veg', _('Non-Veg')
        EGG = 'egg', _('Egg')
        JAIN = 'jain', _('Jain')
        VEGAN = 'vegan', _('Vegan')

    name = models.CharField(max_length=200)
    short_code = models.CharField(max_length=50, blank=True, null=True, db_index=True, help_text="Fast POS item code (e.g. 101, 301, NAAN)")
    sku = models.CharField(max_length=50, blank=True, null=True, help_text="SKU identifier e.g. BUT-4921")
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    tax_type = models.CharField(
        max_length=20,
        choices=TaxType.choices,
        default=TaxType.GST_FOOD
    )
    is_tax_inclusive = models.BooleanField(default=False)
    image = models.ImageField(upload_to='menu_images', blank=True, null=True)
    availability = models.BooleanField(default=True)
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name='menu_items', null=True, blank=True)
    subcategory = models.ForeignKey(SubCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name='menu_items')
    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='menu_items', null=True, blank=True)
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, related_name='menu_items', null=True, blank=True)

    # FSSAI Classification & Lifestyle Preferences
    food_type = models.CharField(
        max_length=20,
        choices=FoodType.choices,
        default=FoodType.VEG,
        help_text="FSSAI legal food classification"
    )
    is_jain = models.BooleanField(default=False, help_text="Jain friendly")
    is_vegan = models.BooleanField(default=False, help_text="Vegan friendly")

    # FSSAI Nutritional & Allergen Compliance
    calories = models.CharField(max_length=50, blank=True, default='', help_text="Calories (Kcal) for FSSAI compliance")
    portion_serves = models.CharField(max_length=50, blank=True, default='1 Person', help_text="Portion serving size")
    preparation_time = models.PositiveIntegerField(default=15, help_text="Estimated preparation time in minutes")
    allergens = models.JSONField(default=list, blank=True, help_text="Declared FSSAI 8 major allergens list")

    # Publishing & Variants
    publishing_channels = models.JSONField(default=list, blank=True, help_text="Publishing channels e.g. ['pos', 'qr', 'online']")
    variants_json = models.JSONField(default=list, blank=True, help_text="Variants and add-ons list")
    markup_price = models.DecimalField(max_digits=10, decimal_places=2, default=0, blank=True, help_text="Zomato / Swiggy markup price override")
    packaging_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0, blank=True, help_text="Zomato / Swiggy packaging charge")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    @property
    def is_vegetarian(self):
        return self.food_type == self.FoodType.VEG

    @property
    def is_alcoholic(self):
        if self.tax_type in [
            self.TaxType.VAT_BEER,
            self.TaxType.VAT_IMFL,
            self.TaxType.VAT_WINE,
            self.TaxType.VAT_LIQUOR,
        ]:
            return True
        cat_name = (self.category.name if self.category else '').lower()
        subcat_name = (self.subcategory.name if self.subcategory else '').lower()
        for kw in ['alcoholic', 'alcohol', 'liquor', 'beer', 'wine', 'spirits', 'bar']:
            if kw in cat_name and 'non-alcoholic' not in cat_name:
                return True
            if kw in subcat_name and 'non-alcoholic' not in subcat_name:
                return True
        return False

    def __str__(self):
        return f"{self.name} (₹{self.price})"


class Order(models.Model):
    class OrderStatus(models.TextChoices):
        DRAFT = 'DRAFT', _('Draft / Scheduled')
        PENDING = 'P', _('Pending')
        PREPARING = 'PR', _('Preparing')
        READY = 'READY', _('Ready')
        SERVED = 'S', _('Served')
        COMPLETED = 'COMPLETED', _('Completed')
        CANCELLED = 'C', _('Cancelled')

    class OrderType(models.TextChoices):
        DINE_IN = 'dine_in', _('Dine-In')
        TAKEAWAY = 'takeaway', _('Takeaway')
        DELIVERY = 'delivery', _('Delivery')

    class PaymentMethod(models.TextChoices):
        CASH = 'cash', _('Cash')
        UPI = 'upi', _('UPI')
        CARD = 'card', _('Card')
        CREDIT = 'credit', _('Credit/Pending')
        ONLINE = 'O', _('Online (Razorpay)')
        COUNTER = 'C', _('Pay at Counter')

    class PaymentStatus(models.TextChoices):
        PAID = 'paid', _('Paid')
        UNPAID = 'unpaid', _('Unpaid')
        PARTIALLY_PAID = 'partially_paid', _('Partially Paid')
        ADVANCE_PAID = 'advance_paid', _('Advance Paid')
        FAILED = 'F', _('Failed')
        COUNTER = 'C', _('Pay at Counter')
        REFUNDED = 'refunded', _('Refunded')

    customer_name = models.CharField(max_length=200, blank=True, default='Guest')
    phone = models.CharField(max_length=20, blank=True, default='')
    email = models.EmailField(blank=True, default='', max_length=254, help_text="Optional customer email")
    delivery_address = models.TextField(blank=True, default='', help_text="Optional delivery / street address")
    image = models.ImageField(upload_to='order_images', blank=True, null=True)
    table_number = models.PositiveIntegerField(null=True, blank=True, help_text="Deprecated: Use 'table' ForeignKey instead")
    table = models.ForeignKey(Table, on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    order_time = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=10,
        choices=OrderStatus.choices,
        default=OrderStatus.PENDING
    )
    order_type = models.CharField(
        max_length=20,
        choices=OrderType.choices,
        default=OrderType.DINE_IN
    )
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CASH
    )
    payment_status = models.CharField(
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PAID
    )
    payments = models.JSONField(default=list, blank=True, help_text="Split payment entries e.g. [{'method':'cash', 'amount':300.0}]")
    paid_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    due_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    razorpay_order_id = models.CharField(max_length=100, blank=True, null=True)
    razorpay_payment_id = models.CharField(max_length=100, blank=True, null=True)
    razorpay_signature = models.CharField(max_length=200, blank=True, null=True)
    security_token = models.CharField(max_length=100, blank=True, null=True, unique=True)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    round_off_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # Coupon fields
    coupon = models.ForeignKey('Coupon', on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_type = models.CharField(max_length=20, default='flat', blank=True, null=True)
    discount_value = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_reason = models.CharField(max_length=255, blank=True, null=True)
    notes = models.TextField(blank=True)
    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='orders', null=True, blank=True)
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='orders')
    branch_order_number = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    prep_minutes = models.PositiveIntegerField(null=True, blank=True)
    prep_start = models.DateTimeField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    inventory_deducted = models.BooleanField(default=False)
    gstin_no = models.CharField(max_length=100, blank=True, null=True, help_text="GST Tax Invoice Number")
    excise_bill_no = models.CharField(max_length=100, blank=True, null=True, help_text="Retail Excise Bill / Bill of Supply Number")
    party = models.ForeignKey('Party', on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    advance_receipt = models.ForeignKey('AdvanceReceipt', on_delete=models.SET_NULL, null=True, blank=True, related_name='linked_orders')
    advance_amount_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    event_date = models.DateField(null=True, blank=True)
    event_time = models.TimeField(null=True, blank=True)
    guest_count = models.PositiveIntegerField(null=True, blank=True)
    parent_order = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sub_orders',
        help_text="Master bill/order when multiple tables or sub-orders are merged"
    )
    is_master_event_order = models.BooleanField(
        default=False,
        help_text="Whether this order is the master tab for a multi-table party/event"
    )
    waive_service_charge = models.BooleanField(default=False, help_text="Whether service charge was removed from this bill upon customer request")
    is_prep_paused = models.BooleanField(default=False, help_text="Whether cooking is paused in KDS")
    paused_at = models.DateTimeField(null=True, blank=True)
    prep_elapsed_seconds = models.PositiveIntegerField(default=0, help_text="Elapsed cooking seconds tracked by KDS")
    counter = models.ForeignKey('POSCounter', on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    shift = models.ForeignKey('POSShift', on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    cashier = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='pos_orders')

    class Meta:
        ordering = ['-order_time']

    @property
    def net_payable(self):
        from decimal import Decimal
        tot = self.total_amount if (self.total_amount is not None and self.total_amount > Decimal('0.00')) else self.calculate_bill_breakdown()['final_total']
        return max(Decimal('0.00'), tot - (self.advance_amount_paid or Decimal('0.00')))

    @property
    def is_ready(self):
        from django.utils import timezone
        if self.status in [self.OrderStatus.SERVED, self.OrderStatus.COMPLETED, self.OrderStatus.CANCELLED, self.OrderStatus.DRAFT]:
            return False
        if self.status == self.OrderStatus.READY:
            return True
        if self.ready_at and self.ready_at <= timezone.now() and self.status in [self.OrderStatus.PENDING, self.OrderStatus.PREPARING]:
            return True
        return False

    @property
    def order_number(self):
        if (self.status == self.OrderStatus.DRAFT or self.status == 'DRAFT') and not self.branch_order_number:
            return f"Draft #{self.id}"
        num = self.branch_order_number if self.branch_order_number is not None else self.id
        return f"ORD-{num:04d}" if num else ""

    @property
    def customer_email(self):
        return self.email

    @customer_email.setter
    def customer_email(self, val):
        self.email = val or ''

    @property
    def display_email(self):
        """Returns email if set, otherwise extracts email from notes if present in legacy format."""
        if self.email and self.email.strip():
            return self.email.strip()
        if self.notes:
            import re
            m = re.search(r'Email:\s*([^\s|\[\],;]+@[^\s|\[\],;]+)', self.notes, re.IGNORECASE)
            if m:
                return m.group(1).strip()
            m2 = re.search(r'Email:\s*([^\s|\[\],;]+)', self.notes, re.IGNORECASE)
            if m2 and m2.group(1).strip():
                return m2.group(1).strip()
        return ''

    @property
    def display_address(self):
        """Returns delivery_address if set, otherwise extracts address from notes if present in legacy format."""
        if self.delivery_address and self.delivery_address.strip():
            return self.delivery_address.strip()
        if self.notes:
            import re
            m = re.search(r'Address:\s*([^|\[\n]+)', self.notes, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return ''

    @property
    def clean_kitchen_notes(self):
        """
        Strips out 'Order Type: ...', 'Email: ...', 'Address: ...' from notes,
        leaving only pure chef/kitchen instructions.
        Handles both multi-round [Round X] notes and single-round notes.
        """
        if not self.notes:
            return ''
        import re

        pattern = r'(\[Round\s+\d+\]:?)'
        parts = re.split(pattern, self.notes)

        cleaned_segments = []
        current_round_tag = ''

        for part in parts:
            if not part:
                continue
            if re.match(pattern, part, re.IGNORECASE):
                current_round_tag = part.strip()
                continue

            seg = part
            seg = re.sub(r'Order Type:\s*[^|\n]+', '', seg, flags=re.IGNORECASE)
            seg = re.sub(r'Email:\s*[^|\n]+', '', seg, flags=re.IGNORECASE)
            seg = re.sub(r'Address:\s*[^|\n]+', '', seg, flags=re.IGNORECASE)
            pipe_items = [p.strip() for p in seg.split('|') if p.strip()]
            cleaned_seg = ' | '.join(pipe_items).strip(' |-\n\r\t')

            if cleaned_seg:
                if current_round_tag:
                    cleaned_segments.append(f"{current_round_tag} {cleaned_seg}")
                    current_round_tag = ''
                else:
                    cleaned_segments.append(cleaned_seg)
            else:
                current_round_tag = ''

        return '\n'.join(cleaned_segments).strip()

    @property
    def rounds_count(self):
        """
        Returns number of order batches / rounds for this order.
        """
        max_batch = 1
        try:
            from django.db.models import Max
            if hasattr(self, '_prefetched_objects_cache') and 'items' in self._prefetched_objects_cache:
                batches = [it.kot_batch for it in self.items.all() if it.kot_batch is not None]
                if batches:
                    max_batch = max(max_batch, max(batches))
            else:
                agg = self.items.aggregate(max_b=Max('kot_batch'))
                if agg and agg.get('max_b'):
                    max_batch = max(max_batch, agg['max_b'])
        except Exception:
            pass
        if self.notes:
            import re
            matches = re.findall(r'\[Round\s+(\d+)\]', self.notes, re.IGNORECASE)
            for m in matches:
                try:
                    max_batch = max(max_batch, int(m))
                except ValueError:
                    pass
        return max_batch

    @property
    def rounds_breakdown(self):
        """
        Returns a structured list of rounds and their kitchen notes:
        [{'round': 1, 'note': '...'}, {'round': 2, 'note': '...'}]
        """
        if not self.notes:
            return []
        import re
        pattern = r'(\[Round\s+\d+\]:?)'
        parts = re.split(pattern, self.notes)

        breakdown = []
        current_round = 1

        for part in parts:
            if not part:
                continue
            m = re.match(r'\[Round\s+(\d+)\]:?', part, re.IGNORECASE)
            if m:
                try:
                    current_round = int(m.group(1))
                except ValueError:
                    pass
                continue

            seg = part
            seg = re.sub(r'Order Type:\s*[^|\n]+', '', seg, flags=re.IGNORECASE)
            seg = re.sub(r'Email:\s*[^|\n]+', '', seg, flags=re.IGNORECASE)
            seg = re.sub(r'Address:\s*[^|\n]+', '', seg, flags=re.IGNORECASE)
            pipe_items = [p.strip() for p in seg.split('|') if p.strip()]
            cleaned_seg = ' | '.join(pipe_items).strip(' |-\n\r\t')

            if cleaned_seg:
                breakdown.append({'round': current_round, 'note': cleaned_seg})

        return breakdown

    def calculate_estimated_prep_minutes(self):
        """
        Calculate estimated preparation time in minutes based on items in this order.
        Takes the maximum preparation time of items in the order.
        """
        prep_times = [
            getattr(it.menu_item, 'preparation_time', 15) or 15
            for it in self.items.select_related('menu_item').all()
            if it.menu_item
        ]
        if prep_times:
            return max(prep_times)
        return 15

    def get_target_prep_minutes(self):
        """
        Get or dynamically initialize target preparation minutes for this order.
        """
        if self.prep_minutes:
            return self.prep_minutes
        estimated = self.calculate_estimated_prep_minutes()
        self.prep_minutes = estimated
        try:
            Order.objects.filter(pk=self.pk).update(prep_minutes=estimated)
        except Exception:
            pass
        return estimated

    def check_item_types(self):
        """
        Returns (has_food, has_alcohol) tuple for items in this order.
        """
        items = list(self.items.select_related('menu_item', 'menu_item__category').all())
        if not items:
            return True, False

        has_food = False
        has_alcohol = False

        alcohol_keywords = {'bar_stock', 'bar stock', 'liquor', 'beer', 'wine', 'bar', 'spirits', 'cocktail', 'cocktails'}

        for item in items:
            mi = item.menu_item
            if not mi:
                has_food = True
                continue

            tax_type = (getattr(mi, 'tax_type', '') or '').upper()
            cat_obj = getattr(mi, 'category', None)
            cat_name = (cat_obj.name if cat_obj and hasattr(cat_obj, 'name') else '').lower()
            cat_slug = (getattr(cat_obj, 'slug', '') or '').lower() if cat_obj else ''

            is_alc = (
                tax_type.startswith('VAT_') or
                cat_name in alcohol_keywords or
                (cat_slug and cat_slug in alcohol_keywords) or
                any(k in cat_name for k in ['beer', 'wine', 'liquor', 'whiskey', 'vodka', 'rum', 'bar stock'])
            )

            if is_alc:
                has_alcohol = True
            else:
                has_food = True

        return has_food, has_alcohol

    def get_food_and_bar_items(self):
        """
        Returns (food_items, bar_items) lists for items in this order.
        """
        items = list(self.items.select_related('menu_item', 'menu_item__category').all())
        alcohol_keywords = {'bar_stock', 'bar stock', 'liquor', 'beer', 'wine', 'bar', 'spirits', 'cocktail', 'cocktails'}

        food_items = []
        bar_items = []

        for item in items:
            mi = item.menu_item
            if not mi:
                food_items.append(item)
                continue

            tax_type = (getattr(mi, 'tax_type', '') or '').upper()
            cat_obj = getattr(mi, 'category', None)
            cat_name = (cat_obj.name if cat_obj and hasattr(cat_obj, 'name') else '').lower()
            cat_slug = (getattr(cat_obj, 'slug', '') or '').lower() if cat_obj else ''

            is_alc = (
                tax_type.startswith('VAT_') or
                cat_name in alcohol_keywords or
                (cat_slug and cat_slug in alcohol_keywords) or
                any(k in cat_name for k in ['beer', 'wine', 'liquor', 'whiskey', 'vodka', 'rum', 'bar stock'])
            )

            if is_alc:
                bar_items.append(item)
            else:
                food_items.append(item)

        return food_items, bar_items


    def assign_invoice_numbers(self):
        """
        Conditionally generates and assigns gstin_no / excise_bill_no:
        - Food-only: gstin_no assigned, excise_bill_no = None
        - Alcohol-only: excise_bill_no assigned, gstin_no = None
        - Mixed: both gstin_no and excise_bill_no assigned
        """
        has_food, has_alcohol = self.check_item_types()
        num = self.branch_order_number if self.branch_order_number is not None else self.id
        formatted_num = f"{num:04d}" if num else ""

        current_year = self.order_time.year if self.order_time else 2026

        if has_food:
            if not self.gstin_no and formatted_num:
                self.gstin_no = f"INV-{current_year}-{formatted_num}"
        else:
            self.gstin_no = None

        if has_alcohol:
            if not self.excise_bill_no and formatted_num:
                self.excise_bill_no = f"EXC-{current_year}-{formatted_num}"
        else:
            self.excise_bill_no = None

        self.save(update_fields=['gstin_no', 'excise_bill_no'])

    @property
    def food_invoice_number(self):
        if self.gstin_no:
            return self.gstin_no
        has_food, has_alcohol = self.check_item_types()
        if has_food:
            num = self.branch_order_number if self.branch_order_number is not None else self.id
            year = self.order_time.year if self.order_time else 2026
            return f"INV-{year}-{num:04d}" if num else ""
        return ""

    @property
    def liquor_bill_number(self):
        if self.excise_bill_no:
            return self.excise_bill_no
        has_food, has_alcohol = self.check_item_types()
        if has_alcohol:
            num = self.branch_order_number if self.branch_order_number is not None else self.id
            year = self.order_time.year if self.order_time else 2026
            return f"EXC-{year}-{num:04d}" if num else ""
        return ""

    def __str__(self):
        return f"{self.order_number} - {self.customer_name} - {self.get_status_display()}"

    def resolve_and_sync_party(self):
        """
        Phone-first customer party resolver and creator.
        Prioritizes 10-digit/cleaned phone number matching so customers with identical names do not collide.
        Falls back to exact name matching only if phone is not provided.
        Automatically creates a customer Party if none exists.
        """
        if not self.restaurant:
            return None

        import re
        from core.models import Party

        cust_name = (self.customer_name or '').strip()
        is_guest = not cust_name or cust_name.lower() in ['guest', 'walk-in', 'walk-in guest', 'walk-in-guest', 'walkin']

        raw_phone = str(self.phone or '').strip()
        digits = re.sub(r'\D', '', raw_phone)
        clean_phone = digits[-10:] if len(digits) >= 10 else digits

        # Anonymous walk-in with no phone number: do not create or link a party
        if is_guest and (not clean_phone or len(clean_phone) < 7):
            return None

        party = None
        base_qs = Party.objects.filter(restaurant=self.restaurant)

        # 1. Primary Lookup: By Phone Number (clean 7+ digits)
        if clean_phone and len(clean_phone) >= 7:
            phone_matches = base_qs.filter(phone__icontains=clean_phone)
            if self.branch:
                party = phone_matches.filter(branch=self.branch).first() or phone_matches.first()
            else:
                party = phone_matches.first()
        elif not is_guest:
            # 2. Secondary Lookup: By Name (ONLY when phone number was NOT provided)
            name_matches = base_qs.filter(name__iexact=cust_name)
            if self.branch:
                party = name_matches.filter(branch=self.branch).first() or name_matches.first()
            else:
                party = name_matches.first()

        # 3. If Party found: update any missing / richer details
        if party:
            updated = []
            if clean_phone and (not party.phone or len(str(party.phone)) < len(clean_phone)):
                party.phone = clean_phone
                updated.append('phone')
            if not is_guest and cust_name and party.name != cust_name:
                if party.name.lower().startswith('customer') or len(cust_name) > len(party.name):
                    party.name = cust_name
                    updated.append('name')
            if self.branch and not party.branch:
                party.branch = self.branch
                updated.append('branch')
            if updated:
                party.save(update_fields=updated)
        else:
            # 4. Auto-create new customer Party
            party_name = cust_name if not is_guest else (f"Customer {clean_phone}" if clean_phone else "Customer")
            gstin_val = (self.gstin_no or '').strip()
            party = Party.objects.create(
                restaurant=self.restaurant,
                branch=self.branch,
                name=party_name,
                party_type=Party.PartyType.CUSTOMER,
                phone=clean_phone or raw_phone,
                gstin=gstin_val if len(gstin_val) == 15 else '',
                gst_registration_type='regular' if len(gstin_val) == 15 else Party.GstRegistrationType.UNREGISTERED,
                balance_type=Party.BalanceType.RECEIVABLE
            )

        self.party = party
        return party

    def save(self, *args, **kwargs):
        import uuid
        from django.db import transaction
        from django.db.models import Max

        if not self.security_token:
            self.security_token = str(uuid.uuid4())

        # Auto-assign branch_order_number per restaurant if missing (only for confirmed/non-draft orders)
        if not self.branch_order_number and self.restaurant:
            if self.status != self.OrderStatus.DRAFT and self.status != 'DRAFT':
                with transaction.atomic():
                    max_num = Order.objects.filter(restaurant=self.restaurant).aggregate(Max('branch_order_number'))['branch_order_number__max'] or 0
                    self.branch_order_number = max_num + 1

        # Sync table_number from Table object if present
        if self.table and not self.table_number:
            self.table_number = self.table.table_number

        # Auto-link or auto-create Party object using phone-first identification
        if not self.party and self.restaurant:
            self.resolve_and_sync_party()
        elif self.party and self.phone and not self.party.phone:
            import re
            digits = re.sub(r'\D', '', str(self.phone))
            if digits:
                self.party.phone = digits[-10:] if len(digits) >= 10 else digits
                self.party.save(update_fields=['phone'])

        # Ensure draft orders always default to unpaid unless advance paid
        if (self.status == self.OrderStatus.DRAFT or self.status == 'DRAFT') and (not self.advance_amount_paid or self.advance_amount_paid <= 0):
            if not self.paid_amount or self.paid_amount <= 0:
                self.payment_status = self.PaymentStatus.UNPAID
                update_fields = kwargs.get('update_fields')
                if update_fields is not None and 'payment_status' not in update_fields:
                    kwargs['update_fields'] = list(update_fields) + ['payment_status']

        super().save(*args, **kwargs)

        # Handle automatic inventory deduction or restoration
        update_fields = kwargs.get('update_fields')
        if update_fields and 'inventory_deducted' in update_fields:
            return

        if self.status in ['P', 'PR', 'S'] and self.payment_status not in ['unpaid', 'U', 'F'] and not self.inventory_deducted:
            self.deduct_inventory()
        elif self.status == 'C' and self.inventory_deducted:
            self.restore_inventory()

    def deduct_inventory(self):
        if self.inventory_deducted:
            return
        if not self.items.exists():
            return

        from django.db import transaction
        with transaction.atomic():
            for order_item in self.items.all():
                ingredients = MenuItemIngredient.objects.filter(menu_item=order_item.menu_item)
                if ingredients.exists():
                    for ing in ingredients:
                        deduction = float(ing.quantity_needed) * float(order_item.quantity)
                        delta_int = max(1, int(round(deduction)))
                        ing.inventory_item.adjust(-delta_int)

                        InventoryTransaction.objects.create(
                            inventory_item=ing.inventory_item,
                            transaction_type=InventoryTransaction.TransactionType.ORDER,
                            delta=-delta_int,
                            order=self
                        )

                        if ing.inventory_item.quantity <= ing.inventory_item.reorder_level:
                            try:
                                from core.sse import publish
                                if self.restaurant and self.restaurant.slug:
                                    publish(self.restaurant.slug, {
                                        'type': 'low_stock_warning',
                                        'item_id': ing.inventory_item.id,
                                        'item_name': ing.inventory_item.name,
                                        'quantity': ing.inventory_item.quantity
                                    })
                            except Exception:
                                pass
                else:
                    # Direct inventory item fallback match
                    direct_items = InventoryItem.objects.filter(restaurant=self.restaurant, name__iexact=order_item.menu_item.name)
                    if self.branch:
                        direct_items = direct_items.filter(branch=self.branch)
                    for direct_item in direct_items:
                        qty_deduct = int(order_item.quantity)
                        direct_item.adjust(-qty_deduct)

                        InventoryTransaction.objects.create(
                            inventory_item=direct_item,
                            transaction_type=InventoryTransaction.TransactionType.ORDER,
                            delta=-qty_deduct,
                            order=self
                        )

            self.inventory_deducted = True
            self.save(update_fields=['inventory_deducted'])

    def restore_inventory(self):
        if not self.inventory_deducted:
            return

        from django.db import transaction
        with transaction.atomic():
            for order_item in self.items.all():
                ingredients = MenuItemIngredient.objects.filter(menu_item=order_item.menu_item)
                for ing in ingredients:
                    total_restore = int(ing.quantity_needed * order_item.quantity)
                    ing.inventory_item.adjust(total_restore)

                    InventoryTransaction.objects.create(
                        inventory_item=ing.inventory_item,
                        transaction_type=InventoryTransaction.TransactionType.CANCEL,
                        delta=total_restore,
                        order=self
                    )

            self.inventory_deducted = False
            self.save(update_fields=['inventory_deducted'])

    def calculate_bill_breakdown(self, bill_type='combined', items_override=None):
        from decimal import Decimal
        food_subtotal = Decimal('0.00')
        beer_subtotal = Decimal('0.00')
        imfl_subtotal = Decimal('0.00')
        wine_subtotal = Decimal('0.00')
        gen_liquor_subtotal = Decimal('0.00')

        # Fetch tax rates BEFORE the loop so inclusive items can reverse-calculate
        settings_obj = RestaurantSettings.get_settings(self.restaurant)
        from core.request_cache import memo
        branch_obj = self.branch or (memo(('first_branch', self.restaurant_id), lambda: self.restaurant.branches.first())
                                     if self.restaurant else None)

        if settings_obj and settings_obj.cgst_percentage is not None:
            cgst_pct = Decimal(str(settings_obj.cgst_percentage)) if bill_type != 'liquor' else Decimal('0.00')
        elif branch_obj:
            if branch_obj.gst_registration_type == 'R18' or branch_obj.is_hotel_tariff_7500_plus:
                cgst_pct = Decimal('9.00') if bill_type != 'liquor' else Decimal('0.00')
            elif branch_obj.gst_registration_type in ['C', 'U']:
                cgst_pct = Decimal('0.00')
            else:
                cgst_pct = Decimal('2.50') if bill_type != 'liquor' else Decimal('0.00')
        else:
            cgst_pct = Decimal('2.50') if bill_type != 'liquor' else Decimal('0.00')

        if settings_obj and settings_obj.sgst_percentage is not None:
            sgst_pct = Decimal(str(settings_obj.sgst_percentage)) if bill_type != 'liquor' else Decimal('0.00')
        elif branch_obj:
            if branch_obj.gst_registration_type == 'R18' or branch_obj.is_hotel_tariff_7500_plus:
                sgst_pct = Decimal('9.00') if bill_type != 'liquor' else Decimal('0.00')
            elif branch_obj.gst_registration_type in ['C', 'U']:
                sgst_pct = Decimal('0.00')
            else:
                sgst_pct = Decimal('2.50') if bill_type != 'liquor' else Decimal('0.00')
        else:
            sgst_pct = Decimal('2.50') if bill_type != 'liquor' else Decimal('0.00')

        if getattr(self, 'waive_service_charge', False):
            service_pct = Decimal('0.00')
        elif branch_obj and branch_obj.service_charge_percentage is not None:
            service_pct = Decimal(str(branch_obj.service_charge_percentage)) if bill_type != 'liquor' else Decimal('0.00')
        else:
            service_pct = Decimal(str(settings_obj.service_charge_percentage or 0)) if bill_type != 'liquor' else Decimal('0.00')

        vat_beer_pct = Decimal(str(branch_obj.vat_beer_rate if branch_obj and getattr(branch_obj, 'vat_beer_rate', None) is not None else 60.00)) if bill_type != 'food' else Decimal('0.00')
        vat_imfl_pct = Decimal(str(branch_obj.vat_imfl_rate if branch_obj and getattr(branch_obj, 'vat_imfl_rate', None) is not None else 65.00)) if bill_type != 'food' else Decimal('0.00')
        vat_wine_pct = Decimal(str(branch_obj.vat_wine_rate if branch_obj and getattr(branch_obj, 'vat_wine_rate', None) is not None else 60.00)) if bill_type != 'food' else Decimal('0.00')

        # General liquor rate: check branch.vat_gen_pct -> settings.vat_gen_pct -> settings.vat_percentage -> 20.00
        b_gen = getattr(branch_obj, 'vat_gen_pct', None) if branch_obj else None
        s_gen = getattr(settings_obj, 'vat_gen_pct', None) or getattr(settings_obj, 'vat_percentage', None)
        gen_rate_val = b_gen if b_gen is not None else (s_gen if s_gen is not None else Decimal('20.00'))
        vat_gen_pct = Decimal(str(gen_rate_val)) if bill_type != 'food' else Decimal('0.00')

        # VAT surcharge / Cess percentage: branch.vat_surcharge_pct -> settings.vat_surcharge_pct -> 0.00
        b_sur = getattr(branch_obj, 'vat_surcharge_pct', None) if branch_obj else None
        s_sur = getattr(settings_obj, 'vat_surcharge_pct', None)
        sur_rate_val = b_sur if b_sur is not None else (s_sur if s_sur is not None else Decimal('0.00'))
        vat_surcharge_pct = Decimal(str(sur_rate_val)) if bill_type != 'food' else Decimal('0.00')

        # VAT surcharge name (e.g. 'Cow Cess', 'State Surcharge')
        b_sur_name = getattr(branch_obj, 'vat_surcharge_name', None) if branch_obj else None
        s_sur_name = getattr(settings_obj, 'vat_surcharge_name', None)
        vat_surcharge_name = (b_sur_name or s_sur_name or 'VAT Surcharge').strip()
        if not vat_surcharge_name:
            vat_surcharge_name = 'VAT Surcharge'

        def get_effective_vat(base_rate):
            if vat_surcharge_pct > Decimal('0.00'):
                return base_rate + ((base_rate * vat_surcharge_pct) / Decimal('100.00'))
            return base_rate

        items_iterable = items_override if items_override is not None else self.items.all()
        for item in items_iterable:
            line_total = item.subtotal
            ttype = item.menu_item.tax_type if item.menu_item else MenuItem.TaxType.GST_FOOD
            is_inclusive = item.menu_item.is_tax_inclusive if item.menu_item else False

            # For tax-inclusive items, reverse-calculate using effective rate
            if is_inclusive:
                if ttype == MenuItem.TaxType.VAT_BEER:
                    rate = get_effective_vat(vat_beer_pct)
                elif ttype == MenuItem.TaxType.VAT_IMFL:
                    rate = get_effective_vat(vat_imfl_pct)
                elif ttype == MenuItem.TaxType.VAT_WINE:
                    rate = get_effective_vat(vat_wine_pct)
                elif ttype == MenuItem.TaxType.VAT_LIQUOR:
                    rate = get_effective_vat(vat_gen_pct)
                else:
                    rate = cgst_pct + sgst_pct
                # Taxable Value = (Inclusive Amount × 100) / (100 + Tax Rate)
                if rate > Decimal('0'):
                    line_total = (line_total * Decimal('100')) / (Decimal('100') + rate)

            if bill_type == 'food':
                if ttype == MenuItem.TaxType.GST_FOOD:
                    food_subtotal += line_total
            elif bill_type == 'liquor':
                if ttype == MenuItem.TaxType.VAT_BEER:
                    beer_subtotal += line_total
                elif ttype == MenuItem.TaxType.VAT_IMFL:
                    imfl_subtotal += line_total
                elif ttype == MenuItem.TaxType.VAT_WINE:
                    wine_subtotal += line_total
                elif ttype == MenuItem.TaxType.VAT_LIQUOR:
                    gen_liquor_subtotal += line_total
            else: # combined
                if ttype == MenuItem.TaxType.VAT_BEER:
                    beer_subtotal += line_total
                elif ttype == MenuItem.TaxType.VAT_IMFL:
                    imfl_subtotal += line_total
                elif ttype == MenuItem.TaxType.VAT_WINE:
                    wine_subtotal += line_total
                elif ttype == MenuItem.TaxType.VAT_LIQUOR:
                    gen_liquor_subtotal += line_total
                else:
                    food_subtotal += line_total

        total_liquor_subtotal = beer_subtotal + imfl_subtotal + wine_subtotal + gen_liquor_subtotal
        gross_subtotal = food_subtotal + total_liquor_subtotal

        discount = Decimal('0.00')
        if self.discount_value and Decimal(str(self.discount_value)) > Decimal('0.00'):
            if self.discount_type == 'percentage':
                discount = (gross_subtotal * (Decimal(str(self.discount_value)) / Decimal('100.00'))).quantize(Decimal('0.01'))
            else:
                discount = min(gross_subtotal, Decimal(str(self.discount_value)))
        elif self.discount_amount and self.discount_amount > Decimal('0.00'):
            discount = Decimal(str(self.discount_amount))
        elif self.coupon:
            try:
                if self.coupon.is_valid_for(gross_subtotal, restaurant=self.restaurant):
                    discount = Decimal(str(self.coupon.calculate_discount(gross_subtotal)))
                else:
                    discount = Decimal('0.00')
            except Exception:
                discount = Decimal('0.00')
        discount = max(Decimal('0.00'), min(discount, gross_subtotal))

        sc_taxable = getattr(settings_obj, 'service_charge_taxable', True)
        coupon_before_tax = getattr(settings_obj, 'coupon_applies_before_tax', True)

        if coupon_before_tax and gross_subtotal > Decimal('0.00'):
            disc_factor = (gross_subtotal - discount) / gross_subtotal
            discounted_food_subtotal = (food_subtotal * disc_factor).quantize(Decimal('0.01'))
            discounted_beer = (beer_subtotal * disc_factor).quantize(Decimal('0.01'))
            discounted_imfl = (imfl_subtotal * disc_factor).quantize(Decimal('0.01'))
            discounted_wine = (wine_subtotal * disc_factor).quantize(Decimal('0.01'))
            discounted_gen_liquor = (gen_liquor_subtotal * disc_factor).quantize(Decimal('0.01'))
        else:
            discounted_food_subtotal = food_subtotal
            discounted_beer = beer_subtotal
            discounted_imfl = imfl_subtotal
            discounted_wine = wine_subtotal
            discounted_gen_liquor = gen_liquor_subtotal

        discounted_liquor_subtotal = discounted_beer + discounted_imfl + discounted_wine + discounted_gen_liquor
        net_subtotal = discounted_food_subtotal + discounted_liquor_subtotal

        # Custom / Additional Charges Calculation
        custom_charges_list = []
        custom_charges_total = Decimal('0.00')
        taxable_custom_charges_base = Decimal('0.00')

        if self.restaurant:
            from core.models import CustomCharge
            charges_qs = CustomCharge.objects.filter(
                restaurant=self.restaurant,
                is_active=True,
                is_auto_applied=True
            )
            if self.branch:
                from django.db.models import Q
                charges_qs = charges_qs.filter(Q(branch=self.branch) | Q(branch__isnull=True))
            else:
                charges_qs = charges_qs.filter(branch__isnull=True)
            from core.request_cache import memo
            charges_qs = memo(('charges', self.restaurant_id, self.branch_id), lambda: list(charges_qs))
            def norm_ot(val):
                return str(val or 'dine_in').lower().strip().replace(' ', '').replace('-', '_')

            curr_ot = norm_ot(self.order_type)
            for cc in charges_qs:
                raw_types = cc.applied_order_types or ['dine_in', 'takeaway', 'delivery']
                if not isinstance(raw_types, list):
                    raw_types = [raw_types]
                types = [norm_ot(t) for t in raw_types]
                if curr_ot in types:
                    amt = cc.calculate_amount(net_subtotal)
                    custom_charges_total += amt
                    if cc.is_taxable:
                        taxable_custom_charges_base += amt
                    custom_charges_list.append({
                        'name': cc.charge_name,
                        'type': cc.charge_type,
                        'value': float(cc.value),
                        'amount': amt,
                        'is_taxable': cc.is_taxable,
                    })

        service_charge = ((discounted_food_subtotal * service_pct) / Decimal('100')).quantize(Decimal('0.01')) if bill_type != 'liquor' else Decimal('0.00')

        if sc_taxable and bill_type != 'liquor':
            gst_base = discounted_food_subtotal + service_charge + taxable_custom_charges_base
        else:
            gst_base = discounted_food_subtotal + taxable_custom_charges_base

        cgst = ((gst_base * cgst_pct) / Decimal('100')).quantize(Decimal('0.01'))
        sgst = ((gst_base * sgst_pct) / Decimal('100')).quantize(Decimal('0.01'))

        # Base VAT per category
        beer_vat = ((discounted_beer * vat_beer_pct) / Decimal('100')).quantize(Decimal('0.01')) if bill_type != 'food' else Decimal('0.00')
        imfl_vat = ((discounted_imfl * vat_imfl_pct) / Decimal('100')).quantize(Decimal('0.01')) if bill_type != 'food' else Decimal('0.00')
        wine_vat = ((discounted_wine * vat_wine_pct) / Decimal('100')).quantize(Decimal('0.01')) if bill_type != 'food' else Decimal('0.00')
        gen_vat = ((discounted_gen_liquor * vat_gen_pct) / Decimal('100')).quantize(Decimal('0.01')) if bill_type != 'food' else Decimal('0.00')
        
        # Sum Base VAT & Apply Surcharge
        total_base_vat = beer_vat + imfl_vat + wine_vat + gen_vat
        vat_surcharge = ((total_base_vat * vat_surcharge_pct) / Decimal('100')).quantize(Decimal('0.01')) if bill_type != 'food' else Decimal('0.00')
        vat_amount = total_base_vat + vat_surcharge

        from decimal import ROUND_HALF_UP
        raw_total = net_subtotal + service_charge + custom_charges_total + cgst + sgst + vat_amount
        rounded_total = raw_total.quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        round_off_amount = (rounded_total - raw_total).quantize(Decimal('0.01'))

        return {
            'bill_type': bill_type,
            'food_subtotal': food_subtotal.quantize(Decimal('0.01')),
            'beer_subtotal': beer_subtotal.quantize(Decimal('0.01')),
            'imfl_subtotal': imfl_subtotal.quantize(Decimal('0.01')),
            'wine_subtotal': wine_subtotal.quantize(Decimal('0.01')),
            'gen_liquor_subtotal': gen_liquor_subtotal.quantize(Decimal('0.01')),
            'liquor_subtotal': total_liquor_subtotal.quantize(Decimal('0.01')),
            'gross_subtotal': gross_subtotal.quantize(Decimal('0.01')),
            'discounted_food_subtotal': discounted_food_subtotal.quantize(Decimal('0.01')),
            'discounted_liquor_subtotal': discounted_liquor_subtotal.quantize(Decimal('0.01')),
            'discount_amount': discount.quantize(Decimal('0.01')),
            'custom_charges_total': custom_charges_total.quantize(Decimal('0.01')),
            'custom_charges_list': custom_charges_list,
            'service_charge': service_charge.quantize(Decimal('0.01')),
            'service_charge_percentage': service_pct,
            'gst_base': gst_base.quantize(Decimal('0.01')),
            'cgst_percentage': cgst_pct,
            'sgst_percentage': sgst_pct,
            'vat_beer_percentage': vat_beer_pct,
            'vat_imfl_percentage': vat_imfl_pct,
            'vat_wine_percentage': vat_wine_pct,
            'vat_gen_percentage': vat_gen_pct,
            'vat_percentage': vat_gen_pct,
            'vat_surcharge_name': vat_surcharge_name,
            'vat_surcharge_percentage': vat_surcharge_pct,
            'cgst': cgst.quantize(Decimal('0.01')),
            'sgst': sgst.quantize(Decimal('0.01')),
            'beer_vat': beer_vat.quantize(Decimal('0.01')),
            'imfl_vat': imfl_vat.quantize(Decimal('0.01')),
            'wine_vat': wine_vat.quantize(Decimal('0.01')),
            'gen_vat': gen_vat.quantize(Decimal('0.01')),
            'total_base_vat': total_base_vat.quantize(Decimal('0.01')),
            'vat_surcharge': vat_surcharge.quantize(Decimal('0.01')),
            'vat': vat_amount.quantize(Decimal('0.01')),
            'raw_total': raw_total.quantize(Decimal('0.01')),
            'round_off_amount': round_off_amount,
            'final_total': rounded_total.quantize(Decimal('0.01')),
        }

    def update_total(self):
        from decimal import Decimal
        breakdown = self.calculate_bill_breakdown()
        self.discount_amount = breakdown['discount_amount']
        self.subtotal = breakdown['gross_subtotal']
        self.tax_amount = breakdown['cgst'] + breakdown['sgst'] + breakdown['vat']
        self.round_off_amount = breakdown['round_off_amount']
        self.total_amount = breakdown['final_total']
        paid = Decimal(str(self.paid_amount or 0))
        tot = Decimal(str(self.total_amount or 0))
        if paid > 0:
            self.due_amount = max(Decimal('0.00'), tot - paid)
            if self.due_amount > 0 and self.payment_status == self.PaymentStatus.PAID:
                self.payment_status = self.PaymentStatus.PARTIALLY_PAID
            elif self.due_amount <= 0:
                self.payment_status = self.PaymentStatus.PAID
        else:
            self.due_amount = tot
        self.save()


class OrderItem(models.Model):
    class DiscountType(models.TextChoices):
        PERCENTAGE = 'percentage', _('Percentage (%)')
        FLAT = 'flat', _('Flat Amount (₹)')

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    menu_item = models.ForeignKey(MenuItem, on_delete=models.PROTECT, related_name='order_items')
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    notes = models.TextField(blank=True)
    discount_type = models.CharField(max_length=20, choices=DiscountType.choices, default=DiscountType.FLAT, blank=True, null=True)
    discount_value = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    discount_reason = models.CharField(max_length=100, blank=True, null=True)
    kot_batch = models.PositiveIntegerField(default=1, help_text="Incremental kitchen print batch number (1, 2, 3...)")
    is_kot_printed = models.BooleanField(default=False, help_text="Whether this item has been sent/printed to the kitchen")
    is_prepared = models.BooleanField(default=False, help_text="Whether this item has been marked prepared in KDS")
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"{self.quantity}x {self.menu_item.name} for Order #{self.order.id}"

    @property
    def gross_subtotal(self):
        from decimal import Decimal
        up = self.unit_price if self.unit_price is not None else (self.menu_item.price if self.menu_item else Decimal('0.00'))
        return self.quantity * Decimal(str(up))

    @property
    def calc_discount_amount(self):
        from decimal import Decimal
        gross = self.gross_subtotal
        if self.discount_amount and Decimal(str(self.discount_amount)) > 0:
            return min(gross, Decimal(str(self.discount_amount)))
        val = Decimal(str(self.discount_value or 0))
        if val <= 0:
            return Decimal('0.00')
        if self.discount_type == 'percentage':
            return (gross * (val / Decimal('100.00'))).quantize(Decimal('0.01'))
        return min(gross, val)

    @property
    def net_subtotal(self):
        from decimal import Decimal
        return max(Decimal('0.00'), self.gross_subtotal - self.calc_discount_amount)

    @property
    def subtotal(self):
        return self.net_subtotal

    def save(self, *args, **kwargs):
        if not self.unit_price and self.menu_item:
            self.unit_price = self.menu_item.price
        if not self.discount_amount and self.discount_value:
            self.discount_amount = self.calc_discount_amount
        super().save(*args, **kwargs)
        self.order.update_total()

    def delete(self, *args, **kwargs):
        order = self.order
        super().delete(*args, **kwargs)
        order.update_total()


from decimal import Decimal


class RestaurantSettings(models.Model):
    restaurant = models.OneToOneField('super_admin.Restaurant', on_delete=models.CASCADE, related_name='settings', null=True, blank=True)
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='settings')
    cgst_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text="CGST percentage e.g. 2.50")
    sgst_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text="SGST percentage e.g. 2.50")
    service_charge_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text="Service charge percentage e.g. 5.00")
    vat_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=20.00, help_text="VAT percentage for liquor e.g. 20.00")
    vat_gen_pct = models.DecimalField(max_digits=5, decimal_places=2, default=20.00, help_text="General/unclassified liquor rate")
    vat_surcharge_name = models.CharField(max_length=100, default='VAT Surcharge', blank=True, help_text="State-specific name for VAT surcharge/cess e.g. Cow Cess, Surcharge")
    vat_surcharge_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, help_text="Government Cess/Surcharge applied on VAT amount")
    service_charge_taxable = models.BooleanField(default=True, help_text="If true, service charge is included in GST base")
    coupon_applies_before_tax = models.BooleanField(default=True, help_text="If true, coupon discount is applied before tax calculation")
    notification_sound = models.FileField(upload_to='sounds/', blank=True, null=True)
    
    # Razorpay Gateway settings
    razorpay_key_id = models.CharField(max_length=255, blank=True, null=True)
    razorpay_key_secret = models.CharField(max_length=255, blank=True, null=True)
    enable_online_payment = models.BooleanField(default=False)
    enable_pay_at_counter = models.BooleanField(default=True)
    enable_customer_ordering = models.BooleanField(default=True, help_text="Allow customers to place orders directly from the digital menu")
    
    # Printing & POS Template settings
    class BillTemplate(models.TextChoices):
        THERMAL = 'thermal', 'Thermal POS Receipt (80mm)'
        STANDARD = 'standard', 'Standard Modern Invoice (A4)'

    class KotTemplate(models.TextChoices):
        THERMAL = 'thermal', 'Thermal POS KOT (80mm)'
        STANDARD = 'standard', 'Standard Kitchen Order Ticket'

    bill_template = models.CharField(
        max_length=20,
        choices=BillTemplate.choices,
        default=BillTemplate.THERMAL,
        help_text="Default print template for customer bills/invoices"
    )
    kot_template = models.CharField(
        max_length=20,
        choices=KotTemplate.choices,
        default=KotTemplate.THERMAL,
        help_text="Default print template for Kitchen Order Tickets (KOT)"
    )
    manager_pin = models.CharField(
        max_length=10,
        default='1234',
        help_text="4-digit authorization PIN for voids and credit notes"
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Tally Accounting Voucher Export Settings
    # ─────────────────────────────────────────────────────────────────────────
    class TallyVoucherNumberSource(models.TextChoices):
        ORDER_NUMBER   = 'order_number',   'Order Number (ORD-0001)'
        INVOICE_NUMBER = 'invoice_number', 'GST Invoice Number (INV-2026-0001)'

    class TallyChangeMode(models.TextChoices):
        ACCOUNTING_INVOICE = 'Accounting Invoice', 'Accounting Invoice'
        AS_VOUCHER         = 'As Voucher',         'As Voucher'

    # Voucher configuration
    tally_voucher_type = models.CharField(
        max_length=50, default='Sales',
        help_text="TallyPrime voucher type name (e.g. 'Sales' or 'POS Sales')"
    )
    tally_voucher_number_source = models.CharField(
        max_length=20,
        choices=TallyVoucherNumberSource.choices,
        default=TallyVoucherNumberSource.ORDER_NUMBER,
        help_text="Column used as the Voucher Number in TallyPrime import"
    )
    tally_change_mode = models.CharField(
        max_length=30,
        choices=TallyChangeMode.choices,
        default=TallyChangeMode.ACCOUNTING_INVOICE,
        help_text="TallyPrime 'Change Mode' column value"
    )

    # Dr Ledger names (Payment / Debit side)
    tally_cash_ledger         = models.CharField(max_length=100, default='Cash A/c',            help_text="Ledger name for Cash payments in TallyPrime")
    tally_upi_ledger          = models.CharField(max_length=100, default='UPI A/c',             help_text="Ledger name for UPI payments in TallyPrime")
    tally_card_ledger         = models.CharField(max_length=100, default='Card A/c',            help_text="Ledger name for Card payments in TallyPrime")
    tally_online_ledger       = models.CharField(max_length=100, default='Online Order A/c',    help_text="Ledger name for Online (Razorpay) payments in TallyPrime")
    tally_zomato_ledger       = models.CharField(max_length=100, default='Zomato A/c',          help_text="Ledger name for Zomato aggregator orders in TallyPrime")
    tally_swiggy_ledger       = models.CharField(max_length=100, default='Swiggy A/c',          help_text="Ledger name for Swiggy aggregator orders in TallyPrime")
    tally_credit_ledger       = models.CharField(max_length=100, default='Sundry Debtors',      help_text="Ledger name for Credit / Party receivable in TallyPrime")

    # Cr Ledger names (Sales / Tax / Charges side)
    tally_sales_ledger        = models.CharField(max_length=100, default='Sales @ 5%',          help_text="Ledger name for Food Sales in TallyPrime")
    tally_liquor_sales_ledger = models.CharField(max_length=100, default='Liquor Sales A/c',    help_text="Ledger name for Liquor / VAT Sales in TallyPrime")
    tally_cgst_ledger         = models.CharField(max_length=100, default='Output CGST',         help_text="Ledger name for Output CGST in TallyPrime")
    tally_sgst_ledger         = models.CharField(max_length=100, default='Output SGST',         help_text="Ledger name for Output SGST in TallyPrime")
    tally_vat_ledger          = models.CharField(max_length=100, default='Output VAT',          help_text="Ledger name for Output VAT (Liquor) in TallyPrime")
    tally_discount_ledger     = models.CharField(max_length=100, default='Discount Allowed on Sales',      help_text="Ledger name for Discount Dr rows in TallyPrime")
    tally_extra_charges_ledger= models.CharField(max_length=100, default='Extra Charges / Packaging A/c', help_text="Ledger name for packaging/service extra charges in TallyPrime")
    tally_round_off_ledger    = models.CharField(max_length=100, default='Round Off',           help_text="Ledger name for Round Off Dr/Cr in TallyPrime")

    # Tally Purchase Voucher Export Configuration
    tally_purchase_voucher_type    = models.CharField(max_length=50, default='Purchase',            help_text="TallyPrime voucher type for purchases (e.g. 'Purchase')")
    tally_purchase_bank_ledger     = models.CharField(max_length=100, default='Bank A/c',           help_text="Ledger name for Bank purchases in TallyPrime")
    tally_default_purchase_ledger  = models.CharField(max_length=100, default='Purchase A/c',       help_text="Default purchase expense ledger if category is unset")
    tally_purchase_category_prefix = models.CharField(max_length=50, default='Purchase - ',         help_text="Prefix for category expense ledgers (e.g. 'Purchase - Vegetables')")
    tally_input_cgst_ledger        = models.CharField(max_length=100, default='Input CGST',          help_text="Ledger name for Input CGST in TallyPrime")
    tally_input_sgst_ledger        = models.CharField(max_length=100, default='Input SGST',          help_text="Ledger name for Input SGST in TallyPrime")
    tally_input_igst_ledger        = models.CharField(max_length=100, default='Input IGST',          help_text="Ledger name for Input IGST in TallyPrime")
    tally_discount_received_ledger = models.CharField(max_length=100, default='Discount Received',  help_text="Ledger name for Discount Received (Cr) in TallyPrime")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'Restaurant Settings'
        ordering = ['-updated_at']

    def __str__(self):
        restaurant_name = self.restaurant.name if self.restaurant else "Global"
        return f"Settings for {restaurant_name}: CGST {self.cgst_percentage}%, SGST {self.sgst_percentage}%, Service {self.service_charge_percentage}%"

    @classmethod
    def get_settings(cls, restaurant=None):
        from core.request_cache import memo
        return memo(('settings', getattr(restaurant, 'pk', restaurant)), lambda: cls._load_settings(restaurant))

    @classmethod
    def _load_settings(cls, restaurant=None):
        if restaurant is None:
            obj = cls.objects.filter(restaurant__isnull=True).first() or cls.objects.first()
            if obj:
                return obj
            return cls.objects.create(cgst_percentage=0, sgst_percentage=0, service_charge_percentage=0)

        cgst_default = Decimal('2.50')
        sgst_default = Decimal('2.50')

        if restaurant:
            branch = restaurant.branches.first()
            if branch:
                if branch.gst_registration_type == 'R18' or branch.is_hotel_tariff_7500_plus:
                    cgst_default = Decimal('9.00')
                    sgst_default = Decimal('9.00')
                elif branch.gst_registration_type in ['C', 'U']:
                    cgst_default = Decimal('0.00')
                    sgst_default = Decimal('0.00')
                else: # R5
                    cgst_default = Decimal('2.50')
                    sgst_default = Decimal('2.50')

        obj, created = cls.objects.get_or_create(
            restaurant=restaurant,
            defaults={'cgst_percentage': cgst_default, 'sgst_percentage': sgst_default, 'service_charge_percentage': 0}
        )
        return obj


class DocumentSequenceSetting(models.Model):
    class DocumentType(models.TextChoices):
        SALES = 'sales', _('Sales / POS')
        PURCHASE = 'purchase', _('Purchase')
        RECEIPT = 'receipt', _('Advance Receipt')
        CHALLAN = 'challan', _('Delivery Challan')
        VAT_BILL = 'vat_bill', _('VAT Liquor Bill')
        PURCHASE_ORDER = 'purchase_order', _('Purchase Order')
        GRN = 'grn', _('Goods Receipt Note')
        DEBIT_NOTE = 'debit_note', _('Purchase Return / Debit Note')

    class GenerationMode(models.TextChoices):
        AUTOMATIC = 'automatic', _('Automatic')
        MANUAL_OVERRIDE = 'manual_override', _('Manual Override')
        MANUAL = 'manual', _('Manual')

    DEFAULT_PREFIXES = {
        'sales': 'INV-',
        'purchase': 'PO-',
        'receipt': 'REC-',
        'challan': 'DC-',
        'vat_bill': 'VAT-',
        'purchase_order': 'PORD-',
        'grn': 'GRN-',
        'debit_note': 'DN-',
    }

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='document_sequence_settings')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='document_sequence_settings')
    document_type = models.CharField(max_length=20, choices=DocumentType.choices)
    generation_mode = models.CharField(max_length=20, choices=GenerationMode.choices, default=GenerationMode.AUTOMATIC)
    prefix = models.CharField(max_length=50, blank=True, null=True, default='')
    suffix = models.CharField(max_length=50, blank=True, null=True, default='')
    padding_digits = models.PositiveIntegerField(default=3)
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)
    current_counter = models.PositiveIntegerField(default=1)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'document_sequence_settings'
        unique_together = ['restaurant', 'branch', 'document_type']
        verbose_name = 'Document Sequence Setting'
        verbose_name_plural = 'Document Sequence Settings'

    def __str__(self):
        branch_str = f" [{self.branch.name}]" if self.branch else ""
        return f"{self.restaurant.name}{branch_str} - {self.get_document_type_display()} ({self.generation_mode}): {self.format_number()}"

    def format_number(self, counter=None):
        if counter is None:
            counter = self.current_counter
        pref = self.prefix if self.prefix is not None else ""
        suff = self.suffix if self.suffix is not None else ""
        digits = self.padding_digits if self.padding_digits is not None else 3
        if digits > 0:
            num_str = str(counter).zfill(digits)
        else:
            num_str = str(counter)
        return f"{pref}{num_str}{suff}"

    @classmethod
    def get_sequence_setting(cls, restaurant, document_type, branch=None):
        default_pref = cls.DEFAULT_PREFIXES.get(document_type, '')
        if branch:
            setting, _ = cls.objects.get_or_create(
                restaurant=restaurant,
                branch=branch,
                document_type=document_type,
                defaults={
                    'generation_mode': cls.GenerationMode.AUTOMATIC,
                    'prefix': default_pref,
                    'suffix': '',
                    'padding_digits': 3,
                    'current_counter': 1,
                }
            )
            return setting

        setting, _ = cls.objects.get_or_create(
            restaurant=restaurant,
            branch=None,
            document_type=document_type,
            defaults={
                'generation_mode': cls.GenerationMode.AUTOMATIC,
                'prefix': default_pref,
                'suffix': '',
                'padding_digits': 3,
                'current_counter': 1,
            }
        )
        return setting


def default_applied_order_types():
    return ['dine_in', 'takeaway', 'delivery']


class CustomCharge(models.Model):
    class ChargeType(models.TextChoices):
        FLAT = 'flat', _('Flat Amount (₹)')
        PERCENTAGE = 'percentage', _('Percentage (%)')

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='custom_charges')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='custom_charges')
    charge_name = models.CharField(max_length=150, help_text="e.g. Packaging Charge, Private Dining Fee")
    charge_type = models.CharField(max_length=20, choices=ChargeType.choices, default=ChargeType.FLAT)
    value = models.DecimalField(max_digits=10, decimal_places=2, help_text="Flat currency amount or percentage value")
    is_taxable = models.BooleanField(default=False, help_text="If true, GST/VAT applies on top of this charge")
    applied_order_types = models.JSONField(default=default_applied_order_types, help_text="List of order types e.g. ['dine_in', 'takeaway', 'delivery']")
    is_auto_applied = models.BooleanField(default=True, help_text="Automatically add to cart when matching order type is selected")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['charge_name']
        verbose_name = 'Custom Charge'
        verbose_name_plural = 'Custom Charges'

    def __str__(self):
        val_str = f"₹{self.value}" if self.charge_type == CustomCharge.ChargeType.FLAT else f"{self.value}%"
        return f"{self.charge_name} ({val_str})"

    def calculate_amount(self, subtotal):
        from decimal import Decimal
        subtotal = Decimal(str(subtotal or 0))
        if self.charge_type == CustomCharge.ChargeType.FLAT:
            return Decimal(str(self.value or 0))
        return (subtotal * Decimal(str(self.value or 0)) / Decimal('100')).quantize(Decimal('0.01'))



class Coupon(models.Model):
    class DiscountType(models.TextChoices):
        PERCENT = 'P', _('Percent')
        FIXED = 'F', _('Fixed Amount')

    code = models.CharField(max_length=50, db_index=True)
    description = models.CharField(max_length=255, blank=True)
    discount_type = models.CharField(max_length=1, choices=DiscountType.choices, default=DiscountType.FIXED)
    value = models.DecimalField(max_digits=10, decimal_places=2, help_text="If percent, provide 0-100; if fixed, currency amount")
    min_spend = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    start_at = models.DateTimeField(null=True, blank=True)
    end_at = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)
    max_uses = models.PositiveIntegerField(null=True, blank=True, help_text="Total number of times this coupon can be used. Leave blank for unlimited.")
    used_count = models.PositiveIntegerField(default=0)
    # Scope to a restaurant / branch (optional).
    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, null=True, blank=True, related_name='coupons')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='coupons')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        unique_together = ('code', 'restaurant')

    def __str__(self):
        return self.code

    def is_active_now(self):
        from django.utils import timezone
        if not self.active:
            return False
        now = timezone.now()
        if self.start_at and now < self.start_at:
            return False
        if self.end_at and now > self.end_at:
            return False
        if self.max_uses is not None and self.used_count >= self.max_uses:
            return False
        return True

    def is_valid_for(self, subtotal, restaurant=None):
        from decimal import Decimal
        if not self.is_active_now():
            return False
        if restaurant and self.restaurant_id and self.restaurant_id != getattr(restaurant, 'id', None):
            return False
        if Decimal(subtotal) < (self.min_spend or Decimal('0')):
            return False
        return True

    def calculate_discount(self, subtotal):
        from decimal import Decimal, ROUND_HALF_UP
        subtotal = Decimal(subtotal or 0)
        if subtotal <= 0:
            return Decimal('0')
        if self.discount_type == Coupon.DiscountType.PERCENT:
            pct = max(Decimal('0'), min(Decimal('100'), Decimal(self.value or 0)))
            return (subtotal * (pct / Decimal('100'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        # fixed amount
        amount = Decimal(self.value or 0)
        return max(Decimal('0'), amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


class RestaurantLanding(models.Model):
    restaurant = models.OneToOneField('super_admin.Restaurant', on_delete=models.CASCADE, related_name='landing')
    LOGO_SHAPE_CHOICES = [
        ('round', 'Round / Circular'),
        ('square', 'Square'),
        ('rectangle', 'Rectangle / Wide'),
    ]
    BRAND_DISPLAY_CHOICES = [
        ('both', 'Both Logo & Name'),
        ('logo_only', 'Logo Only'),
        ('name_only', 'Name Only'),
    ]

    logo = models.ImageField(upload_to='landing/logo/', blank=True, null=True)
    logo_shape = models.CharField(
        max_length=20,
        choices=LOGO_SHAPE_CHOICES,
        default='round',
        help_text="Display shape/format of logo in navbar (round, square, rectangle)"
    )
    brand_display_type = models.CharField(
        max_length=20,
        choices=BRAND_DISPLAY_CHOICES,
        default='both',
        help_text="Header/navbar display mode: both (logo and name), logo_only, or name_only"
    )
    favicon = models.FileField(upload_to='landing/favicon/', blank=True, null=True, help_text="Upload .ico favicon file")
    hero_title = models.CharField(max_length=200, blank=True)
    hero_subtitle = models.CharField(max_length=300, blank=True)
    hero_image = models.ImageField(upload_to='landing/hero/', blank=True, null=True)
    hero_video = models.FileField(
        upload_to='landing/videos/',
        null=True,
        blank=True,
        help_text="Upload a video file (MP4 format recommended). This will be shown in the hero section."
    )
    cta_text = models.CharField(max_length=100, blank=True)
    cta_link = models.CharField(max_length=300, blank=True)
    about_heading = models.CharField(max_length=200, blank=True)
    about_italic = models.TextField(blank=True, default="")
    about_bullet_1 = models.CharField(max_length=255, blank=True, default="")
    about_bullet_2 = models.CharField(max_length=255, blank=True, default="")
    about_bullet_3 = models.CharField(max_length=255, blank=True, default="")
    about_body = models.TextField(blank=True)
    about_image = models.ImageField(upload_to='landing/about/', blank=True, null=True)
    about_video_url = models.CharField(max_length=300, blank=True, default="https://www.youtube.com/embed/SXJqEnauNaY")
    about_stat_num_1 = models.CharField(max_length=50, blank=True, default="600+")
    about_stat_label_1 = models.CharField(max_length=100, blank=True, default="Monthly Visitors")
    about_stat_desc_1 = models.TextField(blank=True, default="Welcoming thousands of gourmet lovers every month with unparalleled dining joy.")
    about_stat_num_2 = models.CharField(max_length=50, blank=True, default="450+")
    about_stat_label_2 = models.CharField(max_length=100, blank=True, default="Positive Reviews")
    about_stat_desc_2 = models.TextField(blank=True, default="High-end customer satisfaction rated outstandingly on popular platforms.")
    about_stat_num_3 = models.CharField(max_length=50, blank=True, default="135+")
    about_stat_label_3 = models.CharField(max_length=100, blank=True, default="Secret Recipes")
    about_stat_desc_3 = models.TextField(blank=True, default="Handcrafted dynamic configurations and ingredients passed through generations.")
    about_stat_num_4 = models.CharField(max_length=50, blank=True, default="25+")
    about_stat_label_4 = models.CharField(max_length=100, blank=True, default="Awards & Honours")
    about_stat_desc_4 = models.TextField(blank=True, default="Acclaimed culinary achievements recognized by major hospitality leagues.")
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True, default="")
    opening_hours = models.CharField(max_length=200, blank=True)
    
    # Social media channels
    facebook_url = models.URLField(blank=True, default="")
    instagram_url = models.URLField(blank=True, default="")
    twitter_url = models.URLField(blank=True, default="")
    youtube_url = models.URLField(blank=True, default="")
    
    # Dynamic SEO & Metadata
    seo_title = models.CharField(max_length=255, blank=True)
    seo_description = models.TextField(blank=True)
    seo_keywords = models.CharField(max_length=500, blank=True, help_text="Comma separated keywords")
    seo_canonical_url = models.CharField(max_length=500, blank=True, help_text="Canonical URL path or full link")
    
    # Open Graph (Facebook / LinkedIn)
    og_title = models.CharField(max_length=255, blank=True)
    og_description = models.TextField(blank=True)
    og_url = models.CharField(max_length=500, blank=True)
    og_site_name = models.CharField(max_length=255, blank=True)
    og_locale = models.CharField(max_length=50, blank=True, default="en_AE")
    og_type = models.CharField(max_length=50, blank=True, default="website")
    
    # Twitter Card
    twitter_card_type = models.CharField(max_length=50, blank=True, default="summary_large_image")
    twitter_title = models.CharField(max_length=255, blank=True)
    twitter_description = models.TextField(blank=True)
    
    # Sub-pages SEO config JSON
    # e.g. {"about": {"title": "...", "description": "..."}, "menu": {...}}
    pages_seo = models.JSONField(default=dict, blank=True)

    # Page-wise FAQ config JSON
    # e.g. {"home": [{"id": "...", "question": "...", "answer": "...", "order": 1, "active": true}], ...}
    pages_faq = models.JSONField(default=dict, blank=True)

    # Dynamic Website Theme & Colors config JSON
    # e.g. {"preset": "emerald_gold", "primary": "#e6b15f", "background": "#18312e", ...}
    theme_config = models.JSONField(default=dict, blank=True, help_text="Custom color scheme and theme settings")
    
    show_hero = models.BooleanField(default=True, verbose_name="Show Hero Section")
    has_hotel_website = models.BooleanField(
        default=False,
        verbose_name="Dedicated Hotel Website Mode",
        help_text="Operates in Hotel Website mode on the public frontend (suppresses restaurant menus and chefs)"
    )
    show_about = models.BooleanField(default=True, verbose_name="Show About Us Section")
    show_why_us = models.BooleanField(default=True, verbose_name="Show Why Choose Us Section")
    show_specials = models.BooleanField(default=True, verbose_name="Show Specials Section")
    show_events = models.BooleanField(default=True, verbose_name="Show Events Section")
    show_gallery = models.BooleanField(default=True, verbose_name="Show Gallery Section")
    show_team = models.BooleanField(default=True, verbose_name="Show Team Section")
    show_reviews = models.BooleanField(default=True, verbose_name="Show Reviews Section")
    show_careers = models.BooleanField(default=True, verbose_name="Show Careers Page / Section")
    careers_title = models.CharField(max_length=200, blank=True, default="Open Positions")
    careers_subtitle = models.TextField(blank=True, default="Join our growing team and create memorable dining experiences with us.")
    why_us_title_1 = models.CharField(max_length=100, blank=True, default="")
    why_us_description_1 = models.TextField(blank=True, default="")
    why_us_title_2 = models.CharField(max_length=100, blank=True, default="")
    why_us_description_2 = models.TextField(blank=True, default="")
    why_us_title_3 = models.CharField(max_length=100, blank=True, default="")
    why_us_description_3 = models.TextField(blank=True, default="")
    
    # Club & Events Page Customization
    events_hero_suptitle = models.CharField(max_length=200, blank=True, default="EXCLUSIVE GATHERINGS & NIGHTLIFE")
    events_hero_title = models.CharField(max_length=200, blank=True, default="Events & Club Experiences")
    events_hero_subtitle = models.TextField(blank=True, default="From candlelit jazz nights and electrifying resident DJ lounges to bespoke sommelier galas and live culinary celebrations.")
    events_hero_image = models.ImageField(upload_to='landing/events_hero/', blank=True, null=True)
    events_hero_video = models.FileField(
        upload_to='landing/events_videos/',
        null=True,
        blank=True,
        help_text="Upload MP4/WebM hero background video for the events page"
    )
    events_hero_video_url = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="External video URL (MP4/WebM, YouTube, Vimeo, or CDN URL)"
    )
    events_hero_bg_type = models.CharField(
        max_length=20,
        default="auto",
        choices=[
            ('auto', 'Auto (Video if available, else Slider)'),
            ('video', 'Background Video'),
            ('slider', 'Image Slider'),
            ('image', 'Single Image'),
        ],
        help_text="Background display mode for Events page hero"
    )
    events_about_suptitle = models.CharField(max_length=200, blank=True, default="ATMOSPHERE & HOSPITALITY")
    events_about_title = models.CharField(max_length=200, blank=True, default="Where Haute Cuisine Meets Electrifying Nights")
    events_about_subtitle = models.TextField(
        blank=True,
        default="We transcend conventional dining. By evening, our curated spaces transform into intimate jazz sanctuaries, energetic DJ lounges, and grand banquet venues designed to create enduring memories."
    )
    events_page_config = models.JSONField(
        default=dict,
        blank=True,
        help_text="Dynamic configuration for events page feature cards and venue stats"
    )
    map_iframe_url = models.TextField(blank=True, default="https://www.google.com/maps/embed?pb=!1m18!1m12!1m3!1d3022.617540232549!2d-73.98785312347953!3d40.74844047138902!2m3!1f0!2f0!3f0!3m2!1i1024!2i768!4f13.1!3m3!1m2!1s0x89c259a9b3117469%3A0xd134e199a405a163!2sEmpire%20State%20Building!5e0!3m2!1sen!2sus!4v1700000000000!5m2!1sen!2sus")
    
    # Legal & Policies
    privacy_policy_title = models.CharField(max_length=200, blank=True, default="Privacy Policy")
    privacy_policy_updated_at = models.CharField(max_length=100, blank=True, default="")
    privacy_policy_content = models.TextField(blank=True, default="")

    terms_of_service_title = models.CharField(max_length=200, blank=True, default="Terms of Service")
    terms_of_service_updated_at = models.CharField(max_length=100, blank=True, default="")
    terms_of_service_content = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Landing Page'
        verbose_name_plural = 'Landing Pages'

    def __str__(self):
        return f"Landing for {self.restaurant.name}"


class Event(models.Model):
    landing = models.ForeignKey(RestaurantLanding, on_delete=models.CASCADE, related_name='events')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='events')
    title = models.CharField(max_length=150)
    price = models.CharField(max_length=50)  # e.g., "$189"
    image = models.ImageField(upload_to='landing/events/', blank=True, null=True)
    description = models.TextField()
    bullet_1 = models.CharField(max_length=200, blank=True)
    bullet_2 = models.CharField(max_length=200, blank=True)
    bullet_3 = models.CharField(max_length=200, blank=True)
    display_order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['display_order', 'id']
        verbose_name = 'Event'
        verbose_name_plural = 'Events'

    def __str__(self):
        branch_str = f" ({self.branch.name})" if self.branch else " (All Outlets)"
        return f"{self.title}{branch_str}"


class GalleryImage(models.Model):
    landing = models.ForeignKey(RestaurantLanding, on_delete=models.CASCADE, related_name='gallery_images')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='gallery_images')
    image = models.ImageField(upload_to='landing/gallery/')
    caption = models.CharField(max_length=200, blank=True)
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['display_order', 'id']

    def __str__(self):
        branch_str = f" ({self.branch.name})" if self.branch else " (All Outlets)"
        return f"{self.caption or f'Image {self.id}'}{branch_str}"


class TeamMember(models.Model):
    landing = models.ForeignKey(RestaurantLanding, on_delete=models.CASCADE, related_name='team_members')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='team_members')
    name = models.CharField(max_length=150)
    position = models.CharField(max_length=150, blank=True)
    photo = models.ImageField(upload_to='landing/team/', blank=True, null=True)
    display_order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['display_order', 'id']
        verbose_name = 'Team Member'
        verbose_name_plural = 'Team Members'

    def __str__(self):
        branch_str = f" ({self.branch.name})" if self.branch else " (All Outlets)"
        return f"{self.name}{branch_str}"


class Review(models.Model):
    landing = models.ForeignKey(RestaurantLanding, on_delete=models.CASCADE, related_name='reviews')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='reviews')
    name = models.CharField(max_length=150)
    stars = models.PositiveSmallIntegerField(default=5, validators=[MinValueValidator(1), MaxValueValidator(5)])
    description = models.TextField()
    display_order = models.PositiveIntegerField(default=0)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    table_no = models.CharField(max_length=50, blank=True, null=True)
    is_customer_submitted = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['display_order', '-created_at', 'id']
        verbose_name = 'Review'
        verbose_name_plural = 'Reviews'

    def __str__(self):
        branch_str = f" ({self.branch.name})" if self.branch else " (All Outlets)"
        return f"{self.name} ({self.stars}★){branch_str}"


class WhyChooseUs(models.Model):
    landing = models.ForeignKey(RestaurantLanding, on_delete=models.CASCADE, related_name='why_choose_us_items')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='why_choose_us_items')
    title = models.CharField(max_length=150)
    description = models.TextField()
    icon = models.CharField(max_length=50, blank=True, default="utensils", help_text="Icon identifier or card style")
    display_order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['display_order', 'id']
        verbose_name = 'Why Choose Us Item'
        verbose_name_plural = 'Why Choose Us Items'

    def __str__(self):
        branch_str = f" ({self.branch.name})" if self.branch else " (All Outlets)"
        return f"{self.title}{branch_str}"


CONFIG_CHOICES = [('inherit', 'Inherit from Parent'), ('override', 'Override Parent')]


class InventoryCategory(models.Model):
    class TaxabilityType(models.TextChoices):
        TAXABLE = 'taxable', _('Taxable')
        EXEMPTED = 'exempted', _('Exempted')
        NIL_RATED = 'nil_rated', _('Nil Rated')
        NON_GST_EXCISE = 'non_gst_excise', _('Non-GST / Excise')

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='inventory_categories', null=True, blank=True)
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='inventory_categories')
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='subcategories')
    name = models.CharField(max_length=150)
    tax_configuration = models.CharField(max_length=20, choices=CONFIG_CHOICES, default='inherit')
    default_gst_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    default_hsn_code = models.CharField(max_length=50, null=True, blank=True)
    taxability = models.CharField(max_length=50, null=True, blank=True)

    # Additional default fields (nullable)
    default_unit = models.CharField(max_length=50, null=True, blank=True)
    default_warehouse = models.CharField(max_length=100, null=True, blank=True)
    valuation_method = models.CharField(max_length=50, null=True, blank=True)
    expense_account = models.CharField(max_length=100, null=True, blank=True)
    permission_group = models.CharField(max_length=100, null=True, blank=True)

    taxability_type = models.CharField(
        max_length=20,
        choices=TaxabilityType.choices,
        default=TaxabilityType.TAXABLE,
        null=True, blank=True
    )
    hsn_sac_code = models.CharField(max_length=50, blank=True, null=True)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'Inventory Category'
        verbose_name_plural = 'Inventory Categories'
        unique_together = ['restaurant', 'branch', 'name', 'parent']

    def __str__(self):
        if self.parent:
            return f"{self.parent.name} > {self.name}"
        return self.name

    def resolve_category_value(self, field_name):
        target_attrs = [field_name]
        if field_name == 'default_gst_rate':
            target_attrs.append('gst_rate')
        elif field_name == 'default_hsn_code':
            target_attrs.append('hsn_sac_code')
        elif field_name == 'taxability':
            target_attrs.append('taxability_type')

        val = None
        for attr in target_attrs:
            curr_val = getattr(self, attr, None)
            if curr_val is not None and curr_val != '':
                val = curr_val
                break

        if self.tax_configuration == 'override' and val is not None:
            return val

        if self.parent:
            parent_val = self.parent.resolve_category_value(field_name)
            if parent_val is not None and parent_val != '':
                return parent_val

        if val is not None:
            return val

        if self.restaurant and hasattr(self.restaurant, 'get_company_default'):
            comp_val = self.restaurant.get_company_default(field_name)
            if comp_val is not None and comp_val != '':
                return comp_val

        return None

    @property
    def effective_hsn_code(self):
        return self.resolve_category_value('default_hsn_code')

    @property
    def effective_gst_rate(self):
        return self.resolve_category_value('default_gst_rate')

    @property
    def effective_taxability(self):
        return self.resolve_category_value('taxability')


class InventoryItem(models.Model):
    class CategoryChoices(models.TextChoices):
        RAW_INGREDIENTS = 'raw_ingredients', _('Raw Ingredients')
        BEVERAGES = 'beverages', _('Beverages')
        PACKAGING = 'packaging', _('Packaging & Tissue')
        BAR_STOCK = 'bar_stock', _('Bar Stock')

    class UnitChoices(models.TextChoices):
        KG = 'kg', _('Kilogram (kg)')
        GM = 'gm', _('Gram (gm)')
        LITRE = 'litre', _('Litre (L)')
        ML = 'ml', _('Millilitre (ml)')
        PIECE = 'piece', _('Piece')
        PACK = 'pack', _('Pack')
        BOX = 'box', _('Box')
        CRATE = 'crate', _('Crate')
        BOTTLE = 'bottle', _('Bottle')

    class TaxabilityType(models.TextChoices):
        TAXABLE = 'taxable', _('Taxable')
        EXEMPTED = 'exempted', _('Exempted')
        NIL_RATED = 'nil_rated', _('Nil Rated')
        NON_GST_EXCISE = 'non_gst_excise', _('Non-GST / Excise')

    name = models.CharField(max_length=150)
    sku = models.CharField(max_length=100, blank=True)
    category = models.ForeignKey('InventoryCategory', on_delete=models.SET_NULL, null=True, blank=True, related_name='items')
    unit = models.CharField(max_length=50, blank=True)
    quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    reorder_level = models.IntegerField(default=5)
    cost_price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, null=True, blank=True)
    price_per_unit = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    selling_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    tax_configuration = models.CharField(max_length=20, choices=CONFIG_CHOICES, default='inherit')
    taxability_type = models.CharField(
        max_length=20,
        choices=TaxabilityType.choices,
        default=TaxabilityType.TAXABLE,
        null=True, blank=True
    )
    hsn_sac_code = models.CharField(max_length=20, blank=True, null=True)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, null=True, blank=True)
    hsn_description = models.TextField(blank=True, null=True)
    # --- Purchase voucher (Elite Zen) masters ---
    class PurchaseType(models.TextChoices):
        STOCK = 'stock', _('Kitchen / bar stock')
        CONSUMABLE = 'consumable', _('Consumable')
        ASSET = 'asset', _('Asset')
        SERVICE = 'service', _('Expense / service')

    purchase_type = models.CharField(max_length=20, choices=PurchaseType.choices, default=PurchaseType.STOCK)
    alias = models.CharField(max_length=255, blank=True, default='', help_text="Other names typed at entry, space separated (e.g. 'aloo alu')")
    purchase_units = models.JSONField(default=list, blank=True, help_text="Alternate units: [{'u': 'katta', 'f': 50}] = 1 katta is 50 of the stock unit")
    last_purchase_rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text="Rate on the last purchase bill, per stock unit")
    track_expiry = models.BooleanField(default=False)
    shelf_life_days = models.PositiveIntegerField(null=True, blank=True)
    track_batch = models.BooleanField(default=False)
    mrp = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="MRP per piece (items sold as bought)")
    pieces_per_unit = models.PositiveIntegerField(null=True, blank=True, help_text="Pieces in one stock unit (a crate of 24)")
    is_active = models.BooleanField(default=True)
    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='inventory_items', null=True, blank=True)
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='inventory_items')
    internal_branch = models.ForeignKey('Branch', on_delete=models.SET_NULL, null=True, blank=True, related_name='inventory_items')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        unique_together = ['restaurant', 'branch', 'name']

    def __str__(self):
        return self.name

    def adjust(self, delta):
        # Stock is kept to 3 decimals (2.5 kg, 0.25 ltr); whole-number callers work unchanged
        self.quantity = (Decimal(str(self.quantity or 0)) + Decimal(str(delta or 0))).quantize(Decimal('0.001'))
        self.save(update_fields=['quantity'])

    @property
    def unit_cost(self):
        from decimal import Decimal
        return self.cost_price or self.price_per_unit or Decimal('0.00')

    @property
    def is_low_stock(self):
        return (self.quantity or 0) <= (self.reorder_level or 0)

    @property
    def effective_hsn_code(self):
        return self.get_effective_value('default_hsn_code')

    @property
    def effective_gst_rate(self):
        return self.get_effective_value('default_gst_rate')

    @property
    def effective_taxability(self):
        return self.get_effective_value('taxability')

    def get_effective_value(self, field_name):
        target_attrs = [field_name]
        if field_name == 'default_gst_rate':
            target_attrs.append('gst_rate')
        elif field_name == 'default_hsn_code':
            target_attrs.append('hsn_sac_code')
        elif field_name == 'taxability':
            target_attrs.append('taxability_type')

        # 1. Check Item Override
        if self.tax_configuration == 'override':
            for attr in target_attrs:
                val = getattr(self, attr, None)
                if val is not None and val != '':
                    return val

        # 2. Traverse Category Tree
        if self.category:
            cat_val = self.category.resolve_category_value(field_name)
            if cat_val is not None and cat_val != '':
                return cat_val

        # 3. Check Company Default
        if self.restaurant and hasattr(self.restaurant, 'get_company_default'):
            comp_val = self.restaurant.get_company_default(field_name)
            if comp_val is not None:
                return comp_val

        # 4. Fallback to direct item value
        for attr in target_attrs:
            val = getattr(self, attr, None)
            if val is not None and val != '':
                return val

        return None


class MenuItemIngredient(models.Model):
    menu_item = models.ForeignKey(MenuItem, on_delete=models.CASCADE, related_name='ingredients')
    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name='used_in_menu_items')
    quantity_needed = models.DecimalField(max_digits=10, decimal_places=3, help_text="Amount of ingredient required for 1 unit of this menu item")

    class Meta:
        unique_together = ['menu_item', 'inventory_item']

    def __str__(self):
        return f"{self.quantity_needed} {self.inventory_item.unit or ''} of {self.inventory_item.name} for {self.menu_item.name}"


class Party(models.Model):
    class PartyType(models.TextChoices):
        VENDOR = 'vendor', _('Vendor (Seller)')
        CUSTOMER = 'customer', _('Customer (Buyer)')
        # BOTH = 'both', _('Both (Vendor & Customer)')

    class BalanceType(models.TextChoices):
        PAYABLE = 'payable', _('Payable (To Pay)')
        RECEIVABLE = 'receivable', _('Receivable (To Receive)')

    class GstRegistrationType(models.TextChoices):
        UNREGISTERED = 'unregistered', _('Unregistered / Consumer')
        REGULAR = 'regular', _('GST Registered - Regular')
        COMPOSITION = 'composition', _('GST Registered - Composition')
        OVERSEAS = 'overseas', _('Overseas / Foreign Supplier')

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='parties', null=True, blank=True)
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='parties')
    name = models.CharField(max_length=200)
    party_type = models.CharField(max_length=20, choices=PartyType.choices, default=PartyType.VENDOR)
    phone = models.CharField(max_length=50, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    gstin = models.CharField(max_length=50, blank=True, null=True)
    gst_registration_type = models.CharField(max_length=50, choices=GstRegistrationType.choices, default=GstRegistrationType.UNREGISTERED, blank=True, null=True)
    place_of_supply = models.CharField(max_length=100, blank=True, null=True)
    address_line1 = models.CharField(max_length=255, blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    state = models.CharField(max_length=100, blank=True, null=True)
    pincode = models.CharField(max_length=20, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    balance_type = models.CharField(max_length=20, choices=BalanceType.choices, default=BalanceType.PAYABLE)
    # --- Purchase voucher (Elite Zen) terms ---
    alias = models.CharField(max_length=255, blank=True, default='', help_text="Other names typed at entry (Hindi / short names)")
    credit_days = models.PositiveIntegerField(default=0, help_text="Usual udhaar period in days")
    interest_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text="% p.a. after the due date")
    is_msme = models.BooleanField(default=False, help_text="MSME supplier: to be paid within 45 days")
    default_payment = models.CharField(max_length=10, choices=[('credit', 'Credit / udhaar'), ('cash', 'Pays on the spot')], default='credit')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'Parties'

    @property
    def full_address(self):
        parts = [self.address_line1, self.city, self.state, self.pincode]
        non_empty = [p.strip() for p in parts if p and p.strip()]
        if non_empty:
            return ", ".join(non_empty)
        return self.address or ""

    def __str__(self):
        return f"{self.name} ({self.get_party_type_display()})"

    def get_sales_queryset(self):
        import re
        from django.db.models import Q
        qs = Order.objects.filter(restaurant=self.restaurant) if self.restaurant else Order.objects.all()
        if self.phone:
            clean_p = re.sub(r'\D', '', str(self.phone))
            if len(clean_p) >= 10:
                clean_p = clean_p[-10:]
            cond = Q(party=self) | (Q(phone__icontains=clean_p) if clean_p else Q(pk=None))
        else:
            cond = Q(party=self) | Q(customer_name__iexact=self.name)
        return qs.filter(cond)

    @property
    def total_invoiced(self):
        from decimal import Decimal
        from django.db.models import Sum
        po_sum = Decimal(str(self.purchase_orders.aggregate(s=Sum('grand_total'))['s'] or 0))
        sales_sum = Decimal(str(self.get_sales_queryset().exclude(status='C').aggregate(s=Sum('total_amount'))['s'] or 0))
        return po_sum + sales_sum

    @property
    def total_paid(self):
        import re
        from decimal import Decimal
        from django.db.models import Sum, Q
        from core.models import AdvanceReceipt

        po_paid = Decimal(str(self.purchase_orders.filter(payment_status__in=['paid', 'P']).aggregate(s=Sum('grand_total'))['s'] or 0))
        po_paid += Decimal(str(self.purchase_orders.exclude(payment_status__in=['paid', 'P']).aggregate(s=Sum('paid_amount'))['s'] or 0))
        sales_qs = self.get_sales_queryset().exclude(status='C')

        sales_paid_direct = Decimal(str(sales_qs.aggregate(s=Sum('paid_amount'))['s'] or 0))
        sales_paid_advance = Decimal(str(sales_qs.aggregate(s=Sum('advance_amount_paid'))['s'] or 0))
        sales_paid_legacy = Decimal(str(sales_qs.filter(paid_amount=0, advance_amount_paid=0, payment_status__in=['paid', 'P']).aggregate(s=Sum('total_amount'))['s'] or 0))

        arc_qs = AdvanceReceipt.objects.filter(restaurant=self.restaurant) if self.restaurant else AdvanceReceipt.objects.all()
        if self.phone:
            clean_p = re.sub(r'\D', '', str(self.phone))
            if len(clean_p) >= 10:
                clean_p = clean_p[-10:]
            cond = Q(party=self) | (Q(phone__icontains=clean_p) if clean_p else Q(pk=None))
        else:
            cond = Q(party=self) | Q(customer_name__iexact=self.name)
        unlinked_advances = Decimal(str(arc_qs.filter(cond, status=AdvanceReceipt.ReceiptStatus.ACTIVE).aggregate(s=Sum('advance_amount'))['s'] or 0))

        sales_paid = sales_paid_direct + sales_paid_advance + sales_paid_legacy + unlinked_advances
        return po_paid + sales_paid

    @property
    def current_balance(self):
        import re
        from decimal import Decimal
        from django.db.models import Sum, Q
        from core.models import AdvanceReceipt

        base_bal = Decimal(str(self.opening_balance or 0))
        
        # Bills marked paid without bill-wise payments (older entries) are fully settled; the rest
        # owe their total less payments and debit notes against them.
        open_bills = self.purchase_orders.exclude(Q(payment_status__in=['paid', 'P']) & Q(paid_amount=0))
        open_sums = open_bills.aggregate(t=Sum('grand_total'), p=Sum('paid_amount'))
        debit_notes = Decimal(str(self.purchase_returns.filter(bill__in=open_bills).aggregate(s=Sum('grand_total'))['s'] or 0))
        unpaid_po = Decimal(str(open_sums['t'] or 0)) - Decimal(str(open_sums['p'] or 0)) - debit_notes
        sales_due_explicit = Decimal(str(self.get_sales_queryset().exclude(status='C').aggregate(s=Sum('due_amount'))['s'] or 0))
        sales_due_legacy = Decimal(str(self.get_sales_queryset().filter(due_amount=0).exclude(payment_status__in=['paid', 'P']).exclude(status='C').aggregate(s=Sum('total_amount'))['s'] or 0))
        unpaid_sales = sales_due_explicit + sales_due_legacy

        arc_qs = AdvanceReceipt.objects.filter(restaurant=self.restaurant) if self.restaurant else AdvanceReceipt.objects.all()
        if self.phone:
            clean_p = re.sub(r'\D', '', str(self.phone))
            if len(clean_p) >= 10:
                clean_p = clean_p[-10:]
            cond = Q(party=self) | (Q(phone__icontains=clean_p) if clean_p else Q(pk=None))
        else:
            cond = Q(party=self) | Q(customer_name__iexact=self.name)
        unlinked_advances = Decimal(str(arc_qs.filter(cond, status=AdvanceReceipt.ReceiptStatus.ACTIVE).aggregate(s=Sum('advance_amount'))['s'] or 0))

        if self.party_type == Party.PartyType.CUSTOMER:
            net = (base_bal if self.balance_type == Party.BalanceType.RECEIVABLE else -base_bal) + unpaid_sales - unlinked_advances
        elif self.party_type == Party.PartyType.VENDOR:
            net = (base_bal if self.balance_type == Party.BalanceType.PAYABLE else -base_bal) + unpaid_po
        else: # both
            net_receivable = (base_bal if self.balance_type == Party.BalanceType.RECEIVABLE else Decimal('0')) + unpaid_sales - unlinked_advances
            net_payable = (base_bal if self.balance_type == Party.BalanceType.PAYABLE else Decimal('0')) + unpaid_po
            net = net_receivable - net_payable

        return net


class Supplier(models.Model):
    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='suppliers', null=True, blank=True)
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='suppliers')
    name = models.CharField(max_length=150)
    gstin = models.CharField(max_length=50, blank=True, null=True)
    phone = models.CharField(max_length=50, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class PurchaseOrder(models.Model):
    class PaymentMethod(models.TextChoices):
        BANK_TRANSFER = 'bank_transfer', _('Bank Transfer')
        UPI = 'upi', _('UPI')
        CASH = 'cash', _('Cash')
        CREDIT = 'credit', _('Credit / On Account')

    class PaymentStatus(models.TextChoices):
        PAID = 'paid', _('Paid')
        UNPAID = 'unpaid', _('Unpaid')
        PARTIAL = 'partial', _('Partial')

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='purchase_orders')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='purchase_orders')
    party = models.ForeignKey(Party, on_delete=models.SET_NULL, null=True, blank=True, related_name='purchase_orders')
    supplier = models.ForeignKey(Supplier, on_delete=models.SET_NULL, null=True, blank=True, related_name='purchase_orders')
    supplier_name = models.CharField(max_length=150)
    supplier_gstin = models.CharField(max_length=50, blank=True, null=True)
    po_number = models.CharField(max_length=100, blank=True, null=True, help_text="Internal Goods Inward Entry / PO Number")
    invoice_number = models.CharField(max_length=100, help_text="Supplier Bill/Invoice Ref No.")
    purchase_date = models.DateField()
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="Input GST / Tax Amount")
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_method = models.CharField(max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.BANK_TRANSFER)
    payment_status = models.CharField(max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.PAID)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="Settled through bill-wise payments")
    freight_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="Additional cost: freight, loading, packing")
    is_interstate = models.BooleanField(default=False)
    reverse_charge = models.BooleanField(default=False, help_text="Tax payable by us under RCM")
    itc_eligible = models.BooleanField(default=True, help_text="Input tax credit can be claimed")
    narration = models.TextField(blank=True, default='')
    source_order = models.ForeignKey('ProcurementOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='bills')
    stock_via_grn = models.BooleanField(default=False, help_text="Stock was already taken in by a Goods Receipt Note")
    # --- Purchase voucher (Elite Zen) ---
    bill_date = models.DateField(null=True, blank=True, help_text="Date printed on the supplier's bill")
    tax_regime = models.CharField(max_length=10, default='intra', help_text="intra (CGST+SGST) / inter (IGST) / none")
    is_tax_inclusive = models.BooleanField(default=False)
    items_gross = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_before_tax = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_charges = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    sgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    igst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cess_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_after_tax = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    refundable_deposits = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    round_off = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    itc_claimed = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    credit_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    credit_days = models.PositiveIntegerField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    interest_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    late_fee = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    notes = models.TextField(blank=True, default='')
    einvoice_data = models.JSONField(null=True, blank=True)
    voucher_data = models.JSONField(null=True, blank=True, help_text="The full voucher as entered (Elite Zen screen)")
    voucher_snapshot = models.JSONField(null=True, blank=True, help_text="Screen state, to reopen the voucher for editing")
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-purchase_date', '-created_at']

    def __str__(self):
        return f"PO #{self.id} - {self.supplier_name} ({self.invoice_number})"

    @property
    def returned_amount(self):
        from django.db.models import Sum
        return self.returns.aggregate(s=Sum('grand_total'))['s'] or Decimal('0.00')

    @property
    def balance_due(self):
        """What we still owe on this bill after payments and debit notes."""
        if self.payment_status == self.PaymentStatus.PAID:
            return Decimal('0.00')
        return max((self.grand_total or 0) - (self.paid_amount or 0) - self.returned_amount, Decimal('0.00'))


class PurchaseOrderItem(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='items')
    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name='purchase_items')
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, null=True, blank=True)
    hsn_code = models.CharField(max_length=50, null=True, blank=True)
    line_total = models.DecimalField(max_digits=12, decimal_places=2)
    grn_line = models.ForeignKey('GoodsReceiptLine', on_delete=models.SET_NULL, null=True, blank=True, related_name='bill_lines')
    # --- Purchase voucher (Elite Zen) line detail ---
    unit = models.CharField(max_length=30, blank=True, default='', help_text="Unit the quantity was typed in")
    rate_unit = models.CharField(max_length=30, blank=True, default='', help_text="Unit the rate is per")
    rate = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    quantity_in_stock_unit = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    free_quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    free_unit = models.CharField(max_length=30, blank=True, default='')
    free_in_stock_unit = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    stock_quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0, help_text="Billed + free, in the stock unit")
    discount_percent = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    taxable_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cess_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    charges_share = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    charges_tax_share = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    landed_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="What the line really cost")
    landed_rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text="Landed cost per stock unit")
    batch_number = models.CharField(max_length=50, blank=True, default='')
    expiry_date = models.DateField(null=True, blank=True)
    expiry_estimated = models.BooleanField(default=False)
    mrp = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    godown = models.CharField(max_length=100, blank=True, default='')
    line_remarks = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"{self.quantity} x {self.inventory_item.name} for PO #{self.purchase_order_id}"


class PurchaseOrderCharge(models.Model):
    """A charge / discount / deposit line of a purchase voucher (freight, labour, trade discount, crate deposit)."""
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='charges')
    ledger = models.ForeignKey('PurchaseLedgerAccount', on_delete=models.SET_NULL, null=True, blank=True, related_name='charge_lines')
    name = models.CharField(max_length=100)
    charge_type = models.CharField(max_length=20, default='charge', help_text="charge / discount / deposit")
    stage = models.CharField(max_length=10, blank=True, default='', help_text="discount: before / after tax")
    method = models.CharField(max_length=10, default='fixed', help_text="fixed / percent")
    rate = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    base = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    gst_treatment = models.CharField(max_length=20, default='none', help_text="items / none / a rate")
    gst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    applies_to = models.JSONField(null=True, blank=True, help_text="'all' or the lines it is for")
    tax_split = models.JSONField(default=list, blank=True)
    in_cost = models.BooleanField(default=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"{self.name} {self.amount} (PO #{self.purchase_order_id})"


class PurchaseOrderPayment(models.Model):
    """How a purchase voucher was settled: cash, a bank transfer / cheque, or a payment made before the bill."""
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='payments')
    mode = models.CharField(max_length=20, help_text="cash / upi / neft / rtgs / imps / cheque / linked")
    ledger = models.ForeignKey('PurchaseLedgerAccount', on_delete=models.SET_NULL, null=True, blank=True, related_name='payment_lines')
    ledger_name = models.CharField(max_length=150, blank=True, default='')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reference_number = models.CharField(max_length=100, blank=True, default='')
    payment_date = models.DateField(null=True, blank=True)
    vendor_advance = models.ForeignKey('VendorAdvance', on_delete=models.SET_NULL, null=True, blank=True, related_name='links')

    class Meta:
        ordering = ['id']


class PurchaseOrderAttachment(models.Model):
    """A copy of the supplier's bill (photo / PDF), kept with the voucher."""
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField(upload_to='purchases/bills/%Y/%m/')
    file_name = models.CharField(max_length=255)
    file_type = models.CharField(max_length=100, blank=True, default='')
    file_size = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']


class PurchaseLedgerAccount(models.Model):
    """The small masters the purchase voucher picks from: cash counters, bank accounts, charges & discounts, godowns."""
    class Kind(models.TextChoices):
        CASH = 'cash', _('Cash in hand')
        BANK = 'bank', _('Bank account')
        CHARGE = 'charge', _('Charge / discount')
        GODOWN = 'godown', _('Godown / store')

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='purchase_ledgers')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='purchase_ledgers')
    kind = models.CharField(max_length=10, choices=Kind.choices)
    name = models.CharField(max_length=150)
    alias = models.CharField(max_length=255, blank=True, default='')
    # Charges only
    charge_type = models.CharField(max_length=10, blank=True, default='', choices=[('charge', 'Charge (adds)'), ('discount', 'Discount (reduces)'), ('deposit', 'Refundable deposit')])
    stage = models.CharField(max_length=10, blank=True, default='', choices=[('before', 'Before tax (on the invoice)'), ('after', 'After tax (payable only)')])
    gst = models.CharField(max_length=10, blank=True, default='items', help_text="items = taxed with the goods (s.15(2)(c)); none; or its own rate e.g. 18")
    default_rate = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True, help_text="Usual % (e.g. mandi commission 6)")
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['kind', 'sort_order', 'id']

    def __str__(self):
        return f"{self.get_kind_display()}: {self.name}"


class VendorAdvance(models.Model):
    """Money given to a supplier before their bill was entered ('paid, awaiting bill'). A purchase links it (Alt+L)."""
    class Mode(models.TextChoices):
        CASH = 'cash', _('Cash')
        UPI = 'upi', _('UPI')
        BANK = 'bank', _('Bank transfer')
        CHEQUE = 'cheque', _('Cheque')

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='vendor_advances')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='vendor_advances')
    party = models.ForeignKey(Party, on_delete=models.CASCADE, related_name='vendor_advances')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.CASH)
    paid_at = models.DateTimeField()
    note = models.CharField(max_length=255, blank=True, default='')
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['paid_at']

    def __str__(self):
        return f"{self.party} {self.amount} ({self.paid_at:%d %b})"

    def amount_left(self, exclude_po=None):
        from django.db.models import Sum
        qs = self.links.all()
        if exclude_po is not None:
            qs = qs.exclude(purchase_order=exclude_po)
        used = qs.aggregate(s=Sum('amount'))['s'] or Decimal('0')
        return (self.amount - used).quantize(Decimal('0.01'))


class PurchaseVoucherSetting(models.Model):
    """Per restaurant: does input GST come back (ITC)? 18% restaurants claim it; 5% restaurants cannot, so GST is cost."""
    restaurant = models.OneToOneField('super_admin.Restaurant', on_delete=models.CASCADE, related_name='purchase_voucher_setting')
    gst_credit = models.BooleanField(default=False, help_text="On: input GST is claimed (not part of item cost)")

    def __str__(self):
        return f"Purchase voucher settings — {self.restaurant}"


class InventoryTransaction(models.Model):
    class TransactionType(models.TextChoices):
        ORDER = 'O', _('Order Deduction')
        CANCEL = 'C', _('Order Cancellation Return')
        MANUAL = 'M', _('Manual Adjustment')
        RESTOCK = 'R', _('Restock')
        INWARD_PURCHASE = 'P', _('Inward Purchase')
        WASTAGE = 'W', _('Wastage')
        STOCK_COUNT = 'S', _('Stock count correction')
        RETURN_OUT = 'V', _('Return to supplier')

    class WastageReason(models.TextChoices):
        SPOILED = 'SPOILED', _('Spoiled / expired')
        PREP_WASTE = 'PREP_WASTE', _('Prep / trim waste')
        OVERPRODUCTION = 'OVERPRODUCTION', _('Over-production')
        RETURNED = 'RETURNED', _('Guest returned dish')
        DAMAGED = 'DAMAGED', _('Spilled / damaged')
        STAFF_MEAL = 'STAFF_MEAL', _('Staff meal')
        COMPLIMENTARY = 'COMPLIMENTARY', _('Complimentary')
        OTHER = 'OTHER', _('Other')

    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name='transactions')
    transaction_type = models.CharField(max_length=1, choices=TransactionType.choices)
    delta = models.DecimalField(max_digits=14, decimal_places=3, help_text="Positive for restock/returns/purchase, negative for consumption")
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, related_name='inventory_transactions')
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name='inventory_transactions')
    goods_receipt = models.ForeignKey('GoodsReceiptNote', on_delete=models.SET_NULL, null=True, blank=True, related_name='inventory_transactions')
    purchase_return = models.ForeignKey('PurchaseReturn', on_delete=models.SET_NULL, null=True, blank=True, related_name='inventory_transactions')
    reason = models.CharField(max_length=20, choices=WastageReason.choices, blank=True)
    note = models.CharField(max_length=255, blank=True)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="Cost per unit when this happened")
    stock_count = models.ForeignKey('StockCount', on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions')
    performed_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.get_transaction_type_display()} - {self.inventory_item.name} ({self.delta})"


class AdvanceReceipt(models.Model):
    class ReceiptStatus(models.TextChoices):
        ACTIVE = 'active', _('Active')
        REDEEMED = 'redeemed', _('Redeemed / Converted')
        CANCELLED = 'cancelled', _('Cancelled')

    class ReceiptType(models.TextChoices):
        ADVANCE = 'advance', _('Advance Booking')
        ON_ACCOUNT = 'on_account', _('On-Account Payment')

    class PaymentMethod(models.TextChoices):
        UPI = 'upi', _('UPI')
        CASH = 'cash', _('Cash')
        CARD = 'card', _('Card')

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='advance_receipts')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='advance_receipts')
    receipt_no = models.CharField(max_length=50, db_index=True)
    receipt_type = models.CharField(max_length=20, choices=ReceiptType.choices, default=ReceiptType.ON_ACCOUNT)
    reference_number = models.CharField(max_length=100, blank=True, default='', db_index=True)
    booking_ref = models.CharField(max_length=50, db_index=True, blank=True, default='')
    party = models.ForeignKey('Party', on_delete=models.SET_NULL, null=True, blank=True, related_name='advance_receipts')
    customer_name = models.CharField(max_length=200)
    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    event_date = models.DateField()
    event_time = models.TimeField(null=True, blank=True)
    guest_count = models.PositiveIntegerField(default=1)
    advance_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    payment_method = models.CharField(max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.UPI)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=ReceiptStatus.choices, default=ReceiptStatus.ACTIVE)
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, related_name='receipt_records')
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ['restaurant', 'receipt_no']

    def __str__(self):
        return f"{self.receipt_no} ({self.customer_name} - ₹{self.advance_amount})"

    def save(self, *args, **kwargs):
        from django.db import transaction

        if not self.receipt_no and self.restaurant:
            with transaction.atomic():
                last_receipt = AdvanceReceipt.objects.filter(restaurant=self.restaurant).order_by('-id').first()
                next_num = (last_receipt.id + 1001) if last_receipt else 1001
                self.receipt_no = f"REC-{next_num}"
                while AdvanceReceipt.objects.filter(restaurant=self.restaurant, receipt_no=self.receipt_no).exists():
                    next_num += 1
                    self.receipt_no = f"REC-{next_num}"

        if not self.reference_number and self.receipt_type == self.ReceiptType.ADVANCE:
            if self.booking_ref:
                self.reference_number = self.booking_ref
            elif self.order and self.order.order_number:
                self.reference_number = self.order.order_number
            elif self.restaurant:
                with transaction.atomic():
                    last_receipt = AdvanceReceipt.objects.filter(restaurant=self.restaurant).order_by('-id').first()
                    next_num = (last_receipt.id + 1001) if last_receipt else 1001
                    self.reference_number = f"ORD-{next_num}"
                    while AdvanceReceipt.objects.filter(restaurant=self.restaurant, reference_number=self.reference_number).exists():
                        next_num += 1
                        self.reference_number = f"ORD-{next_num}"

        if not self.booking_ref and self.reference_number:
            self.booking_ref = self.reference_number

        super().save(*args, **kwargs)

from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

@receiver([post_save, post_delete], sender=Order)
def broadcast_table_status(sender, instance, **kwargs):
    """
    Broadcasts the live table status to the admin dashboard when an Order is saved or deleted.
    """
    restaurant = instance.restaurant
    if not restaurant and hasattr(instance, 'table') and instance.table:
        restaurant = instance.table.restaurant

    if not restaurant or not getattr(restaurant, 'slug', None):
        return

    table = instance.table
    if not table and instance.table_number:
        table = Table.objects.filter(restaurant=restaurant, table_number=instance.table_number).first()

    if table:
        try:
            # pyrefly: ignore [missing-import]
            from .sse import publish
            publish(restaurant.slug, {
                'type': 'table_status_update',
                'table_id': table.id,
                'status': table.current_status
            })
        except Exception as e:
            print("Error publishing table status update:", e)


class Branch(models.Model):
    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='internal_branches')
    branch_name = models.CharField(max_length=200)
    branch_code = models.CharField(max_length=50, blank=True, null=True)
    address_line_1 = models.CharField(max_length=255, blank=True, null=True)
    address_line_2 = models.CharField(max_length=255, blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    state = models.CharField(max_length=100, blank=True, null=True)
    pincode = models.CharField(max_length=20, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    gstin = models.CharField(max_length=50, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'branches'
        ordering = ['branch_name']
        verbose_name = 'Internal Branch'
        verbose_name_plural = 'Internal Branches'

    def __str__(self):
        return f"{self.branch_name} ({self.branch_code})" if self.branch_code else self.branch_name

    @property
    def full_address(self):
        parts = [self.address_line_1, self.address_line_2, self.city, self.state, self.pincode]
        non_empty = [p.strip() for p in parts if p and p.strip()]
        if non_empty:
            return ", ".join(non_empty)
        return self.address or ""



class DeliveryChallan(models.Model):
    class StatusChoices(models.TextChoices):
        IN_TRANSIT = 'in_transit', _('In Transit')
        RECEIVED = 'received', _('Received')
        CANCELLED = 'cancelled', _('Cancelled')

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='delivery_challans')
    challan_no = models.CharField(max_length=50, db_index=True)
    from_branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.SET_NULL, related_name='challans_dispatched', null=True, blank=True)
    to_branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.SET_NULL, related_name='challans_received', null=True, blank=True)
    
    # Transport Details
    reason_for_transport = models.CharField(max_length=100, default='Branch Transfer')
    transport_mode = models.CharField(max_length=50, default='Road')
    vehicle_no = models.CharField(max_length=50, blank=True, null=True)

    # Receiver Details
    receiver_party = models.ForeignKey('Party', on_delete=models.SET_NULL, null=True, blank=True, related_name='delivery_challans')
    receiver_name = models.CharField(max_length=200, blank=True, null=True)
    receiver_address = models.TextField(blank=True, null=True)
    receiver_state = models.CharField(max_length=100, blank=True, null=True)
    receiver_gstin = models.CharField(max_length=15, blank=True, null=True)

    dispatch_date = models.DateField()
    status = models.CharField(max_length=20, choices=StatusChoices.choices, default=StatusChoices.IN_TRANSIT)
    
    # Financial & Tax Summaries
    total_value = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    cgst_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    sgst_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    igst_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    total_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)

    notes = models.TextField(blank=True, null=True)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'delivery_challans'
        ordering = ['-dispatch_date', '-created_at']

    def __str__(self):
        dest = self.receiver_name or (self.to_branch.branch_name if self.to_branch else 'Destination')
        return f"DC #{self.challan_no} -> {dest}"


class DeliveryChallanItem(models.Model):
    challan = models.ForeignKey(DeliveryChallan, on_delete=models.CASCADE, related_name='items')
    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name='challan_items', null=True, blank=True)
    menu_item = models.ForeignKey('MenuItem', on_delete=models.SET_NULL, null=True, blank=True, related_name='challan_items')
    hsn_code = models.CharField(max_length=20, blank=True, null=True)
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    unit = models.CharField(max_length=20, blank=True, null=True)
    unit_value = models.DecimalField(max_digits=10, decimal_places=2)
    line_total = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        db_table = 'delivery_challan_items'

    @property
    def item_name(self):
        if self.menu_item:
            return self.menu_item.name
        elif self.inventory_item:
            return self.inventory_item.name
        return "Unknown Item"

    def __str__(self):
        return f"{self.quantity} {self.unit or ''} x {self.item_name} @ ₹{self.unit_value}"


class BlogPost(models.Model):
    class ShapePattern(models.TextChoices):
        ARCH = 'arch', _('Arch Shape')
        RECTANGLE = 'rectangle', _('Rectangular Shape')

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='blog_posts')
    title = models.CharField(max_length=250)
    slug = models.SlugField(max_length=250, blank=True)
    category = models.CharField(max_length=150, default="Deserts, Salads", help_text="Comma-separated or main category")
    author = models.CharField(max_length=100, default="Admin")
    cover_image = models.ImageField(upload_to='blogs/covers/', blank=True, null=True)
    shape_pattern = models.CharField(max_length=20, choices=ShapePattern.choices, default=ShapePattern.ARCH)
    excerpt = models.TextField(blank=True, help_text="Short summary for blog card and SEO description")
    intro_content = models.TextField(blank=True, help_text="Introductory paragraph")
    section_1_title = models.CharField(max_length=250, blank=True)
    section_1_text = models.TextField(blank=True)
    quote = models.TextField(blank=True, help_text="Highlighted quote in blog post")
    section_2_title = models.CharField(max_length=250, blank=True)
    section_2_text = models.TextField(blank=True)
    gallery_image_1 = models.ImageField(upload_to='blogs/gallery/', blank=True, null=True)
    gallery_image_2 = models.ImageField(upload_to='blogs/gallery/', blank=True, null=True)
    tags = models.CharField(max_length=250, blank=True, help_text="Comma-separated tags e.g. FOOD, ORGANIC, PUB")
    seo_title = models.CharField(max_length=255, blank=True)
    seo_description = models.TextField(blank=True)
    is_published = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['display_order', '-created_at']
        verbose_name = 'Blog Post'
        verbose_name_plural = 'Blog Posts'
        unique_together = ['restaurant', 'slug']

    def __str__(self):
        return f"{self.title} ({self.restaurant.name})"

    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify
            base_slug = slugify(self.title)
            slug = base_slug
            count = 1
            while BlogPost.objects.filter(restaurant=self.restaurant, slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug}-{count}"
                count += 1
            self.slug = slug
        super().save(*args, **kwargs)


class BlogComment(models.Model):
    post = models.ForeignKey(BlogPost, on_delete=models.CASCADE, related_name='comments')
    author_name = models.CharField(max_length=100)
    author_email = models.EmailField(blank=True)
    content = models.TextField()
    is_approved = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Blog Comment'
        verbose_name_plural = 'Blog Comments'

    def __str__(self):
        return f"Comment by {self.author_name} on {self.post.title}"


class EmailOTP(models.Model):
    class Purpose(models.TextChoices):
        LOGIN = 'LOGIN', 'Login Verification'
        PASSWORD_RESET = 'PASSWORD_RESET', 'Password Reset'
        EMAIL_VERIFY = 'EMAIL_VERIFY', 'Email Verification'

    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='email_otps')
    email = models.EmailField(help_text="Email address the OTP was dispatched to")
    otp_code = models.CharField(max_length=6, help_text="6-digit numerical code")
    purpose = models.CharField(max_length=20, choices=Purpose.choices, default=Purpose.LOGIN)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=5)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Email OTP'
        verbose_name_plural = 'Email OTPs'
        indexes = [
            models.Index(fields=['user', 'purpose', 'is_used']),
            models.Index(fields=['email', 'otp_code']),
        ]

    def __str__(self):
        return f"{self.purpose} OTP for {self.user.username} ({self.email}) - {'Used' if self.is_used else ('Expired' if self.is_expired else 'Active')}"

    @property
    def is_expired(self):
        from django.utils import timezone
        return timezone.now() >= self.expires_at

    @property
    def is_valid(self):
        return not self.is_used and not self.is_expired and self.attempts < self.max_attempts

    @classmethod
    def generate_otp(cls, user, email=None, purpose=Purpose.LOGIN, expiry_minutes=10, max_attempts=5, ip_address=None):
        import secrets
        from datetime import timedelta
        from django.utils import timezone

        dest_email = email or user.email
        if not dest_email:
            raise ValueError("User does not have an email address configured.")

        # Invalidate previous unused OTPs for same user and purpose
        cls.objects.filter(user=user, purpose=purpose, is_used=False).update(is_used=True)

        # Generate secure 6-digit code
        code = f"{secrets.randbelow(900000) + 100000:06d}"
        expires_at = timezone.now() + timedelta(minutes=expiry_minutes)

        return cls.objects.create(
            user=user,
            email=dest_email,
            otp_code=code,
            purpose=purpose,
            expires_at=expires_at,
            max_attempts=max_attempts,
            ip_address=ip_address,
        )

    def verify(self, code_input):
        """
        Verifies the given code.
        Returns (success: bool, error_message: str or None)
        """
        if self.is_used:
            return False, "This verification code has already been used. Please request a new code."

        if self.is_expired:
            return False, "This verification code has expired. Please request a new code."

        if self.attempts >= self.max_attempts:
            return False, "Maximum verification attempts exceeded. Please request a new code."

        clean_input = str(code_input or '').strip()
        if clean_input == self.otp_code:
            self.is_used = True
            self.save(update_fields=['is_used'])
            return True, None

        self.attempts += 1
        self.save(update_fields=['attempts'])
        remaining = max(0, self.max_attempts - self.attempts)
        if remaining > 0:
            return False, f"Invalid verification code. {remaining} attempt{'s' if remaining != 1 else ''} remaining."
        return False, "Invalid verification code. Maximum attempts exceeded. Please request a new code."


class CreditNote(models.Model):
    class ReasonCode(models.TextChoices):
        DEFECTIVE_FOOD = 'DEFECTIVE_FOOD', _('Food Quality / Defective')
        WRONG_ORDER = 'WRONG_ORDER', _('Wrong Item Served / Prepared')
        CUSTOMER_DISPUTE = 'CUSTOMER_DISPUTE', _('Customer Dispute / Dissatisfaction')
        DELAYED_SERVICE = 'DELAYED_SERVICE', _('Excessive Preparation Delay')
        BILLING_ERROR = 'BILLING_ERROR', _('Billing / Punching Error')
        OTHER = 'OTHER', _('Other Reason')

    class RefundRoute(models.TextChoices):
        CASH = 'CASH', _('Cash Refund')
        ORIGINAL_METHOD = 'ORIGINAL_METHOD', _('Original Payment Method')
        UPI = 'UPI', _('UPI Transfer')
        CARD = 'CARD', _('Card Reversal')
        LEDGER_CREDIT = 'LEDGER_CREDIT', _('Credit to Customer Ledger')

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='credit_notes')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='credit_notes')
    order = models.OneToOneField('Order', on_delete=models.CASCADE, related_name='credit_note')
    credit_note_number = models.CharField(max_length=50, unique=True, db_index=True)
    original_invoice_no = models.CharField(max_length=100)
    reason_code = models.CharField(max_length=50, choices=ReasonCode.choices, default=ReasonCode.BILLING_ERROR)
    reason_description = models.CharField(max_length=255, blank=True)
    refund_route = models.CharField(max_length=50, choices=RefundRoute.choices, blank=True, null=True)

    # Negative / adjusted financial amounts
    subtotal_adjusted = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cgst_adjusted = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    sgst_adjusted = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    vat_adjusted = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_adjusted = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    service_charge_adjusted = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_adjusted = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    round_off_adjusted = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_refund_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    authorized_by = models.CharField(max_length=100, default='Manager')
    counter = models.ForeignKey('POSCounter', on_delete=models.SET_NULL, null=True, blank=True, related_name='credit_notes')
    shift = models.ForeignKey('POSShift', on_delete=models.SET_NULL, null=True, blank=True, related_name='credit_notes')
    cashier = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='pos_credit_notes')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Credit Note'
        verbose_name_plural = 'Credit Notes'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.credit_note_number} (Ref: {self.original_invoice_no}) - ₹{self.total_refund_amount}"

    @classmethod
    def generate_credit_note_number(cls, restaurant, branch=None):
        from django.utils import timezone
        today_str = timezone.now().strftime('%Y%m%d')
        prefix = f"CN-{today_str}-"
        qs = cls.objects.filter(restaurant=restaurant, credit_note_number__startswith=prefix)
        count = qs.count() + 1
        return f"{prefix}{count:04d}"


class POSCounter(models.Model):
    """
    Logical master record representing a billing desk or service counter.
    """
    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='pos_counters')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='pos_counters')
    name = models.CharField(max_length=100, help_text="e.g. Main Desk, Bar Counter, Express Takeaway")
    place = models.CharField(max_length=150, blank=True, default='', help_text="Optional location e.g. Ground Floor Lobby, Rooftop Lounge")
    standard_opening_amount = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=Decimal('0.00'), 
        help_text="Default base cash float configured for this counter"
    )
    is_active = models.BooleanField(default=True, help_text="Enable or disable this counter for shift selection")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        unique_together = ('restaurant', 'branch', 'name')
        verbose_name = 'POS Counter'
        verbose_name_plural = 'POS Counters'

    def __str__(self):
        loc_str = f" ({self.place})" if self.place else ""
        return f"{self.name}{loc_str}"

    @property
    def current_active_shift(self):
        return self.shifts.filter(status=POSShift.Status.OPEN).first()


class POSShift(models.Model):
    """
    Transactional record representing a cashier operating session against a specific logical counter.
    """
    class Status(models.TextChoices):
        OPEN = 'OPEN', _('Open / Active')
        CLOSED = 'CLOSED', _('Closed / Reconciled')

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='pos_shifts')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='pos_shifts')
    counter = models.ForeignKey(POSCounter, on_delete=models.CASCADE, related_name='shifts')
    cashier = models.ForeignKey('auth.User', on_delete=models.CASCADE, related_name='pos_shifts')
    
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    
    opening_amount = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=Decimal('0.00'), 
        help_text="Actual physical cash float in drawer at start of shift"
    )
    closing_cash_actual = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        null=True, 
        blank=True, 
        help_text="Actual cash counted by cashier at close of register"
    )
    closing_cash_expected = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=Decimal('0.00'), 
        help_text="Opening Amount + Cash Sales - Cash Refunds"
    )
    cash_discrepancy = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=Decimal('0.00'), 
        help_text="Actual - Expected (Positive = Overage, Negative = Shortage)"
    )
    notes = models.TextField(blank=True, default='')
    closed_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='closed_pos_shifts')

    class Meta:
        ordering = ['-opened_at']
        verbose_name = 'POS Shift'
        verbose_name_plural = 'POS Shifts'

    def __str__(self):
        return f"Shift #{self.id} - {self.counter.name} ({self.cashier.get_full_name() or self.cashier.username}) [{self.status}]"

    def calculate_expected_cash(self):
        """
        Expected Cash = Opening Amount + Cash Payments Collected - Cash Refunds Issued
        """
        cash_sales = Decimal('0.00')
        for order in self.orders.exclude(status=Order.OrderStatus.CANCELLED):
            order_payments = order.payments or []
            if not order_payments and order.paid_amount and order.paid_amount > Decimal('0.00'):
                if str(order.payment_method or '').lower() == 'cash':
                    cash_sales += Decimal(str(order.paid_amount))
            else:
                for p in order_payments:
                    if str(p.get('method', '')).lower() == 'cash':
                        try:
                            cash_sales += Decimal(str(p.get('amount') or 0))
                        except Exception:
                            pass

        cash_refunds = Decimal('0.00')
        for cn in self.credit_notes.all():
            if cn.refund_route == CreditNote.RefundRoute.CASH:
                try:
                    cash_refunds += Decimal(str(cn.total_refund_amount or 0))
                except Exception:
                    pass

        return (Decimal(str(self.opening_amount or 0)) + cash_sales - cash_refunds).quantize(Decimal('0.01'))

    def get_financial_summary(self):
        """
        Detailed breakdown for X-Report / Z-Report
        """
        cash_sales = Decimal('0.00')
        upi_sales = Decimal('0.00')
        card_sales = Decimal('0.00')
        other_sales = Decimal('0.00')
        total_sales = Decimal('0.00')

        orders_qs = self.orders.exclude(status=Order.OrderStatus.CANCELLED)
        order_count = orders_qs.count()

        for order in orders_qs:
            order_total = Decimal(str(order.total_amount or 0))
            total_sales += order_total
            order_payments = order.payments or []
            if not order_payments and order.paid_amount and order.paid_amount > Decimal('0.00'):
                method = str(order.payment_method or '').lower()
                amt = Decimal(str(order.paid_amount))
                if method == 'cash':
                    cash_sales += amt
                elif method == 'upi':
                    upi_sales += amt
                elif method == 'card':
                    card_sales += amt
                else:
                    other_sales += amt
            else:
                for p in order_payments:
                    method = str(p.get('method', '')).lower()
                    try:
                        amt = Decimal(str(p.get('amount') or 0))
                    except Exception:
                        amt = Decimal('0.00')
                    if method == 'cash':
                        cash_sales += amt
                    elif method == 'upi':
                        upi_sales += amt
                    elif method == 'card':
                        card_sales += amt
                    else:
                        other_sales += amt

        refunds_qs = self.credit_notes.all()
        refund_count = refunds_qs.count()
        cash_refunds = Decimal('0.00')
        total_refunds = Decimal('0.00')
        for cn in refunds_qs:
            try:
                r_amt = Decimal(str(cn.total_refund_amount or 0))
            except Exception:
                r_amt = Decimal('0.00')
            total_refunds += r_amt
            if cn.refund_route == CreditNote.RefundRoute.CASH:
                cash_refunds += r_amt

        expected_cash = (Decimal(str(self.opening_amount or 0)) + cash_sales - cash_refunds).quantize(Decimal('0.01'))
        actual_cash = Decimal(str(self.closing_cash_actual or 0)) if self.closing_cash_actual is not None else None
        discrepancy = (actual_cash - expected_cash) if actual_cash is not None else Decimal('0.00')

        return {
            'order_count': order_count,
            'total_sales': total_sales.quantize(Decimal('0.01')),
            'cash_sales': cash_sales.quantize(Decimal('0.01')),
            'upi_sales': upi_sales.quantize(Decimal('0.01')),
            'card_sales': card_sales.quantize(Decimal('0.01')),
            'other_sales': other_sales.quantize(Decimal('0.01')),
            'refund_count': refund_count,
            'total_refunds': total_refunds.quantize(Decimal('0.01')),
            'cash_refunds': cash_refunds.quantize(Decimal('0.01')),
            'opening_amount': Decimal(str(self.opening_amount or 0)).quantize(Decimal('0.01')),
            'expected_cash': expected_cash,
            'actual_cash': actual_cash.quantize(Decimal('0.01')) if actual_cash is not None else None,
            'discrepancy': discrepancy.quantize(Decimal('0.01')),
        }

class StockCount(models.Model):
    """A physical stock take: staff count what is on the shelf, then posting corrects the books."""
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', _('Counting')
        POSTED = 'POSTED', _('Posted')
        CANCELLED = 'CANCELLED', _('Cancelled')

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='stock_counts')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='stock_counts')
    title = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    note = models.TextField(blank=True)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    posted_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title or f"Stock count {self.created_at:%d %b %Y}"


class StockCountLine(models.Model):
    count = models.ForeignKey(StockCount, on_delete=models.CASCADE, related_name='lines')
    item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name='count_lines')
    expected = models.IntegerField(help_text="Book quantity when the count started")
    counted = models.IntegerField(null=True, blank=True)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        ordering = ['item__name']
        unique_together = ('count', 'item')

    @property
    def variance(self):
        return None if self.counted is None else self.counted - self.expected

    @property
    def variance_value(self):
        v = self.variance
        return None if v is None else (Decimal(v) * (self.unit_cost or 0))


class GatewayPayment(models.Model):
    """One row per online payment we accept, so the same Razorpay payment can never be
    claimed twice (for another order, room booking or event ticket)."""
    KIND_CHOICES = [('order', 'Restaurant order'), ('room', 'Room booking'), ('event', 'Event ticket')]

    provider = models.CharField(max_length=20, default='razorpay')
    payment_id = models.CharField(max_length=100)
    gateway_order_id = models.CharField(max_length=100, blank=True, default='')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    object_id = models.PositiveBigIntegerField()
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['provider', 'payment_id'], name='uniq_gateway_payment')]
        indexes = [models.Index(fields=['kind', 'object_id'])]

    def __str__(self):
        return f"{self.provider}:{self.payment_id} → {self.kind} #{self.object_id}"

    @classmethod
    def claim(cls, payment_id, kind, object_id, gateway_order_id='', amount=None, provider='razorpay'):
        """Record the payment against this object. False when it was already used for a
        different object (a replay); True when new or already ours (an idempotent retry)."""
        from django.db import IntegrityError, transaction
        if not payment_id:
            return False
        existing = cls.objects.filter(provider=provider, payment_id=payment_id).first()
        if existing:
            return existing.kind == kind and existing.object_id == object_id
        try:
            with transaction.atomic():
                cls.objects.create(provider=provider, payment_id=payment_id, kind=kind, object_id=object_id,
                                   gateway_order_id=gateway_order_id or '', amount=amount)
        except IntegrityError:
            return cls.objects.filter(provider=provider, payment_id=payment_id, kind=kind, object_id=object_id).exists()
        return True


class ProcurementOrder(models.Model):
    """TallyPrime-style Purchase Order: what we have asked a supplier for. It moves no stock and
    no money; Goods Receipt Notes are tracked against it until it is received or short-closed."""
    class Status(models.TextChoices):
        DRAFT = 'draft', _('Draft')
        OPEN = 'open', _('Approved / open')
        PARTIAL = 'partial', _('Partly received')
        RECEIVED = 'received', _('Fully received')
        CLOSED = 'closed', _('Short-closed')
        CANCELLED = 'cancelled', _('Cancelled')

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='procurement_orders')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='procurement_orders')
    party = models.ForeignKey(Party, on_delete=models.SET_NULL, null=True, blank=True, related_name='procurement_orders')
    supplier_name = models.CharField(max_length=200)
    supplier_gstin = models.CharField(max_length=50, blank=True, default='')
    supplier_state = models.CharField(max_length=100, blank=True, default='')
    number = models.CharField(max_length=100)
    order_date = models.DateField()
    expected_date = models.DateField(null=True, blank=True, help_text="Due date for delivery")
    credit_days = models.PositiveIntegerField(default=0, help_text="Supplier credit period, copied to the bill due date")
    is_interstate = models.BooleanField(default=False)
    reverse_charge = models.BooleanField(default=False)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    sgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    igst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    freight_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    terms = models.TextField(blank=True, default='')
    narration = models.TextField(blank=True, default='')
    close_reason = models.CharField(max_length=255, blank=True, default='')
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    approved_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    OPEN_STATUSES = ('open', 'partial')

    class Meta:
        ordering = ['-order_date', '-id']

    def __str__(self):
        return f"{self.number} - {self.supplier_name}"

    @property
    def is_overdue(self):
        from django.utils import timezone
        return bool(self.status in self.OPEN_STATUSES and self.expected_date and self.expected_date < timezone.localdate())


class ProcurementOrderLine(models.Model):
    order = models.ForeignKey(ProcurementOrder, on_delete=models.CASCADE, related_name='lines')
    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name='order_lines')
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    received_qty = models.DecimalField(max_digits=12, decimal_places=3, default=0, help_text="Accepted on GRNs")
    rate = models.DecimalField(max_digits=12, decimal_places=2)
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    hsn_code = models.CharField(max_length=50, blank=True, default='')
    taxable_value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        ordering = ['id']

    @property
    def pending_qty(self):
        return max((self.quantity or 0) - (self.received_qty or 0), Decimal('0'))

    @property
    def net_rate(self):
        return (self.rate * (Decimal('100') - (self.discount_pct or 0)) / Decimal('100')).quantize(Decimal('0.01'))


class GoodsReceiptNote(models.Model):
    """Receipt Note: goods physically received (stock goes up), waiting for the supplier's bill."""
    class Status(models.TextChoices):
        PENDING_BILL = 'pending', _('Bill pending')
        BILLED = 'billed', _('Billed')
        CANCELLED = 'cancelled', _('Cancelled')

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='goods_receipts')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='goods_receipts')
    party = models.ForeignKey(Party, on_delete=models.SET_NULL, null=True, blank=True, related_name='goods_receipts')
    order = models.ForeignKey(ProcurementOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name='receipts')
    bill = models.ForeignKey('PurchaseOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='receipts')
    supplier_name = models.CharField(max_length=200)
    supplier_gstin = models.CharField(max_length=50, blank=True, default='')
    number = models.CharField(max_length=100)
    receipt_date = models.DateField()
    challan_no = models.CharField(max_length=100, blank=True, default='', help_text="Supplier delivery challan / dispatch no.")
    vehicle_no = models.CharField(max_length=50, blank=True, default='')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING_BILL)
    value = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="Accepted quantity at order rate, before tax")
    note = models.TextField(blank=True, default='')
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-receipt_date', '-id']

    def __str__(self):
        return f"{self.number} - {self.supplier_name}"


class GoodsReceiptLine(models.Model):
    grn = models.ForeignKey(GoodsReceiptNote, on_delete=models.CASCADE, related_name='lines')
    order_line = models.ForeignKey(ProcurementOrderLine, on_delete=models.SET_NULL, null=True, blank=True, related_name='receipt_lines')
    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name='receipt_lines')
    received_qty = models.DecimalField(max_digits=12, decimal_places=3)
    rejected_qty = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    rejection_reason = models.CharField(max_length=255, blank=True, default='')
    rate = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    hsn_code = models.CharField(max_length=50, blank=True, default='')
    batch_no = models.CharField(max_length=60, blank=True, default='')
    expiry_date = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ['id']

    @property
    def accepted_qty(self):
        return max((self.received_qty or 0) - (self.rejected_qty or 0), Decimal('0'))


class PurchaseReturn(models.Model):
    """Rejection Out / Debit Note: goods sent back to the supplier after the bill. Stock goes down
    and the supplier owes us (reduces the bill's balance)."""
    class Reason(models.TextChoices):
        DAMAGED = 'damaged', _('Damaged / broken')
        EXPIRED = 'expired', _('Expired / near expiry')
        QUALITY = 'quality', _('Quality not as ordered')
        EXCESS = 'excess', _('Excess / not ordered')
        RATE = 'rate', _('Rate difference')
        OTHER = 'other', _('Other')

    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='purchase_returns')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='purchase_returns')
    party = models.ForeignKey(Party, on_delete=models.SET_NULL, null=True, blank=True, related_name='purchase_returns')
    bill = models.ForeignKey('PurchaseOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='returns')
    supplier_name = models.CharField(max_length=200)
    supplier_gstin = models.CharField(max_length=50, blank=True, default='')
    number = models.CharField(max_length=100)
    return_date = models.DateField()
    reason = models.CharField(max_length=10, choices=Reason.choices, default=Reason.DAMAGED)
    is_interstate = models.BooleanField(default=False)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    sgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    igst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    note = models.TextField(blank=True, default='')
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-return_date', '-id']

    def __str__(self):
        return f"{self.number} - {self.supplier_name}"


class PurchaseReturnLine(models.Model):
    purchase_return = models.ForeignKey(PurchaseReturn, on_delete=models.CASCADE, related_name='lines')
    bill_line = models.ForeignKey(PurchaseOrderItem, on_delete=models.SET_NULL, null=True, blank=True, related_name='return_lines')
    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name='return_lines')
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    rate = models.DecimalField(max_digits=12, decimal_places=2)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    taxable_value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        ordering = ['id']


class PurchasePayment(models.Model):
    """Bill-wise payment to a supplier (Tally's 'Agst Ref' settlement)."""
    bill = models.ForeignKey('PurchaseOrder', on_delete=models.CASCADE, related_name='bill_payments')
    paid_on = models.DateField()
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=20, choices=PurchaseOrder.PaymentMethod.choices, default=PurchaseOrder.PaymentMethod.BANK_TRANSFER)
    reference = models.CharField(max_length=100, blank=True, default='', help_text="UTR / cheque / UPI ref")
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-paid_on', '-id']


# Keep the per-request memo (core.request_cache) honest when these change mid-request.
from django.db.models.signals import post_delete as _post_delete, post_save as _post_save  # noqa: E402


def _forget_request_memo(sender, **kwargs):
    from core.request_cache import forget
    forget({RestaurantSettings: ('settings',), CustomCharge: ('charges',)}.get(sender, ('first_branch',)))


for _model in (RestaurantSettings, CustomCharge):
    _post_save.connect(_forget_request_memo, sender=_model, dispatch_uid=f'memo_save_{_model.__name__}')
    _post_delete.connect(_forget_request_memo, sender=_model, dispatch_uid=f'memo_delete_{_model.__name__}')
_post_save.connect(_forget_request_memo, sender='super_admin.RestaurantBranch', dispatch_uid='memo_save_branch')
