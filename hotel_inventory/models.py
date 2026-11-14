"""Hotel stores: a stock system of its own for the hotel admin (amenities, housekeeping supplies,
linen, minibar, engineering spares). It is deliberately separate from the restaurant inventory in
``core`` — no shared items, vendors or stock.

Stock is held per ``Location`` in ``StockLevel``; every change is a ``StockMove`` written by
``hotel_inventory.services.move`` (the only code that changes quantities). Moves are never edited
or deleted — a mistake is put right with a reversing move. Items are costed at weighted average
cost (``StoreItem.wac``), recalculated on every inward move; transfers carry the cost unchanged.
"""
from decimal import Decimal

from django.db import models
from django.db.models import Q, Sum

BRANCH = 'super_admin.RestaurantBranch'
D0 = Decimal('0')


class StoreCategory(models.Model):
    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='store_categories')
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='children')
    name = models.CharField(max_length=100)
    hsn_code = models.CharField(max_length=20, blank=True)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ['name']
        constraints = [models.UniqueConstraint(fields=['branch', 'parent', 'name'], name='hinv_category_unique')]
        verbose_name_plural = 'store categories'

    def __str__(self):
        return f"{self.parent.name} › {self.name}" if self.parent_id else self.name


class Vendor(models.Model):
    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='store_vendors')
    name = models.CharField(max_length=150)
    contact_person = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    gstin = models.CharField(max_length=15, blank=True)
    state = models.CharField(max_length=60, blank=True, help_text="Place of supply; decides CGST+SGST or IGST")
    address = models.TextField(blank=True)
    payment_terms_days = models.PositiveSmallIntegerField(default=0, help_text="Credit period in days")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        constraints = [models.UniqueConstraint(fields=['branch', 'name'], name='hinv_vendor_unique')]

    def __str__(self):
        return self.name


class StoreItem(models.Model):
    class Kind(models.TextChoices):
        CONSUMABLE = 'CONSUMABLE', 'Consumable'
        CIRCULATING = 'CIRCULATING', 'Circulating (linen, robes)'
        CHARGEABLE = 'CHARGEABLE', 'Chargeable to guests (minibar)'

    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='store_items')
    name = models.CharField(max_length=150)
    sku = models.CharField(max_length=50, blank=True)
    barcode = models.CharField(max_length=64, blank=True, help_text="EAN / QR value printed on the pack or our label")
    category = models.ForeignKey(StoreCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name='items')
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.CONSUMABLE)
    unit = models.CharField(max_length=20, default='pc', help_text="Base unit: stock is kept and issued in this unit")
    hsn_code = models.CharField(max_length=20, blank=True)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    sale_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="Guest price (minibar)")
    reorder_level = models.DecimalField(max_digits=14, decimal_places=3, default=D0,
                                        help_text="Reorder when stock across all locations falls to this; 0 = off")
    reorder_qty = models.DecimalField(max_digits=14, decimal_places=3, default=D0, help_text="Usual order quantity")
    preferred_vendor = models.ForeignKey(Vendor, on_delete=models.SET_NULL, null=True, blank=True, related_name='items')
    track_expiry = models.BooleanField(default=False)
    wac = models.DecimalField(max_digits=14, decimal_places=4, default=D0, help_text="Weighted average cost per base unit")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(fields=['branch', 'name'], name='hinv_item_unique'),
            models.UniqueConstraint(fields=['branch', 'barcode'], condition=~Q(barcode=''), name='hinv_item_barcode_unique'),
        ]

    def __str__(self):
        return self.name

    @property
    def effective_gst_rate(self):
        if self.gst_rate is not None:
            return self.gst_rate
        cat = self.category
        while cat is not None:
            if cat.gst_rate is not None:
                return cat.gst_rate
            cat = cat.parent
        return None

    @property
    def effective_hsn(self):
        if self.hsn_code:
            return self.hsn_code
        cat = self.category
        while cat is not None:
            if cat.hsn_code:
                return cat.hsn_code
            cat = cat.parent
        return ''

    def on_hand(self):
        return self.levels.aggregate(s=Sum('quantity'))['s'] or D0

    def unit_choices(self):
        """[(name, factor)] — the base unit first, then the pack sizes it is bought or issued in."""
        return [(self.unit, Decimal('1'))] + [(u.name, u.factor) for u in self.units.all()]


