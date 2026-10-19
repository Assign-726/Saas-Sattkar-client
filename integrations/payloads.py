"""Stable JSON shapes shared by the public API and webhooks. Change them only additively."""
from decimal import Decimal

ORDER_STATUS = {
    'DRAFT': 'draft', 'P': 'placed', 'PR': 'preparing', 'READY': 'ready',
    'S': 'served', 'COMPLETED': 'completed', 'C': 'cancelled',
}


def _money(v):
    return str(Decimal(v or 0).quantize(Decimal('0.01')))


def order(o):
    try:
        link = o.external_link
    except Exception:
        link = None
    return {
        'id': o.id,
        'number': str(o.order_number),
        'branch_id': o.branch_id,
        'status': ORDER_STATUS.get(o.status, o.status),
        'order_type': o.order_type,
        'source': link.source if link else 'pos',
        'external_id': link.external_id if link else None,
        'customer': {'name': o.customer_name, 'phone': o.phone},
        'items': [
            {'id': it.id, 'menu_item_id': it.menu_item_id, 'name': it.menu_item.name if it.menu_item_id else '',
             'sku': getattr(it.menu_item, 'sku', None) if it.menu_item_id else None,
             'quantity': it.quantity, 'unit_price': _money(it.unit_price), 'notes': it.notes}
            for it in o.items.select_related('menu_item').all()
        ],
        'subtotal': _money(o.subtotal), 'tax': _money(o.tax_amount), 'discount': _money(o.discount_amount),
        'total': _money(o.total_amount),
        'payment': {'status': o.payment_status, 'method': o.payment_method, 'paid': _money(o.paid_amount), 'due': _money(o.due_amount)},
        'notes': o.notes,
        'placed_at': o.order_time.isoformat() if o.order_time else None,
    }


def booking(b):
    try:
        link = b.external_link
    except Exception:
        link = None
    return {
        'id': b.booking_id,
        'branch_id': b.branch_id,
        'status': (b.booking_status or '').lower(),
        'source': link.source if link else 'direct',
        'external_id': link.external_id if link else None,
        'room': {'number': b.room.room_number, 'category_id': b.room.category_id, 'category': b.room.category.name} if b.room_id else None,
        'guest': {'name': b.guest_name, 'email': b.guest_email, 'phone': b.guest_phone, 'count': b.num_guests},
        'check_in': b.check_in_date.isoformat() if b.check_in_date else None,
        'check_out': b.check_out_date.isoformat() if b.check_out_date else None,
        'nights': b.nights_count,
        'amount': {'room_charges': _money(b.total_room_charges), 'taxes': _money(b.taxes), 'total': _money(b.grand_total)},
        'payment_status': (b.payment_status or '').lower(),
        'special_requests': b.special_requests,
        'created_at': b.created_at.isoformat() if b.created_at else None,
    }


def menu_item(m):
    return {
        'id': m.id, 'name': m.name, 'sku': m.sku, 'short_code': m.short_code,
        'category_id': m.category_id, 'category': m.category.name if m.category_id else None,
        'price': _money(m.price), 'aggregator_price': _money(m.markup_price) if m.markup_price else None,
        'packaging_fee': _money(m.packaging_fee), 'available': bool(m.availability),
        'food_type': m.food_type, 'tax_inclusive': m.is_tax_inclusive, 'description': m.description,
        'channels': m.publishing_channels or [],
    }
