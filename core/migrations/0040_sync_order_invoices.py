from django.db import migrations

def sync_order_invoices(apps, schema_editor):
    Order = apps.get_model('core', 'Order')
    for order in Order.objects.all():
        try:
            items = list(order.items.select_related('menu_item', 'menu_item__category').all())
            if not items:
                continue

            has_food = False
            has_alcohol = False
            alcohol_keywords = {'bar_stock', 'bar stock', 'liquor', 'beer', 'wine', 'bar', 'spirits', 'cocktail', 'cocktails'}

            for item in items:
                mi = item.menu_item
                if not mi:
                    has_food = True
                    continue

                tax_type = (getattr(mi, 'tax_type', '') or '').upper()
                cat_name = (mi.category.name if getattr(mi, 'category', None) else '').lower()
                cat_slug = (mi.category.slug if getattr(mi, 'category', None) else '').lower()

                is_alc = (
                    tax_type.startswith('VAT_') or
                    cat_name in alcohol_keywords or
                    cat_slug in alcohol_keywords or
                    any(k in cat_name for k in ['beer', 'wine', 'liquor', 'whiskey', 'vodka', 'rum', 'bar stock'])
                )

                if is_alc:
                    has_alcohol = True
                else:
                    has_food = True

            num = order.branch_order_number if order.branch_order_number is not None else order.id
            formatted_num = f"{num:04d}" if num else ""
            current_year = order.order_time.year if order.order_time else 2026

            changed = False
            if has_food:
                if not order.gstin_no and formatted_num:
                    order.gstin_no = f"INV-{current_year}-{formatted_num}"
                    changed = True
            else:
                if order.gstin_no is not None:
                    order.gstin_no = None
                    changed = True

            if has_alcohol:
                if not order.excise_bill_no and formatted_num:
                    order.excise_bill_no = f"EXC-{current_year}-{formatted_num}"
                    changed = True
            else:
                if order.excise_bill_no is not None:
                    order.excise_bill_no = None
                    changed = True

            if changed:
                order.save(update_fields=['gstin_no', 'excise_bill_no'])
        except Exception:
            pass

class Migration(migrations.Migration):

    dependencies = [
        ('core', '0039_inventoryitem_cost_price_inventoryitem_gst_rate_and_more'),
    ]

    operations = [
        migrations.RunPython(sync_order_invoices, reverse_code=migrations.RunPython.noop),
    ]