class ItemUnit(models.Model):
    """A pack size: 1 ``name`` = ``factor`` base units (1 case = 500 pc, 1 can = 5 L)."""
    item = models.ForeignKey(StoreItem, on_delete=models.CASCADE, related_name='units')
    name = models.CharField(max_length=20)
    factor = models.DecimalField(max_digits=12, decimal_places=4)
    is_purchase_default = models.BooleanField(default=False)

    class Meta:
        ordering = ['factor']
        constraints = [models.UniqueConstraint(fields=['item', 'name'], name='hinv_unit_unique')]

    def __str__(self):
        return f"1 {self.name} = {self.factor.normalize():f} {self.item.unit}"


class Location(models.Model):
    class Kind(models.TextChoices):
        STORE = 'STORE', 'Store / warehouse'
        PANTRY = 'PANTRY', 'Floor pantry'
        CART = 'CART', 'Housekeeping cart'
        DEPARTMENT = 'DEPARTMENT', 'Department'
        MINIBAR = 'MINIBAR', 'Room minibar'
        LINEN_CLEAN = 'LINEN_CLEAN', 'Linen — clean store'
        LINEN_IN_USE = 'LINEN_IN_USE', 'Linen — in rooms'
        LINEN_SOILED = 'LINEN_SOILED', 'Linen — soiled'
        LINEN_LAUNDRY = 'LINEN_LAUNDRY', 'Linen — at laundry'
        SCRAP = 'SCRAP', 'Scrap / retired'

    class Department(models.TextChoices):
        HOUSEKEEPING = 'HOUSEKEEPING', 'Housekeeping'
        FNB = 'FNB', 'Food & beverage'
        KITCHEN = 'KITCHEN', 'Kitchen'
        ENGINEERING = 'ENGINEERING', 'Engineering'
        FRONT_OFFICE = 'FRONT_OFFICE', 'Front office'
        SPA = 'SPA', 'Spa'
        ADMIN = 'ADMIN', 'Admin & accounts'

    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='store_locations')
    parent = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='children')
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=14, choices=Kind.choices, default=Kind.STORE)
    department = models.CharField(max_length=14, choices=Department.choices, blank=True)
    room = models.ForeignKey('hotel.Room', on_delete=models.SET_NULL, null=True, blank=True, related_name='store_locations')
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'name']
        constraints = [models.UniqueConstraint(fields=['branch', 'parent', 'name'], name='hinv_location_unique')]

    def __str__(self):
        return self.path

    @property
    def path(self):
        parts, node, seen = [], self, set()
        while node is not None and node.pk not in seen:
            seen.add(node.pk)
            parts.append(node.name)
            node = node.parent
        return ' › '.join(reversed(parts))

    def descendant_ids(self):
        """This location and everything under it (Main Store includes its pantries and carts)."""
        ids, frontier = {self.pk}, [self.pk]
        while frontier:
            frontier = list(Location.objects.filter(parent_id__in=frontier).exclude(pk__in=ids).values_list('pk', flat=True))
            ids.update(frontier)
        return ids


class StockLevel(models.Model):
    """What is on the shelf at one location, and the levels it should be kept at."""
    item = models.ForeignKey(StoreItem, on_delete=models.CASCADE, related_name='levels')
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name='levels')
    quantity = models.DecimalField(max_digits=14, decimal_places=3, default=D0)
    min_level = models.DecimalField(max_digits=14, decimal_places=3, default=D0, help_text="Alert at or below; 0 = off")
    par_level = models.DecimalField(max_digits=14, decimal_places=3, default=D0, help_text="Top up to this")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['location__name']
        constraints = [models.UniqueConstraint(fields=['item', 'location'], name='hinv_level_unique')]

    def __str__(self):
        return f"{self.item} @ {self.location}: {self.quantity}"

    @property
    def is_low(self):
        return self.min_level > 0 and self.quantity <= self.min_level

    @property
    def shortfall(self):
        return max((self.par_level or self.min_level) - self.quantity, D0)


