from django.db import models
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator, MaxValueValidator


class Zone(models.Model):
    name = models.CharField(max_length=100)
    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='zones')
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='zones')

    class Meta:
        verbose_name = 'Zone'
        verbose_name_plural = 'Zones'
        ordering = ['name']
        unique_together = ['name', 'restaurant']

    def __str__(self):
        restaurant_name = self.restaurant.name if self.restaurant else "Unknown"
        return f"{restaurant_name} - {self.name}"


class Table(models.Model):
    name = models.CharField(max_length=100)
    table_number = models.PositiveIntegerField(default=1)
    capacity = models.PositiveIntegerField(default=4)
    qr_code = models.ImageField(upload_to='table_qrs', blank=True, null=True)
    zone = models.ForeignKey(Zone, on_delete=models.PROTECT, related_name='tables', null=True, blank=True)
    restaurant = models.ForeignKey('super_admin.Restaurant', on_delete=models.CASCADE, related_name='tables', null=True, blank=True)
    branch = models.ForeignKey('super_admin.RestaurantBranch', on_delete=models.CASCADE, null=True, blank=True, related_name='tables')
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = (
            ('restaurant', 'zone', 'table_number'),
            ('restaurant', 'zone', 'name'),
        )
        ordering = ['zone', 'table_number']

    def __str__(self):
        zone_name = self.zone.name if self.zone else "No Zone"
        return f"{self.name} (Zone: {zone_name})"

    def save(self, *args, **kwargs):
        import uuid
        import os
        from django.conf import settings
        
        # Only try QR generation if qrcode library is available
        if not self.qr_code:
            try:
                import qrcode
                from io import BytesIO
                from django.core.files import File
                
                # Determine base domain
                base_domain = getattr(settings, 'FRONTEND_URL', os.environ.get('FRONTEND_URL', 'http://localhost:3000'))
                restaurant_slug = self.restaurant.slug if self.restaurant else 'default'
                
                # e.g. https://domain.com/r/my-restaurant/menu?table=5
                url = f"{base_domain.rstrip('/')}/r/{restaurant_slug}/menu?table={self.table_number}"
                
                qr = qrcode.make(url)
                canvas = BytesIO()
                # pyrefly: ignore [unexpected-keyword]
                qr.save(canvas, format='PNG')
                filename = f"rest_{self.restaurant.id if self.restaurant else 0}_table_{self.table_number}_{uuid.uuid4().hex[:8]}.png"
                self.qr_code.save(filename, File(canvas), save=False)
            except ImportError:
                # qrcode not installed, skip generation
                pass
                
        super().save(*args, **kwargs)

    @property
    def current_status(self):
        # Active orders that are NOT cancelled, NOT failed, and NOT fully paid
        active_orders = self.orders.exclude(status='C').exclude(payment_status__in=['F', 'paid', 'P'])
        if not active_orders.exists():
            return 'BLANK'

        # Check if any active order is PENDING (Running KOT)
        if active_orders.filter(status='P').exists():
            return 'RUNNING_KOT'

        # Check if any active order is PREPARING (Running)
        if active_orders.filter(status='PR').exists():
            return 'RUNNING'

        # Check if any active order is SERVED (Billed / Waiting for payment)
        if active_orders.filter(status='S').exists():
            return 'PRINTED'

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

    name = models.CharField(max_length=200)
    short_code = models.CharField(max_length=50, blank=True, null=True, db_index=True, help_text="Fast POS item code (e.g. 101, 301, NAAN)")
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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} (${self.price})"


