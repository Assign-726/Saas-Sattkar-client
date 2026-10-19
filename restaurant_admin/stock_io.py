"""Import / export for Stock & Supply Chain: stock items, stock categories, wastage, stock counts,
plus exports of inventory logs and delivery challans. Built on the reader/writer in menu_io.

Stock rules kept by import (so the books stay auditable):
- a new stock item's "Stock" is its opening stock, posted as a Restock movement;
- an existing item's stock is never overwritten by an import — that is what a stock count is for;
- wastage rows reduce stock and are logged exactly like Wastage → Log wastage;
- a stock-count import only fills in "Counted" on an open count; posting stays a separate step.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from core.models import InventoryCategory, InventoryItem, InventoryTransaction, StockCountLine
from restaurant_admin.menu_io import _list, _money, _yes_no  # noqa: F401 (shared value parsers)

CENT = Decimal('0.01')
UNITS = {'kg': 'kg', 'kgs': 'kg', 'kilogram': 'kg', 'kilograms': 'kg', 'gm': 'gm', 'g': 'gm', 'gram': 'gm', 'grams': 'gm',
         'gms': 'gm', 'litre': 'litre', 'liter': 'litre', 'l': 'litre', 'ltr': 'litre', 'litres': 'litre', 'ml': 'ml',
         'millilitre': 'ml', 'piece': 'piece', 'pieces': 'piece', 'pc': 'piece', 'pcs': 'piece', 'nos': 'piece',
         'no': 'piece', 'unit': 'piece', 'pack': 'pack', 'packet': 'pack', 'pkt': 'pack', 'box': 'box', 'crate': 'crate',
         'bottle': 'bottle', 'btl': 'bottle'}
UNIT_LABELS = ['kg', 'gm', 'litre', 'ml', 'piece', 'pack', 'box', 'crate', 'bottle']
GST_RATES = [Decimal(x) for x in ('0', '0.25', '3', '5', '12', '18', '28')]
TAXABILITY = {k: str(v) for k, v in InventoryItem.TaxabilityType.choices}
REASONS = {k: str(v) for k, v in InventoryTransaction.WastageReason.choices}

ITEM_COLUMNS = [
    ('SKU', 'sku', 'Your item code. Used to match existing items; leave blank to match by name.'),
    ('Name *', 'name', 'Stock item name. Required.'),
    ('Category', 'category', 'Stock category. Use “Parent > Child” for a subcategory. Created if missing.'),
    ('Unit *', 'unit', 'kg, gm, litre, ml, piece, pack, box, crate or bottle. Required for new items.'),
    ('Stock', 'quantity', 'Opening stock for NEW items (whole numbers). Existing stock is not changed — use a stock count.'),
    ('Reorder level', 'reorder_level', 'Low-stock alert below this quantity.'),
    ('Cost price (₹)', 'cost_price', 'Purchase cost per unit.'),
    ('Selling price (₹)', 'selling_price', 'Optional.'),
    ('GST %', 'gst_rate', 'Item-specific GST: 0, 0.25, 3, 5, 12, 18 or 28. Blank = use the category’s.'),
    ('HSN/SAC', 'hsn_sac_code', 'Item-specific HSN/SAC. Blank = use the category’s.'),
    ('Taxability', 'taxability_type', 'Taxable, Exempted, Nil Rated or Non-GST. Blank = use the category’s.'),
    ('Active', 'is_active', 'Yes / No.'),
    ('HSN description', 'hsn_description', 'Optional.'),
    ('GST used', 'effective_gst', 'Export only: the rate applied after category inheritance.'),
    ('Stock value (₹)', 'value', 'Export only.'),
]
CATEGORY_COLUMNS = [
    ('Category *', 'name', 'Category name. Required.'),
    ('Parent category', 'parent', 'Blank for a top-level category. A missing parent is created.'),
    ('GST %', 'gst_rate', 'Category GST: 0, 0.25, 3, 5, 12, 18 or 28. Blank = inherit from the parent / company.'),
    ('HSN/SAC', 'hsn_sac_code', 'Blank = inherit.'),
    ('Taxability', 'taxability_type', 'Taxable, Exempted, Nil Rated or Non-GST. Blank = inherit.'),
    ('Items', 'items', 'Export only.'),
]
WASTAGE_COLUMNS = [
    ('SKU', 'sku', 'Matches the stock item by SKU; or leave blank and give the item name.'),
    ('Item *', 'name', 'Stock item name.'),
    ('Quantity *', 'quantity', 'Whole units wasted (in the item’s unit).'),
    ('Reason *', 'reason', ', '.join(REASONS.values()) + '.'),
    ('Note', 'note', 'Optional.'),
]
COUNT_COLUMNS = [
    ('SKU', 'sku', 'Do not change — used to match the line.'),
    ('Item *', 'name', 'Do not change — used to match the line.'),
    ('Category', 'category', 'For reference.'),
    ('Unit', 'unit', 'For reference.'),
    ('Book', 'expected', 'Quantity in the books when the count started (for reference).'),
    ('Counted', 'counted', 'Enter what is on the shelf. Leave blank for items not counted yet.'),
    ('Variance', 'variance', 'Export only.'),
    ('Unit cost (₹)', 'unit_cost', 'Export only.'),
    ('Variance value (₹)', 'variance_value', 'Export only.'),
]
LOG_HEADERS = ['When', 'Item', 'SKU', 'Unit', 'Movement', 'Quantity', 'Unit cost (₹)', 'Value (₹)', 'Reason', 'Note',
               'Order', 'Purchase', 'By']
CHALLAN_HEADERS = ['Challan no', 'Dispatch date', 'Status', 'From branch', 'To branch', 'Receiver', 'Receiver GSTIN',
                   'Receiver state', 'Reason', 'Transport', 'Vehicle no', 'Items', 'Taxable value (₹)', 'CGST (₹)',
                   'SGST (₹)', 'IGST (₹)', 'Total tax (₹)', 'Notes']
CHALLAN_LINE_HEADERS = ['Challan no', 'Dispatch date', 'Item', 'HSN', 'Quantity', 'Unit', 'Unit value (₹)', 'Line total (₹)']

TAXABILITY_LIST = list(TAXABILITY.values())
ITEM_LISTS = {'unit': UNIT_LABELS, 'is_active': ['Yes', 'No'], 'taxability_type': TAXABILITY_LIST,
              'gst_rate': ['0', '5', '12', '18', '28']}
CATEGORY_LISTS = {'taxability_type': TAXABILITY_LIST, 'gst_rate': ['0', '5', '12', '18', '28']}
WASTAGE_LISTS = {'reason': list(REASONS.values())}

ITEM_NOTE = ('Items match existing ones by SKU, else by name (ignoring case). Blank cells keep the current value. '
             'Stock is only set for new items (opening stock); correct existing stock with a stock count.')
CATEGORY_NOTE = 'Categories match by name and parent (ignoring case). Blank tax cells keep the current setting.'
WASTAGE_NOTE = 'Each row logs wastage and reduces stock, exactly like Wastage → Log wastage.'
COUNT_NOTE = 'Rows match the count lines by SKU, else by item name. Only “Counted” is read; the count is not posted.'


# ── shared parsing ─────────────────────────────────────────────────────────

def _whole(v, label, minimum=0):
    try:
        f = float(str(v).replace(',', '').strip())
    except ValueError:
        raise ValueError(f'{label} “{v}” is not a number')
    if f != int(f):
        raise ValueError(f'{label} must be a whole number (got {v})')
    if f < minimum:
        raise ValueError(f'{label} cannot be below {minimum}')
    return int(f)


def _unit(v):
    u = UNITS.get(str(v).strip().lower().rstrip('.'))
    if not u:
        raise ValueError(f'Unit “{v}” must be one of: ' + ', '.join(UNIT_LABELS))
    return u


def _gst(v):
    d = _money(str(v).replace('%', ''), 'GST %')
    if d not in GST_RATES:
        raise ValueError(f'GST % must be one of 0, 0.25, 3, 5, 12, 18, 28 (got {v})')
    return d


def _taxability(v):
    s = str(v).strip().lower().replace('-', ' ').replace('_', ' ')
    for code, label in TAXABILITY.items():
        if s in (code.replace('_', ' '), label.lower(), label.lower().replace('/', ' ').replace('  ', ' ')):
            return code
    if s.startswith('non'):
        return 'non_gst_excise'
    if s.startswith('nil'):
        return 'nil_rated'
    if s.startswith('exempt'):
        return 'exempted'
    raise ValueError(f'Taxability “{v}” must be Taxable, Exempted, Nil Rated or Non-GST')


def _reason(v):
    s = str(v).strip().lower()
    for code, label in REASONS.items():
        if s in (code.lower(), label.lower()) or label.lower().startswith(s) or s.replace(' ', '_') == code.lower():
            return code
    hits = [code for code, label in REASONS.items() if s and s in label.lower()]  # e.g. "trim", "expired"
    if len(hits) == 1:
        return hits[0]
    raise ValueError(f'Reason “{v}” must be one of: ' + ', '.join(REASONS.values()))


def items_scope(restaurant, branch):
    qs = InventoryItem.objects.filter(restaurant=restaurant)
    return qs.filter(branch=branch) if branch else qs


def categories_scope(restaurant, branch):
    qs = InventoryCategory.objects.filter(restaurant=restaurant)
    return qs.filter(branch=branch) if branch else qs


def _cat_key(name, parent=''):
    return ((parent or '').strip().lower(), name.strip().lower())


def _split_path(text):
    parts = [p.strip() for p in str(text).split('>') if p.strip()]
    if not parts:
        return '', ''
    return (parts[-2] if len(parts) > 1 else ''), parts[-1]


def _match_item(lookup_sku, lookup_name, sku, name):
    """(item, error) — SKU first, then name; a SKU and a name that point at two items is an error."""
    by_sku = lookup_sku.get(sku.lower()) if sku else None
    by_name = lookup_name.get(name.lower()) if name else None
    if by_sku and by_name and by_sku.pk != by_name.pk:
        return None, f'SKU {sku} belongs to “{by_sku.name}”, but another item is called “{name}”'
    return by_sku or by_name, None


# ── stock items ───────────────────────────────────────────────────────────

def plan_items(rows, restaurant, branch, mode='upsert'):
    items = list(items_scope(restaurant, branch).select_related('category', 'category__parent'))
    by_sku = {i.sku.strip().lower(): i for i in items if i.sku and i.sku.strip()}
    by_name = {i.name.strip().lower(): i for i in items}
    cats = {_cat_key(c.name, c.parent.name if c.parent else ''): c
            for c in categories_scope(restaurant, branch).select_related('parent')}
    seen_n, seen_s, new_cats, plan = {}, {}, [], []
    for rowno, r in rows:
        errs, warn, data = [], [], {}
        name = ' '.join(r.get('name', '').split())
        sku = r.get('sku', '').strip()
        entry = {'row': rowno, 'name': name or '—', 'category': r.get('category', ''), 'price': r.get('quantity', ''),
                 'action': 'error', 'messages': errs, 'warnings': warn}
        plan.append(entry)
        if not name:
            errs.append('Name is missing')
            continue
        if name.lower() in seen_n:
            errs.append(f'Same name as row {seen_n[name.lower()]}')
            continue
        seen_n[name.lower()] = rowno
        if sku:
            if sku.lower() in seen_s:
                errs.append(f'SKU {sku} is also used on row {seen_s[sku.lower()]}')
                continue
            seen_s[sku.lower()] = rowno
        existing, err = _match_item(by_sku, by_name, sku, name)
        if err:
            errs.append(err)
            continue
        if existing and sku and existing.sku and existing.sku.strip().lower() != sku.lower() and sku.lower() in by_sku:
            errs.append(f'SKU {sku} is already used by “{by_sku[sku.lower()].name}”')
            continue
        entry['existing_id'] = existing.pk if existing else None
        try:
            if 'unit' in r:
                data['unit'] = _unit(r['unit'])
            if 'reorder_level' in r:
                data['reorder_level'] = _whole(r['reorder_level'], 'Reorder level')
            for k, label in (('cost_price', 'Cost price'), ('selling_price', 'Selling price')):
                if k in r:
                    data[k] = _money(r[k], label)
            if 'cost_price' in data:
                data['price_per_unit'] = data['cost_price']
            if 'gst_rate' in r:
                data['gst_rate'] = _gst(r['gst_rate'])
            if 'taxability_type' in r:
                data['taxability_type'] = _taxability(r['taxability_type'])
            if 'is_active' in r:
                data['is_active'] = _yes_no(r['is_active'], 'Active')
            if 'quantity' in r:
                qty = _whole(r['quantity'], 'Stock')
                if existing:
                    if qty != (existing.quantity or 0):
                        warn.append(f'Stock not changed ({existing.quantity} in the books) — correct it with a stock count')
                else:
                    data['quantity'] = qty
        except ValueError as e:
            errs.append(str(e))
            continue
        for k in ('hsn_sac_code', 'hsn_description'):
            if k in r:
                data[k] = r[k]
        if sku:
            data['sku'] = sku
        if not existing or name != existing.name:
            data['name'] = name
        if any(k in data for k in ('gst_rate', 'hsn_sac_code', 'taxability_type')):
            data['tax_configuration'] = 'override'
        if 'category' in r:
            parent, cname = _split_path(r['category'])
            data['_category'] = (parent, cname)
            if _cat_key(cname, parent) not in cats:
                label = f'{parent} > {cname}' if parent else cname
                if label not in new_cats:
                    new_cats.append(label)
        if not existing:
            if 'unit' not in data:
                errs.append('Unit is missing')
                continue
            if mode == 'update':
                entry['action'] = 'skip'
                warn.append('Not in stock yet — skipped (import mode: only update existing)')
                continue
            entry['action'] = 'create'
            if data.get('quantity'):
                entry['changes'] = [f'Opening stock {data["quantity"]}']
        else:
            if mode == 'create':
                entry['action'] = 'skip'
                warn.append('Already exists — skipped (import mode: only add new)')
                continue
            changes = _item_changes(existing, data)
            entry['action'] = 'update' if changes else 'unchanged'
            entry['changes'] = changes
        entry['data'] = data
    return plan, new_cats


def _item_changes(item, data):
    labels = {c[1]: c[0].replace(' *', '') for c in ITEM_COLUMNS}
    out = []
    for k, v in data.items():
        if k == '_category':
            parent, cname = v
            cur = item.category
            if not cur or cur.name.lower() != cname.lower() or ((cur.parent.name.lower() if cur.parent else '') != parent.lower()):
                out.append('Category')
        elif k in ('price_per_unit', 'tax_configuration'):
            continue
        else:
            cur = getattr(item, k)
            same = (Decimal(str(cur or 0)) == Decimal(str(v or 0))) if isinstance(v, Decimal) else (cur or '') == (v or '') \
                if isinstance(v, str) else cur == v
            if not same:
                out.append(labels.get(k, k))
    return out


def _get_category(cache, restaurant, branch, parent_name, name):
    """Find or create a stock category (and its parent) by name."""
    parent = None
    if parent_name:
        key = _cat_key(parent_name)
        parent = cache.get(key)
        if not parent:
            parent = InventoryCategory.objects.create(restaurant=restaurant, branch=branch, name=parent_name)
            cache[key] = parent
    key = _cat_key(name, parent_name)
    if key not in cache:
        cache[key] = InventoryCategory.objects.create(restaurant=restaurant, branch=branch, name=name, parent=parent)
    return cache[key]


@transaction.atomic
def apply_items(plan, restaurant, branch, user=None):
    cache = {_cat_key(c.name, c.parent.name if c.parent else ''): c
             for c in categories_scope(restaurant, branch).select_related('parent')}
    before = len(cache)
    counts = {'create': 0, 'update': 0, 'categories': 0}
    for p in plan:
        if p['action'] not in ('create', 'update'):
            continue
        data = dict(p['data'])
        cat = data.pop('_category', None)
        if p['action'] == 'update':
            item = InventoryItem.objects.select_for_update().get(pk=p['existing_id'])
        else:
            item = InventoryItem(restaurant=restaurant, branch=branch, tax_configuration='inherit')
        for k, v in data.items():
            setattr(item, k, v)
        if cat:
            item.category = _get_category(cache, restaurant, branch, *cat)
        if item.cost_price and not item.price_per_unit:
            item.price_per_unit = item.cost_price
        item.save()
        if p['action'] == 'create' and item.quantity:
            InventoryTransaction.objects.create(inventory_item=item, transaction_type=InventoryTransaction.TransactionType.RESTOCK,
                                                delta=item.quantity, unit_cost=item.unit_cost, note='Opening stock (import)',
                                                performed_by=user)
        counts[p['action']] += 1
    counts['categories'] = len(cache) - before
    return counts


def item_export_rows(items):
    for i in items:
        tax_own = i.tax_configuration == 'override'
        eff = i.effective_gst_rate
        yield [i.sku or '', i.name, str(i.category) if i.category else '', i.unit or '', i.quantity or 0, i.reorder_level or 0,
               float(i.cost_price or i.price_per_unit or 0), float(i.selling_price) if i.selling_price is not None else '',
               float(i.gst_rate) if tax_own and i.gst_rate is not None else '', (i.hsn_sac_code or '') if tax_own else '',
               TAXABILITY.get(i.taxability_type, '') if tax_own and i.taxability_type else '', 'Yes' if i.is_active else 'No',
               i.hsn_description or '', float(eff) if eff is not None and eff != '' else '',
               float((Decimal(i.quantity or 0) * i.unit_cost).quantize(CENT))]


ITEM_SAMPLE = [
    ['RM-001', 'Basmati Rice', 'Raw Ingredients > Grains', 'kg', 50, 10, 95, '', '', '', '', 'Yes', ''],
    ['DRY-014', 'Paneer', 'Raw Ingredients > Dairy', 'kg', 8, 2, 380, '', 5, '0406', 'Taxable', 'Yes', 'Cottage cheese'],
]


# ── stock categories ──────────────────────────────────────────────────────

def plan_categories(rows, restaurant, branch, mode='upsert'):
    cats = {_cat_key(c.name, c.parent.name if c.parent else ''): c
            for c in categories_scope(restaurant, branch).select_related('parent')}
    seen, plan, will_exist = {}, [], set(cats)
    for rowno, r in rows:
        errs, warn, data = [], [], {}
        name = ' '.join(r.get('name', '').split())
        parent = ' '.join(r.get('parent', '').split())
        entry = {'row': rowno, 'name': (f'{parent} > {name}' if parent else name) or '—', 'category': parent,
                 'price': '', 'action': 'error', 'messages': errs, 'warnings': warn}
        plan.append(entry)
        if not name:
            errs.append('Category name is missing')
            continue
        if parent and parent.lower() == name.lower():
            errs.append('A category cannot be its own parent')
            continue
        key = _cat_key(name, parent)
        if key in seen:
            errs.append(f'Same category as row {seen[key]}')
            continue
        seen[key] = rowno
        try:
            if 'gst_rate' in r:
                data['gst_rate'] = _gst(r['gst_rate'])
            if 'taxability_type' in r:
                data['taxability_type'] = _taxability(r['taxability_type'])
        except ValueError as e:
            errs.append(str(e))
            continue
        if 'hsn_sac_code' in r:
            data['hsn_sac_code'] = r['hsn_sac_code']
        if parent and _cat_key(parent) not in will_exist:
            warn.append(f'Parent “{parent}” will be created')
        existing = cats.get(key)
        entry.update({'existing_id': existing.pk if existing else None, 'parent_name': parent, 'cat_name': name, 'data': data})
        if existing:
            if mode == 'create':
                entry['action'] = 'skip'
                warn.append('Already exists — skipped (import mode: only add new)')
                continue
            changes = []
            for k, v in data.items():
                cur = getattr(existing, k)
                if (Decimal(str(cur)) if cur is not None else None) != v if isinstance(v, Decimal) else (cur or '') != (v or ''):
                    changes.append({'gst_rate': 'GST %', 'hsn_sac_code': 'HSN/SAC', 'taxability_type': 'Taxability'}[k])
            if data and existing.tax_configuration != 'override' and not changes:
                changes.append('Tax set on this category')
            entry['action'] = 'update' if changes else 'unchanged'
            entry['changes'] = changes
        else:
            if mode == 'update':
                entry['action'] = 'skip'
                warn.append('Category does not exist — skipped (import mode: only update existing)')
                continue
            entry['action'] = 'create'
            will_exist.add(key)
    return plan


def _apply_cat_tax(cat, data):
    """Same rule as the category form: any tax value → override (mirrored to the default_* fields)."""
    for k, v in data.items():
        setattr(cat, k, v)
    if data:
        cat.tax_configuration = 'override'
        if cat.gst_rate is not None:
            cat.default_gst_rate = cat.gst_rate
        if cat.hsn_sac_code:
            cat.default_hsn_code = cat.hsn_sac_code
        if cat.taxability_type:
            cat.taxability = cat.taxability_type


@transaction.atomic
def apply_categories(plan, restaurant, branch, user=None):
    cache = {_cat_key(c.name, c.parent.name if c.parent else ''): c
             for c in categories_scope(restaurant, branch).select_related('parent')}
    counts = {'create': 0, 'update': 0}
    # parents before children, so a parent created by its own row gets that row's tax settings
    for p in sorted((p for p in plan if p['action'] in ('create', 'update')), key=lambda p: bool(p['parent_name'])):
        if p['action'] == 'update':
            cat = InventoryCategory.objects.select_for_update().get(pk=p['existing_id'])
        else:
            key = _cat_key(p['cat_name'], p['parent_name'])
            cat = cache.get(key)  # may already exist as the auto-created parent of an earlier row
            if not cat:
                cat = _get_category(cache, restaurant, branch, p['parent_name'], p['cat_name'])
        _apply_cat_tax(cat, p['data'])
        cat.save()
        counts[p['action']] += 1
    return counts


def category_export_rows(restaurant, branch):
    cats = (categories_scope(restaurant, branch).select_related('parent').annotate(n=Count('items', distinct=True))
            .order_by('parent__name', 'name'))
    for c in sorted(cats, key=lambda c: ((c.parent.name if c.parent else c.name).lower(), bool(c.parent), c.name.lower())):
        own = c.tax_configuration == 'override'
        yield [c.name, c.parent.name if c.parent else '',
               float(c.gst_rate) if own and c.gst_rate is not None else '', (c.hsn_sac_code or '') if own else '',
               TAXABILITY.get(c.taxability_type, '') if own and c.taxability_type else '', c.n]


CATEGORY_SAMPLE = [['Raw Ingredients', '', '', '', '', ''], ['Dairy', 'Raw Ingredients', 5, '0406', 'Taxable', '']]


# ── wastage ───────────────────────────────────────────────────────────────

def plan_wastage(rows, restaurant, branch, mode='upsert'):
    items = list(items_scope(restaurant, branch))
    by_sku = {i.sku.strip().lower(): i for i in items if i.sku and i.sku.strip()}
    by_name = {i.name.strip().lower(): i for i in items}
    left, plan = {i.pk: i.quantity or 0 for i in items}, []
    for rowno, r in rows:
        errs, warn = [], []
        name = ' '.join(r.get('name', '').split())
        sku = r.get('sku', '').strip()
        entry = {'row': rowno, 'name': name or sku or '—', 'category': r.get('reason', ''), 'price': r.get('quantity', ''),
                 'action': 'error', 'messages': errs, 'warnings': warn}
        plan.append(entry)
        if not name and not sku:
            errs.append('Item is missing')
            continue
        item, err = _match_item(by_sku, by_name, sku, name)
        if err or not item:
            errs.append(err or f'No stock item called “{name or sku}”')
            continue
        try:
            qty = _whole(r.get('quantity', ''), 'Quantity', minimum=1) if r.get('quantity') else None
            reason = _reason(r['reason']) if r.get('reason') else None
        except ValueError as e:
            errs.append(str(e))
            continue
        if qty is None:
            errs.append('Quantity is missing')
        if reason is None:
            errs.append('Reason is missing')
        if errs:
            continue
        left[item.pk] -= qty
        if left[item.pk] < 0:
            warn.append('More than is in stock — stock will go below zero; run a stock count')
        value = (Decimal(qty) * item.unit_cost).quantize(CENT)
        entry.update({'action': 'create', 'name': item.name, 'category': REASONS[reason],
                      'price': f'{qty} {item.unit or ""}'.strip(), 'changes': [f'₹{value} written off'],
                      'data': {'item_id': item.pk, 'qty': qty, 'reason': reason, 'note': r.get('note', '')[:255]}})
    return plan


@transaction.atomic
def apply_wastage(plan, restaurant, branch, user=None):
    n = 0
    for p in plan:
        if p['action'] != 'create':
            continue
        d = p['data']
        item = InventoryItem.objects.select_for_update().get(pk=d['item_id'])
        item.adjust(-d['qty'])
        InventoryTransaction.objects.create(inventory_item=item, transaction_type='W', delta=-d['qty'], reason=d['reason'],
                                            note=d['note'], unit_cost=item.unit_cost, performed_by=user)
        n += 1
    return {'create': n, 'update': 0}


def wastage_export_rows(logs):
    for t in logs:
        cost = t.unit_cost if t.unit_cost is not None else t.inventory_item.unit_cost
        yield [t.inventory_item.sku or '', t.inventory_item.name, -t.delta, REASONS.get(t.reason, ''), t.note or '',
               timezone.localtime(t.created_at).strftime('%Y-%m-%d %H:%M'), t.inventory_item.unit or '',
               float((Decimal(-t.delta) * cost).quantize(CENT)), t.performed_by.username if t.performed_by else '']


WASTAGE_EXPORT_COLUMNS = WASTAGE_COLUMNS + [('When', 'when', 'Export only.'), ('Unit', 'unit', 'Export only.'),
                                            ('Value (₹)', 'value', 'Export only.'), ('By', 'by', 'Export only.')]
WASTAGE_SAMPLE = [['RM-001', 'Basmati Rice', 2, 'Spoiled / expired', 'Weevils in the sack'],
                  ['', 'Paneer', 1, 'Prep / trim waste', '']]


# ── stock counts ──────────────────────────────────────────────────────────

def plan_count(rows, count):
    lines = list(count.lines.select_related('item'))
    by_sku = {l.item.sku.strip().lower(): l for l in lines if l.item.sku and l.item.sku.strip()}
    by_name = {l.item.name.strip().lower(): l for l in lines}
    seen, plan = {}, []
    for rowno, r in rows:
        errs, warn = [], []
        name = ' '.join(r.get('name', '').split())
        sku = r.get('sku', '').strip()
        entry = {'row': rowno, 'name': name or sku or '—', 'category': '', 'price': r.get('counted', ''),
                 'action': 'error', 'messages': errs, 'warnings': warn}
        plan.append(entry)
        if not name and not sku:
            errs.append('Item is missing')
            continue
        a = by_sku.get(sku.lower()) if sku else None
        b = by_name.get(name.lower()) if name else None
        if a and b and a.pk != b.pk:
            errs.append(f'SKU {sku} belongs to “{a.item.name}”, not “{name}”')
            continue
        line = a or b
        if not line:
            errs.append('Not part of this count')
            continue
        if line.pk in seen:
            errs.append(f'Same item as row {seen[line.pk]}')
            continue
        seen[line.pk] = rowno
        entry.update({'name': line.item.name, 'category': f'Book {line.expected}'})
        if 'counted' not in r:
            entry['action'] = 'skip'
            warn.append('Counted is blank — left as it is')
            continue
        try:
            counted = _whole(r['counted'], 'Counted')
        except ValueError as e:
            errs.append(str(e))
            continue
        variance = counted - line.expected
        entry['price'] = str(counted)
        entry['changes'] = [f'Variance {variance:+d}' if variance else 'Matches the books']
        entry['action'] = 'unchanged' if line.counted == counted else 'update'
        entry['data'] = {'line_id': line.pk, 'counted': counted}
    return plan


@transaction.atomic
def apply_count(plan, count):
    n = 0
    for p in plan:
        if p['action'] == 'update':
            StockCountLine.objects.filter(pk=p['data']['line_id'], count=count).update(counted=p['data']['counted'])
            n += 1
    return {'create': 0, 'update': n}


def count_export_rows(count):
    for l in count.lines.select_related('item', 'item__category'):
        yield [l.item.sku or '', l.item.name, str(l.item.category) if l.item.category else '', l.item.unit or '', l.expected,
               '' if l.counted is None else l.counted, '' if l.variance is None else l.variance, float(l.unit_cost or 0),
               '' if l.variance_value is None else float(l.variance_value.quantize(CENT))]


# ── exports only ──────────────────────────────────────────────────────────

def log_export_rows(logs):
    types = {k: str(v) for k, v in InventoryTransaction.TransactionType.choices}
    for t in logs:
        cost = t.unit_cost if t.unit_cost is not None else t.inventory_item.unit_cost
        yield [timezone.localtime(t.created_at).strftime('%Y-%m-%d %H:%M'), t.inventory_item.name, t.inventory_item.sku or '',
               t.inventory_item.unit or '', types.get(t.transaction_type, t.transaction_type), t.delta, float(cost or 0),
               float((Decimal(t.delta) * (cost or 0)).quantize(CENT)), REASONS.get(t.reason, ''), t.note or '',
               (getattr(t.order, 'order_number', None) or (f'#{t.order_id}' if t.order_id else '')),
               (getattr(t.purchase_order, 'invoice_number', None) or getattr(t.purchase_order, 'po_number', None)
                or (f'#{t.purchase_order_id}' if t.purchase_order_id else '')),
               t.performed_by.username if t.performed_by else '']


def challan_export(challans):
    summary, lines = [], []
    for c in challans:
        items = list(c.items.all())
        summary.append([c.challan_no, c.dispatch_date.strftime('%Y-%m-%d') if c.dispatch_date else '', c.get_status_display(),
                        c.from_branch.name if c.from_branch else '', c.to_branch.name if c.to_branch else '',
                        c.receiver_name or '', c.receiver_gstin or '', c.receiver_state or '', c.reason_for_transport or '',
                        c.transport_mode or '', c.vehicle_no or '', len(items), float(c.total_value or 0), float(c.cgst_amount or 0),
                        float(c.sgst_amount or 0), float(c.igst_amount or 0), float(c.total_tax or 0), c.notes or ''])
        for it in items:
            what = it.inventory_item.name if it.inventory_item else (it.menu_item.name if it.menu_item else '')
            lines.append([c.challan_no, c.dispatch_date.strftime('%Y-%m-%d') if c.dispatch_date else '', what, it.hsn_code or '',
                          float(it.quantity), it.unit or '', float(it.unit_value), float(it.line_total)])
    return summary, lines


def as_columns(headers):
    """Plain header list → column tuples for file_response (exports with no import)."""
    return [(h, f'c{i}', '') for i, h in enumerate(headers)]