class StockMove(models.Model):
    """One line of the stock ledger. Append-only: saving an existing row or deleting one raises."""
    class Type(models.TextChoices):
        OPENING = 'OPENING', 'Opening stock'
        RECEIPT = 'RECEIPT', 'Goods received'
        RETURN_OUT = 'RETURN_OUT', 'Returned to vendor'
        TRANSFER = 'TRANSFER', 'Transfer'
        ISSUE = 'ISSUE', 'Issued to department'
        CONSUME = 'CONSUME', 'Consumed'
        WASTAGE = 'WASTAGE', 'Wastage / expired'
        BREAKAGE = 'BREAKAGE', 'Breakage / damage'
        ADJUST = 'ADJUST', 'Adjustment'
        COUNT = 'COUNT', 'Stock count correction'
        MINIBAR = 'MINIBAR', 'Minibar sale'
        LINEN_OUT = 'LINEN_OUT', 'Linen sent to laundry'
        LINEN_IN = 'LINEN_IN', 'Linen back from laundry'
        RETIRE = 'RETIRE', 'Retired from use'
        REVERSAL = 'REVERSAL', 'Reversal'
        COST_ADJ = 'COST_ADJ', 'Cost correction (purchase bill)'

    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='store_moves')
    item = models.ForeignKey(StoreItem, on_delete=models.PROTECT, related_name='moves')
    from_location = models.ForeignKey(Location, on_delete=models.PROTECT, null=True, blank=True, related_name='moves_out')
    to_location = models.ForeignKey(Location, on_delete=models.PROTECT, null=True, blank=True, related_name='moves_in')
    move_type = models.CharField(max_length=12, choices=Type.choices, db_index=True)
    quantity = models.DecimalField(max_digits=14, decimal_places=3, help_text="Always positive, in the base unit")
    entered_qty = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    entered_unit = models.CharField(max_length=20, blank=True, help_text="Unit the user typed (case, can…)")
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4, default=D0)
    value = models.DecimalField(max_digits=14, decimal_places=2, default=D0)
    from_balance = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True, help_text="Left at the source after this")
    to_balance = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True, help_text="At the destination after this")
    wac_after = models.DecimalField(max_digits=14, decimal_places=4, default=D0)
    reason = models.CharField(max_length=40, blank=True)
    note = models.CharField(max_length=255, blank=True)
    ref_type = models.CharField(max_length=20, blank=True, help_text="grn, requisition, count, folio_charge, manifest…")
    ref_id = models.PositiveBigIntegerField(null=True, blank=True)
    ref_label = models.CharField(max_length=60, blank=True)
    reverses = models.OneToOneField('self', on_delete=models.PROTECT, null=True, blank=True, related_name='reversed_by')
    performed_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['branch', 'item', 'created_at']), models.Index(fields=['ref_type', 'ref_id'])]

    def __str__(self):
        return f"{self.get_move_type_display()} {self.quantity} {self.item.unit} {self.item}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError('Stock moves cannot be changed — post a reversal instead.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError('Stock moves cannot be deleted — post a reversal instead.')

    @property
    def direction(self):
        if self.from_location_id and self.to_location_id:
            return 'transfer'
        if not self.from_location_id and not self.to_location_id:
            return 'value'  # a cost correction: money changes, quantity does not
        return 'in' if self.to_location_id else 'out'


# ============================================================ Procurement

class PurchaseOrder(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        APPROVED = 'APPROVED', 'Approved'
        PARTIALLY_RECEIVED = 'PARTIALLY_RECEIVED', 'Partially received'
        RECEIVED = 'RECEIVED', 'Received'
        CLOSED = 'CLOSED', 'Closed'
        CANCELLED = 'CANCELLED', 'Cancelled'

    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='store_pos')
    po_number = models.CharField(max_length=50, db_index=True)
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name='purchase_orders')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    expected_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=D0)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    approved_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [models.UniqueConstraint(fields=['branch', 'po_number'], name='hinv_po_number_unique')]

    def __str__(self):
        return f"{self.po_number} · {self.vendor.name}"

    def recalculate_totals(self):
        tot = sum((line.total_amount for line in self.lines.all()), D0)
        self.total_amount = tot
        self.save(update_fields=['total_amount', 'updated_at'])

    def update_received_status(self):
        lines = list(self.lines.all())
        if not lines:
            return
        all_rec = all(l.is_fully_received for l in lines)
        any_rec = any(l.received_qty > 0 for l in lines)
        if all_rec:
            self.status = PurchaseOrder.Status.RECEIVED
        elif any_rec:
            self.status = PurchaseOrder.Status.PARTIALLY_RECEIVED
        self.save(update_fields=['status', 'updated_at'])