class Order(models.Model):
    class OrderStatus(models.TextChoices):
        DRAFT = 'DRAFT', _('Draft / Scheduled')
        PENDING = 'P', _('Pending')
        PREPARING = 'PR', _('Preparing')
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

    customer_name = models.CharField(max_length=200)
    phone = models.CharField(max_length=20, blank=True, default='')
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

    class Meta:
        ordering = ['-order_time']

    @property
    def net_payable(self):
        from decimal import Decimal
        tot = self.total_amount if (self.total_amount is not None and self.total_amount > Decimal('0.00')) else self.calculate_bill_breakdown()['final_total']
        return max(Decimal('0.00'), tot - (self.advance_amount_paid or Decimal('0.00')))

    @property
    def order_number(self):
        num = self.branch_order_number if self.branch_order_number is not None else self.id
        return f"ORD-{num:04d}" if num else ""

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

    def save(self, *args, **kwargs):
        import uuid
        from django.db import transaction
        from django.db.models import Max

        if not self.security_token:
            self.security_token = str(uuid.uuid4())

        # Auto-assign branch_order_number per restaurant if missing
        if not self.branch_order_number and self.restaurant:
            with transaction.atomic():
                max_num = Order.objects.filter(restaurant=self.restaurant).aggregate(Max('branch_order_number'))['branch_order_number__max'] or 0
                self.branch_order_number = max_num + 1

        # Sync table_number from Table object if present
        if self.table and not self.table_number:
            self.table_number = self.table.table_number

        # Auto-link Party object if missing but customer_name and restaurant are set
        if not self.party and self.customer_name and self.customer_name.lower() not in ['guest', 'walk-in guest'] and self.restaurant:
            party_qs = Party.objects.filter(restaurant=self.restaurant, name__iexact=self.customer_name)
            if self.branch:
                party_qs = party_qs.filter(branch=self.branch)
            matched_party = party_qs.first()
            if matched_party:
                self.party = matched_party

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

    def calculate_bill_breakdown(self, bill_type='combined'):
        from decimal import Decimal
        food_subtotal = Decimal('0.00')
        beer_subtotal = Decimal('0.00')
        imfl_subtotal = Decimal('0.00')
        wine_subtotal = Decimal('0.00')
        gen_liquor_subtotal = Decimal('0.00')

        # Fetch tax rates BEFORE the loop so inclusive items can reverse-calculate
        settings_obj = RestaurantSettings.get_settings(self.restaurant)
        branch_obj = self.branch or (self.restaurant.branches.first() if self.restaurant else None)

        if branch_obj:
            if branch_obj.gst_registration_type == 'R18' or branch_obj.is_hotel_tariff_7500_plus:
                cgst_pct = Decimal('9.00') if bill_type != 'liquor' else Decimal('0.00')
                sgst_pct = Decimal('9.00') if bill_type != 'liquor' else Decimal('0.00')
            elif branch_obj.gst_registration_type in ['C', 'U']:
                cgst_pct = Decimal('0.00')
                sgst_pct = Decimal('0.00')
            else:
                cgst_pct = Decimal(str(settings_obj.cgst_percentage or 2.50)) if bill_type != 'liquor' else Decimal('0.00')
                sgst_pct = Decimal(str(settings_obj.sgst_percentage or 2.50)) if bill_type != 'liquor' else Decimal('0.00')
        else:
            cgst_pct = Decimal(str(settings_obj.cgst_percentage or 2.50)) if bill_type != 'liquor' else Decimal('0.00')
            sgst_pct = Decimal(str(settings_obj.sgst_percentage or 2.50)) if bill_type != 'liquor' else Decimal('0.00')

        if branch_obj and branch_obj.service_charge_percentage is not None:
            service_pct = Decimal(str(branch_obj.service_charge_percentage)) if bill_type != 'liquor' else Decimal('0.00')
        else:
            service_pct = Decimal(str(settings_obj.service_charge_percentage or 0)) if bill_type != 'liquor' else Decimal('0.00')

        vat_beer_pct = Decimal(str(branch_obj.vat_beer_rate if branch_obj else 60.00)) if bill_type != 'food' else Decimal('0.00')
        vat_imfl_pct = Decimal(str(branch_obj.vat_imfl_rate if branch_obj else 65.00)) if bill_type != 'food' else Decimal('0.00')
        vat_wine_pct = Decimal(str(branch_obj.vat_wine_rate if branch_obj else 60.00)) if bill_type != 'food' else Decimal('0.00')
        vat_gen_pct = Decimal(str(getattr(settings_obj, 'vat_percentage', 20) or 20)) if bill_type != 'food' else Decimal('0.00')

        for item in self.items.all():
            line_total = item.subtotal
            ttype = item.menu_item.tax_type if item.menu_item else MenuItem.TaxType.GST_FOOD
            is_inclusive = item.menu_item.is_tax_inclusive if item.menu_item else False

            # For tax-inclusive items, reverse-calculate to extract the base taxable value
            if is_inclusive:
                if ttype == MenuItem.TaxType.VAT_BEER:
                    rate = vat_beer_pct
                elif ttype == MenuItem.TaxType.VAT_IMFL:
                    rate = vat_imfl_pct
                elif ttype == MenuItem.TaxType.VAT_WINE:
                    rate = vat_wine_pct
                elif ttype == MenuItem.TaxType.VAT_LIQUOR:
                    rate = vat_gen_pct
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
        if self.discount_amount and self.discount_amount > Decimal('0.00'):
            discount = Decimal(str(self.discount_amount))
        elif self.coupon:
            try:
                discount = Decimal(str(self.coupon.calculate_discount(gross_subtotal)))
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

        beer_vat = ((discounted_beer * vat_beer_pct) / Decimal('100')).quantize(Decimal('0.01')) if bill_type != 'food' else Decimal('0.00')
        imfl_vat = ((discounted_imfl * vat_imfl_pct) / Decimal('100')).quantize(Decimal('0.01')) if bill_type != 'food' else Decimal('0.00')
        wine_vat = ((discounted_wine * vat_wine_pct) / Decimal('100')).quantize(Decimal('0.01')) if bill_type != 'food' else Decimal('0.00')
        gen_vat = ((discounted_gen_liquor * vat_gen_pct) / Decimal('100')).quantize(Decimal('0.01')) if bill_type != 'food' else Decimal('0.00')
        vat_amount = beer_vat + imfl_vat + wine_vat + gen_vat

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
            'vat_percentage': vat_gen_pct,
            'cgst': cgst.quantize(Decimal('0.01')),
            'sgst': sgst.quantize(Decimal('0.01')),
            'beer_vat': beer_vat.quantize(Decimal('0.01')),
            'imfl_vat': imfl_vat.quantize(Decimal('0.01')),
            'wine_vat': wine_vat.quantize(Decimal('0.01')),
            'gen_vat': gen_vat.quantize(Decimal('0.01')),
            'vat': vat_amount.quantize(Decimal('0.01')),
            'raw_total': raw_total.quantize(Decimal('0.01')),
            'round_off_amount': round_off_amount,
            'final_total': rounded_total.quantize(Decimal('0.01')),
        }

    def update_total(self):
        breakdown = self.calculate_bill_breakdown()
        self.discount_amount = breakdown['discount_amount']
        self.subtotal = breakdown['gross_subtotal']
        self.tax_amount = breakdown['cgst'] + breakdown['sgst'] + breakdown['vat']
        self.round_off_amount = breakdown['round_off_amount']
        self.total_amount = breakdown['final_total']
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
    logo = models.ImageField(upload_to='landing/logo/', blank=True, null=True)
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
    seo_title = models.CharField(max_length=255, blank=True)
    seo_description = models.CharField(max_length=500, blank=True)
    show_hero = models.BooleanField(default=True, verbose_name="Show Hero Section")
    show_about = models.BooleanField(default=True, verbose_name="Show About Us Section")
    show_why_us = models.BooleanField(default=True, verbose_name="Show Why Choose Us Section")
    show_specials = models.BooleanField(default=True, verbose_name="Show Specials Section")
    show_events = models.BooleanField(default=True, verbose_name="Show Events Section")
    show_gallery = models.BooleanField(default=True, verbose_name="Show Gallery Section")
    show_team = models.BooleanField(default=True, verbose_name="Show Team Section")
    show_reviews = models.BooleanField(default=True, verbose_name="Show Reviews Section")
    why_us_title_1 = models.CharField(max_length=100, blank=True, default="")
    why_us_description_1 = models.TextField(blank=True, default="")
    why_us_title_2 = models.CharField(max_length=100, blank=True, default="")
    why_us_description_2 = models.TextField(blank=True, default="")
    why_us_title_3 = models.CharField(max_length=100, blank=True, default="")
    why_us_description_3 = models.TextField(blank=True, default="")
    map_iframe_url = models.TextField(blank=True, default="https://www.google.com/maps/embed?pb=!1m18!1m12!1m3!1d3022.617540232549!2d-73.98785312347953!3d40.74844047138902!2m3!1f0!2f0!3f0!3m2!1i1024!2i768!4f13.1!3m3!1m2!1s0x89c259a9b3117469%3A0xd134e199a405a163!2sEmpire%20State%20Building!5e0!3m2!1sen!2sus!4v1700000000000!5m2!1sen!2sus")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Landing Page'
        verbose_name_plural = 'Landing Pages'

    def __str__(self):
        return f"Landing for {self.restaurant.name}"