class POLine(models.Model):
    po = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='lines')
    item = models.ForeignKey(StoreItem, on_delete=models.PROTECT, related_name='po_lines')
    unit_name = models.CharField(max_length=20, blank=True)
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit_price = models.DecimalField(max_digits=14, decimal_places=4)
    tax_percent = models.DecimalField(max_digits=5, decimal_places=2, default=D0)
    received_qty = models.DecimalField(max_digits=14, decimal_places=3, default=D0)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=D0)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"{self.quantity} {self.unit_name or self.item.unit} {self.item.name}"

    @property
    def pending_qty(self):
        return max(self.quantity - self.received_qty, D0)

    @property
    def is_fully_received(self):
        return self.received_qty >= self.quantity


class GoodsReceipt(models.Model):
    class Status(models.TextChoices):
        RECEIVED = 'RECEIVED', 'Received'
        CANCELLED = 'CANCELLED', 'Cancelled'

    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='store_grns')
    grn_number = models.CharField(max_length=50, db_index=True)
    po = models.ForeignKey(PurchaseOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name='grns')
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name='grns')
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='grns')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RECEIVED)
    invoice_number = models.CharField(max_length=60, blank=True)
    invoice_date = models.DateField(null=True, blank=True)
    is_itc_eligible = models.BooleanField(default=True, help_text="Input Tax Credit eligible")
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=D0)
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=D0)
    cgst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=D0)
    sgst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=D0)
    igst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=D0)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=D0)
    notes = models.TextField(blank=True)
    bill = models.ForeignKey('PurchaseBill', on_delete=models.SET_NULL, null=True, blank=True, related_name='grns',
                             help_text="The vendor's bill this delivery was billed on; until then the GRN value is a provisional payable")
    received_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [models.UniqueConstraint(fields=['branch', 'grn_number'], name='hinv_grn_number_unique')]

    def __str__(self):
        return f"{self.grn_number} · {self.vendor.name}"

    @property
    def is_billed(self):
        return self.bill_id is not None

    @property
    def paid_amount(self):
        return self.payments.aggregate(s=Sum('amount'))['s'] or D0

    @property
    def due_amount(self):
        if self.bill_id:  # what is owed now sits on the bill
            return D0
        return max(self.total_amount - self.paid_amount, D0)

    @property
    def is_paid(self):
        if self.bill_id:
            return self.bill.is_paid
        return self.due_amount <= D0


class GRNLine(models.Model):
    grn = models.ForeignKey(GoodsReceipt, on_delete=models.CASCADE, related_name='lines')
    po_line = models.ForeignKey(POLine, on_delete=models.SET_NULL, null=True, blank=True, related_name='grn_lines')
    item = models.ForeignKey(StoreItem, on_delete=models.PROTECT, related_name='grn_lines')
    unit_name = models.CharField(max_length=20, blank=True)
    received_qty = models.DecimalField(max_digits=14, decimal_places=3)
    rejected_qty = models.DecimalField(max_digits=14, decimal_places=3, default=D0)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4)
    tax_percent = models.DecimalField(max_digits=5, decimal_places=2, default=D0)
    batch_number = models.CharField(max_length=60, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=D0)
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=D0)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=D0)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"{self.received_qty} {self.unit_name or self.item.unit} {self.item.name}"


class VendorPayment(models.Model):
    class Method(models.TextChoices):
        BANK = 'BANK', 'Bank transfer'
        UPI = 'UPI', 'UPI'
        CHEQUE = 'CHEQUE', 'Cheque'
        CASH = 'CASH', 'Cash'

    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='store_vendor_payments')
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name='payments')
    grn = models.ForeignKey(GoodsReceipt, on_delete=models.SET_NULL, null=True, blank=True, related_name='payments')
    bill = models.ForeignKey('PurchaseBill', on_delete=models.SET_NULL, null=True, blank=True, related_name='payments')
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    payment_date = models.DateField()
    payment_method = models.CharField(max_length=20, choices=Method.choices, default=Method.BANK)
    reference = models.CharField(max_length=80, blank=True, help_text="UTR / Cheque / Receipt no.")
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-payment_date', '-id']

    def __str__(self):
        return f"₹{self.amount} to {self.vendor.name} on {self.payment_date}"


class PurchaseBill(models.Model):
    """The vendor's tax invoice, entered against the goods already received (GRNs) or, for a small
    direct purchase, together with them. It fixes the real cost (bill rate, delivery charges,
    discount, GST that cannot be claimed) and becomes what the hotel owes the vendor."""
    class Status(models.TextChoices):
        POSTED = 'POSTED', 'Posted'
        CANCELLED = 'CANCELLED', 'Cancelled'

    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='store_bills')
    bill_number = models.CharField(max_length=50, db_index=True, help_text="Our reference (PB-YYYYMM-0001)")
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name='bills')
    invoice_number = models.CharField(max_length=60, help_text="Number printed on the vendor's bill")
    invoice_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.POSTED, db_index=True)
    is_direct = models.BooleanField(default=False, help_text="Goods were received on this bill (no earlier GRN)")
    is_itc_eligible = models.BooleanField(default=True, help_text="Off: GST is not claimed back, so it is part of the cost")
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=D0, help_text="Taxable value of the items")
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=D0)
    cgst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=D0)
    sgst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=D0)
    igst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=D0)
    other_charges = models.DecimalField(max_digits=12, decimal_places=2, default=D0, help_text="Delivery, loading, packing")
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=D0)
    round_off = models.DecimalField(max_digits=8, decimal_places=2, default=D0)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=D0)
    cost_difference = models.DecimalField(max_digits=14, decimal_places=2, default=D0,
                                          help_text="Bill cost minus what the GRNs had put into stock")
    notes = models.TextField(blank=True)
    attachment = models.FileField(upload_to='hotel_inventory/bills/%Y/%m/', blank=True)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    cancelled_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['-invoice_date', '-id']
        constraints = [
            models.UniqueConstraint(fields=['branch', 'bill_number'], name='hinv_bill_number_unique'),
            # the same vendor bill cannot be entered twice (a cancelled entry can be re-entered)
            models.UniqueConstraint(fields=['branch', 'vendor', 'invoice_number'], condition=~Q(status='CANCELLED'),
                                    name='hinv_bill_vendor_invoice_unique'),
        ]

    def __str__(self):
        return f"{self.bill_number} · {self.vendor.name} #{self.invoice_number}"

    @property
    def paid_amount(self):
        return self.payments.aggregate(s=Sum('amount'))['s'] or D0

    @property
    def due_amount(self):
        if self.status == self.Status.CANCELLED:
            return D0
        return max(self.total_amount - self.paid_amount, D0)

    @property
    def is_paid(self):
        return self.due_amount <= D0

    @property
    def is_overdue(self):
        from django.utils import timezone
        return bool(self.due_date and self.due_amount > 0 and self.due_date < timezone.localdate())


class PurchaseBillLine(models.Model):
    bill = models.ForeignKey(PurchaseBill, on_delete=models.CASCADE, related_name='lines')
    # A delivery line is billed once at a time (GoodsReceipt.bill guards that); a cancelled bill keeps
    # its lines for the record, so the same GRN line can appear on a later bill.
    grn_line = models.ForeignKey(GRNLine, on_delete=models.PROTECT, null=True, blank=True, related_name='bill_lines')
    item = models.ForeignKey(StoreItem, on_delete=models.PROTECT, related_name='bill_lines')
    unit_name = models.CharField(max_length=20, blank=True)
    quantity = models.DecimalField(max_digits=14, decimal_places=3, help_text="In unit_name")
    rate = models.DecimalField(max_digits=14, decimal_places=4, help_text="Bill rate per unit_name, before GST")
    tax_percent = models.DecimalField(max_digits=5, decimal_places=2, default=D0)
    taxable_amount = models.DecimalField(max_digits=14, decimal_places=2, default=D0)
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=D0)
    charges_share = models.DecimalField(max_digits=14, decimal_places=2, default=D0, help_text="Delivery less discount, spread by value")
    landed_cost = models.DecimalField(max_digits=14, decimal_places=2, default=D0, help_text="What this line really cost the hotel")
    grn_value = models.DecimalField(max_digits=14, decimal_places=2, default=D0, help_text="What the GRN had put into stock")

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"{self.quantity} {self.unit_name or self.item.unit} {self.item.name} @ {self.rate}"

    @property
    def cost_difference(self):
        return self.landed_cost - self.grn_value