class Event(models.Model):
    landing = models.ForeignKey(RestaurantLanding, on_delete=models.CASCADE, related_name='events')
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
        return self.title


class GalleryImage(models.Model):
    landing = models.ForeignKey(RestaurantLanding, on_delete=models.CASCADE, related_name='gallery_images')
    image = models.ImageField(upload_to='landing/gallery/')
    caption = models.CharField(max_length=200, blank=True)
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['display_order', 'id']

    def __str__(self):
        return self.caption or f"Image {self.id}"


class TeamMember(models.Model):
    landing = models.ForeignKey(RestaurantLanding, on_delete=models.CASCADE, related_name='team_members')
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
        return self.name


class Review(models.Model):
    landing = models.ForeignKey(RestaurantLanding, on_delete=models.CASCADE, related_name='reviews')
    name = models.CharField(max_length=150)
    stars = models.PositiveSmallIntegerField(default=5, validators=[MinValueValidator(1), MaxValueValidator(5)])
    description = models.TextField()
    display_order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['display_order', 'id']
        verbose_name = 'Review'
        verbose_name_plural = 'Reviews'

    def __str__(self):
        return f"{self.name} ({self.stars}★)"


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
    quantity = models.IntegerField(default=0)
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

    def adjust(self, delta: int):
        self.quantity = (self.quantity or 0) + int(delta)
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
        from django.db.models import Q
        qs = Order.objects.filter(restaurant=self.restaurant) if self.restaurant else Order.objects.all()
        cond = Q(party=self) | Q(customer_name__iexact=self.name)
        if self.phone:
            cond |= Q(phone=self.phone)
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
        from decimal import Decimal
        from django.db.models import Sum, Q
        from core.models import AdvanceReceipt

        po_paid = Decimal(str(self.purchase_orders.filter(payment_status__in=['paid', 'P']).aggregate(s=Sum('grand_total'))['s'] or 0))
        sales_qs = self.get_sales_queryset().exclude(status='C')

        sales_paid_direct = Decimal(str(sales_qs.aggregate(s=Sum('paid_amount'))['s'] or 0))
        sales_paid_advance = Decimal(str(sales_qs.aggregate(s=Sum('advance_amount_paid'))['s'] or 0))
        sales_paid_legacy = Decimal(str(sales_qs.filter(paid_amount=0, advance_amount_paid=0, payment_status__in=['paid', 'P']).aggregate(s=Sum('total_amount'))['s'] or 0))

        arc_qs = AdvanceReceipt.objects.filter(restaurant=self.restaurant) if self.restaurant else AdvanceReceipt.objects.all()
        cond = Q(party=self) | Q(customer_name__iexact=self.name)
        if self.phone:
            cond |= Q(phone=self.phone)
        unlinked_advances = Decimal(str(arc_qs.filter(cond, status=AdvanceReceipt.ReceiptStatus.ACTIVE).aggregate(s=Sum('advance_amount'))['s'] or 0))

        sales_paid = sales_paid_direct + sales_paid_advance + sales_paid_legacy + unlinked_advances
        return po_paid + sales_paid

    @property
    def current_balance(self):
        from decimal import Decimal
        from django.db.models import Sum, Q
        from core.models import AdvanceReceipt

        base_bal = Decimal(str(self.opening_balance or 0))
        
        unpaid_po = Decimal(str(self.purchase_orders.exclude(payment_status__in=['paid', 'P']).aggregate(s=Sum('grand_total'))['s'] or 0))
        sales_due_explicit = Decimal(str(self.get_sales_queryset().exclude(status='C').aggregate(s=Sum('due_amount'))['s'] or 0))
        sales_due_legacy = Decimal(str(self.get_sales_queryset().filter(due_amount=0).exclude(payment_status__in=['paid', 'P']).exclude(status='C').aggregate(s=Sum('total_amount'))['s'] or 0))
        unpaid_sales = sales_due_explicit + sales_due_legacy

        arc_qs = AdvanceReceipt.objects.filter(restaurant=self.restaurant) if self.restaurant else AdvanceReceipt.objects.all()
        cond = Q(party=self) | Q(customer_name__iexact=self.name)
        if self.phone:
            cond |= Q(phone=self.phone)
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
    invoice_number = models.CharField(max_length=100, help_text="Supplier Bill/Invoice Ref No.")
    purchase_date = models.DateField()
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="Input GST / Tax Amount")
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_method = models.CharField(max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.BANK_TRANSFER)
    payment_status = models.CharField(max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.PAID)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-purchase_date', '-created_at']

    def __str__(self):
        return f"PO #{self.id} - {self.supplier_name} ({self.invoice_number})"


class PurchaseOrderItem(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='items')
    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name='purchase_items')
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, null=True, blank=True)
    hsn_code = models.CharField(max_length=50, null=True, blank=True)
    line_total = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"{self.quantity} x {self.inventory_item.name} for PO #{self.purchase_order_id}"


class InventoryTransaction(models.Model):
    class TransactionType(models.TextChoices):
        ORDER = 'O', _('Order Deduction')
        CANCEL = 'C', _('Order Cancellation Return')
        MANUAL = 'M', _('Manual Adjustment')
        RESTOCK = 'R', _('Restock')
        INWARD_PURCHASE = 'P', _('Inward Purchase')

    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name='transactions')
    transaction_type = models.CharField(max_length=1, choices=TransactionType.choices)
    delta = models.IntegerField(help_text="Positive for restock/returns/purchase, negative for consumption")
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, related_name='inventory_transactions')
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name='inventory_transactions')
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