# ============================================================ Internal Movement

class Requisition(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        SUBMITTED = 'SUBMITTED', 'Submitted'
        APPROVED = 'APPROVED', 'Approved'
        PARTIALLY_ISSUED = 'PARTIALLY_ISSUED', 'Partially issued'
        ISSUED = 'ISSUED', 'Issued'
        REJECTED = 'REJECTED', 'Rejected'

    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='store_requisitions')
    req_number = models.CharField(max_length=50, db_index=True)
    department = models.CharField(max_length=20, choices=Location.Department.choices)
    from_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='requisitions_from')
    to_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='requisitions_to')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    requested_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    approved_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    approved_at = models.DateTimeField(null=True, blank=True)
    issued_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    issued_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [models.UniqueConstraint(fields=['branch', 'req_number'], name='hinv_req_number_unique')]

    def __str__(self):
        return f"{self.req_number} ({self.get_department_display()})"


class RequisitionLine(models.Model):
    requisition = models.ForeignKey(Requisition, on_delete=models.CASCADE, related_name='lines')
    item = models.ForeignKey(StoreItem, on_delete=models.PROTECT, related_name='requisition_lines')
    unit_name = models.CharField(max_length=20, blank=True)
    requested_qty = models.DecimalField(max_digits=14, decimal_places=3)
    approved_qty = models.DecimalField(max_digits=14, decimal_places=3, default=D0)
    issued_qty = models.DecimalField(max_digits=14, decimal_places=3, default=D0)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"{self.requested_qty} {self.unit_name or self.item.unit} {self.item.name}"


class StockAdjustment(models.Model):
    class Reason(models.TextChoices):
        WASTAGE = 'WASTAGE', 'Wastage / Expired'
        BREAKAGE = 'BREAKAGE', 'Breakage / Damage'
        THEFT = 'THEFT', 'Loss / Theft'
        CORRECTION = 'CORRECTION', 'Correction'
        SCRAP = 'SCRAP', 'Scrapped / Retired'
        OTHER = 'OTHER', 'Other'

    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='store_adjustments')
    adjustment_number = models.CharField(max_length=50, db_index=True)
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='adjustments')
    item = models.ForeignKey(StoreItem, on_delete=models.PROTECT, related_name='adjustments')
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit_name = models.CharField(max_length=20, blank=True)
    base_quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4, default=D0)
    value = models.DecimalField(max_digits=14, decimal_places=2, default=D0)
    reason = models.CharField(max_length=20, choices=Reason.choices, default=Reason.WASTAGE)
    room = models.ForeignKey('hotel.Room', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    notes = models.TextField(blank=True)
    photo = models.ImageField(upload_to='hotel_inventory/adjustments/', null=True, blank=True)
    expense = models.ForeignKey('finance.Expense', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    move = models.ForeignKey(StockMove, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [models.UniqueConstraint(fields=['branch', 'adjustment_number'], name='hinv_adj_number_unique')]

    def __str__(self):
        return f"{self.adjustment_number} · {self.get_reason_display()} · {self.item.name}"


class StockCount(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        COMPLETED = 'COMPLETED', 'Completed & Posted'
        CANCELLED = 'CANCELLED', 'Cancelled'

    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='store_counts')
    count_number = models.CharField(max_length=50, db_index=True)
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name='stock_counts')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    counted_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    counted_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    total_variance_value = models.DecimalField(max_digits=14, decimal_places=2, default=D0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [models.UniqueConstraint(fields=['branch', 'count_number'], name='hinv_count_number_unique')]

    def __str__(self):
        return f"{self.count_number} · {self.location.path}"


class StockCountLine(models.Model):
    count = models.ForeignKey(StockCount, on_delete=models.CASCADE, related_name='lines')
    item = models.ForeignKey(StoreItem, on_delete=models.PROTECT, related_name='count_lines')
    expected_qty = models.DecimalField(max_digits=14, decimal_places=3, default=D0)
    counted_qty = models.DecimalField(max_digits=14, decimal_places=3, default=D0)
    variance_qty = models.DecimalField(max_digits=14, decimal_places=3, default=D0)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4, default=D0)
    variance_value = models.DecimalField(max_digits=14, decimal_places=2, default=D0)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"{self.item.name}: expected {self.expected_qty}, counted {self.counted_qty}"


# ============================================================ Hotel-Specific (Minibar & Linen)

class MinibarProfile(models.Model):
    """Standard par set for minibars in a room category."""
    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='minibar_profiles')
    room_category = models.ForeignKey('hotel.RoomCategory', on_delete=models.CASCADE, related_name='minibar_profiles')
    item = models.ForeignKey(StoreItem, on_delete=models.CASCADE, related_name='minibar_profiles')
    par_quantity = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal('1'))
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['room_category__name', 'item__name']
        constraints = [models.UniqueConstraint(fields=['room_category', 'item'], name='hinv_minibar_profile_unique')]

    def __str__(self):
        return f"{self.room_category.name} · {self.item.name} ({self.par_quantity} {self.item.unit})"


class LinenPar(models.Model):
    """Standard circulating linen quantity par per room category."""
    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='linen_pars')
    room_category = models.ForeignKey('hotel.RoomCategory', on_delete=models.CASCADE, related_name='linen_pars')
    item = models.ForeignKey(StoreItem, on_delete=models.CASCADE, related_name='linen_pars')
    par_per_room = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal('1'))
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['room_category__name', 'item__name']
        constraints = [models.UniqueConstraint(fields=['room_category', 'item'], name='hinv_linen_par_unique')]

    def __str__(self):
        return f"{self.room_category.name} · {self.item.name} ({self.par_per_room}/room)"


class LaundryManifest(models.Model):
    class ManifestType(models.TextChoices):
        OUTBOUND = 'OUTBOUND', 'Outbound (Soiled → Laundry)'
        INBOUND = 'INBOUND', 'Inbound (Laundry → Clean)'

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        SENT = 'SENT', 'Sent'
        RECEIVED = 'RECEIVED', 'Received'
        RECONCILED = 'RECONCILED', 'Reconciled'
        CANCELLED = 'CANCELLED', 'Cancelled'

    branch = models.ForeignKey(BRANCH, on_delete=models.CASCADE, related_name='laundry_manifests')
    manifest_number = models.CharField(max_length=50, db_index=True)
    manifest_type = models.CharField(max_length=12, choices=ManifestType.choices, default=ManifestType.OUTBOUND)
    vendor = models.ForeignKey(Vendor, on_delete=models.SET_NULL, null=True, blank=True, related_name='laundry_manifests')
    related_outbound = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='inbound_returns')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    date = models.DateField()
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-id']
        constraints = [models.UniqueConstraint(fields=['branch', 'manifest_number'], name='hinv_manifest_number_unique')]

    def __str__(self):
        return f"{self.manifest_number} ({self.get_manifest_type_display()})"


class LaundryManifestLine(models.Model):
    manifest = models.ForeignKey(LaundryManifest, on_delete=models.CASCADE, related_name='lines')
    item = models.ForeignKey(StoreItem, on_delete=models.PROTECT, related_name='laundry_manifest_lines')
    sent_qty = models.DecimalField(max_digits=14, decimal_places=3, default=D0)
    returned_qty = models.DecimalField(max_digits=14, decimal_places=3, default=D0)
    damaged_qty = models.DecimalField(max_digits=14, decimal_places=3, default=D0)
    missing_qty = models.DecimalField(max_digits=14, decimal_places=3, default=D0)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4, default=D0)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"{self.item.name} (Sent: {self.sent_qty}, Ret: {self.returned_qty})"

