"""Import and export of menu items and menu categories (Excel .xlsx or CSV).

Import is two-step: ``plan_*`` reads the file and works out, row by row, what would be created,
updated, left unchanged or rejected (nothing is saved); ``apply_*`` saves the valid rows in one
transaction. The page shows the plan as a preview before the user confirms.

Matching: a menu item matches an existing one by its Code (POS short code) when the file gives one,
otherwise by name. Categories match by name. Matching ignores case and is scoped to the branch the
admin is working in. On an existing item a blank cell keeps the current value, so a sheet with only
Name + Price is a valid price update.
"""
import csv
import io
import random
import re
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.utils import timezone

from core.models import Category, MenuItem, SubCategory

MAX_ROWS = 5000
MAX_BYTES = 5 * 1024 * 1024
PREVIEW_ROWS = 500

YES = {'yes', 'y', 'true', '1', '✓', 'on'}
NO = {'no', 'n', 'false', '0', 'off', '✗'}
ALLERGENS = ['Dairy/Milk', 'Gluten', 'Peanuts', 'Tree Nuts', 'Eggs', 'Fish', 'Crustaceans', 'Soybeans']
ALLERGEN_ALIASES = {'dairy': 'Dairy/Milk', 'milk': 'Dairy/Milk', 'nuts': 'Tree Nuts', 'tree nut': 'Tree Nuts',
                    'peanut': 'Peanuts', 'egg': 'Eggs', 'shellfish': 'Crustaceans', 'soy': 'Soybeans', 'soya': 'Soybeans',
                    'wheat': 'Gluten'}
CHANNELS = {'pos': 'pos', 'qr': 'qr', 'qr menu': 'qr', 'online': 'online', 'zomato': 'online', 'swiggy': 'online',
            'delivery': 'online', 'website': 'online'}
FOOD_LABELS = {'veg': 'Veg', 'non_veg': 'Non-Veg', 'egg': 'Egg'}
TAX_LABELS = {'GST_FOOD': 'GST Food', 'VAT_BEER': 'VAT Beer', 'VAT_IMFL': 'VAT IMFL', 'VAT_WINE': 'VAT Wine',
              'VAT_LIQUOR': 'VAT Liquor'}
TAX_ALIASES = {'gst': 'GST_FOOD', 'food': 'GST_FOOD', 'gst food': 'GST_FOOD', 'beer': 'VAT_BEER', 'imfl': 'VAT_IMFL',
               'spirits': 'VAT_IMFL', 'wine': 'VAT_WINE', 'liquor': 'VAT_LIQUOR'}

# (header, key, help) — the order is the column order of exports and the template
MENU_COLUMNS = [
    ('Code', 'short_code', 'POS item code, e.g. 101. Used to match existing items; leave blank to match by name.'),
    ('Name *', 'name', 'Item name. Required.'),
    ('Category *', 'category', 'Created automatically if it does not exist. Required for new items.'),
    ('Subcategory', 'subcategory', 'Optional. Created under the category if missing.'),
    ('Price *', 'price', 'Selling price in ₹. Required for new items.'),
    ('Food type', 'food_type', 'Veg, Non-Veg or Egg.'),
    ('Jain', 'is_jain', 'Yes / No.'),
    ('Vegan', 'is_vegan', 'Yes / No.'),
    ('Tax type', 'tax_type', 'GST Food, VAT Beer, VAT IMFL, VAT Wine or VAT Liquor (VAT only for outlets that serve alcohol).'),
    ('Price includes tax', 'is_tax_inclusive', 'Yes / No.'),
    ('Available', 'availability', 'Yes / No — No hides it from the POS and menus.'),
    ('Description', 'description', ''),
    ('SKU', 'sku', 'Generated when left blank on a new item.'),
    ('Calories', 'calories', 'kcal, e.g. 420.'),
    ('Serves', 'portion_serves', 'e.g. 1 Person.'),
    ('Prep time (min)', 'preparation_time', 'Whole minutes.'),
    ('Allergens', 'allergens', 'Comma separated: ' + ', '.join(ALLERGENS) + '.'),
    ('Channels', 'publishing_channels', 'Comma separated: POS, QR, Online.'),
    ('Variants', 'variants_json', 'Type: Name = Price, separated by ;  e.g.  Size: Half = 210; Size: Full = 349'),
    ('Online markup (₹)', 'markup_price', 'Zomato / Swiggy price markup.'),
    ('Packaging fee (₹)', 'packaging_fee', 'Zomato / Swiggy packaging charge.'),
]
CATEGORY_COLUMNS = [
    ('Category *', 'name', 'Category name. Required. Matched by name.'),
    ('Description', 'description', 'Blank keeps the current description.'),
    ('Subcategories', 'subcategories', 'Separated by ;  — missing ones are added, none are removed.'),
    ('Items', 'items', 'Export only (ignored on import).'),
]


# ── reading files ──────────────────────────────────────────────────────────

class ImportFileError(ValueError):
    pass


def _norm_header(h):
    return re.sub(r'[^a-z0-9]+', ' ', str(h or '').replace('*', '').lower()).strip()


def _header_map(columns, aliases=None):
    """normalised header text → field key; accepts the key itself and a few common names too."""
    m = {}
    valid = {c[1] for c in columns}
    common = {'item': 'name', 'item name': 'name', 'short code': 'short_code', 'pos code': 'short_code',
              'item code': 'short_code', 'mrp': 'price', 'rate': 'price', 'veg non veg': 'food_type', 'type': 'food_type',
              'tax inclusive': 'is_tax_inclusive', 'availability': 'availability', 'in stock': 'availability',
              'category name': 'name' if columns is CATEGORY_COLUMNS else 'category', 'sub category': 'subcategory',
              'sub categories': 'subcategories', 'markup': 'markup_price', 'packaging': 'packaging_fee'}
    for alias, key in {**common, **(aliases or {})}.items():  # only aliases for columns this sheet has
        if key in valid:
            m.setdefault(_norm_header(alias), key)
    for header, key, _ in columns:
        m[_norm_header(header)] = key
        m[_norm_header(key)] = key
    return m


def _cell(v):
    if v is None:
        return ''
    if isinstance(v, bool):
        return 'Yes' if v else 'No'
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def read_rows(upload, columns, required=('name',), aliases=None):
    """The uploaded .xlsx/.csv as a list of (sheet row number, {field key: text}).
    ``required``: the heading row must contain at least one of these columns."""
    if upload is None:
        raise ImportFileError('Choose a file to import.')
    if upload.size > MAX_BYTES:
        raise ImportFileError('The file is larger than 5 MB.')
    name = (upload.name or '').lower()
    if name.endswith('.xlsx') or name.endswith('.xlsm'):
        import openpyxl
        try:
            wb = openpyxl.load_workbook(upload, read_only=True, data_only=True)
        except Exception:
            raise ImportFileError('This Excel file could not be read. Save it again as .xlsx and retry.')
        ws = wb.worksheets[0]
        raw = ([_cell(c) for c in r] for r in ws.iter_rows(values_only=True))
    elif name.endswith('.csv') or name.endswith('.txt'):
        data = upload.read()
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = data.decode('latin-1')
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t')
        except csv.Error:
            dialect = csv.excel
        raw = ([_cell(c) for c in r] for r in csv.reader(io.StringIO(text), dialect))
    else:
        raise ImportFileError('Upload an Excel (.xlsx) or CSV file.')

    hmap = _header_map(columns, aliases)
    keys, rows = None, []
    for i, values in enumerate(raw, start=1):
        if keys is None:
            if not any(values):
                continue
            keys = [hmap.get(_norm_header(v)) for v in values]
            if not set(required) & set(keys):
                need = next(h for h, k, _ in columns if k == required[0])
                raise ImportFileError(f'The first row must be the column headings, including “{need}” '
                                      '(download the template to see them).')
            continue
        if not any(values):
            continue
        row = {}
        for k, v in zip(keys, values):
            if k and v != '':
                row[k] = v
        rows.append((i, row))
        if len(rows) > MAX_ROWS:
            raise ImportFileError(f'The file has more than {MAX_ROWS} rows. Split it and import in parts.')
    if keys is None:
        raise ImportFileError('The file is empty.')
    return rows


# ── value parsing ──────────────────────────────────────────────────────────

def _money(v, label):
    s = str(v).replace('₹', '').replace(',', '').replace('Rs.', '').replace('Rs', '').strip()
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        raise ValueError(f'{label} “{v}” is not a number')
    if d < 0:
        raise ValueError(f'{label} cannot be negative')
    return d.quantize(Decimal('0.01'))


def _yes_no(v, label):
    s = str(v).strip().lower()
    if s in YES:
        return True
    if s in NO:
        return False
    raise ValueError(f'{label} must be Yes or No (got “{v}”)')


def _food(v):
    s = re.sub(r'[^a-z]', '', str(v).lower())
    if s in ('veg', 'vegetarian', 'v', 'jain', 'vegan', 'pureveg'):
        return 'veg', s
    if s in ('nonveg', 'nonvegetarian', 'nv', 'meat', 'chicken'):
        return 'non_veg', s
    if s in ('egg', 'eggetarian', 'containsegg'):
        return 'egg', s
    raise ValueError(f'Food type must be Veg, Non-Veg or Egg (got “{v}”)')


def _tax(v):
    s = str(v).strip()
    up = s.upper().replace(' ', '_').replace('-', '_')
    if up in TAX_LABELS:
        return up
    for code, label in TAX_LABELS.items():
        if s.lower() == label.lower():
            return code
    for alias, code in TAX_ALIASES.items():
        if alias in s.lower():
            return code
    raise ValueError(f'Tax type “{v}” is not one of: ' + ', '.join(TAX_LABELS.values()))


def _list(v):
    return [x.strip() for x in re.split(r'[,;|\n]', str(v)) if x.strip()]


def _allergens(v, warn):
    out = []
    for a in _list(v):
        canon = next((x for x in ALLERGENS if x.lower() == a.lower()), None) or ALLERGEN_ALIASES.get(a.lower())
        if canon:
            if canon not in out:
                out.append(canon)
        else:
            warn.append(f'Unknown allergen “{a}” ignored')
    return out


def _channels(v):
    out = []
    for c in _list(v):
        key = CHANNELS.get(c.lower())
        if not key:
            raise ValueError(f'Channel “{c}” must be POS, QR or Online')
        if key not in out:
            out.append(key)
    return out


def _variants(v):
    out = []
    for part in [p.strip() for p in str(v).split(';') if p.strip()]:
        m = re.match(r'^(?:(?P<type>[^:=]+):)?\s*(?P<name>[^=]+?)\s*=\s*(?P<price>.+)$', part)
        if not m:
            raise ValueError(f'Variant “{part}” should look like  Size: Half = 210')
        out.append({'type': (m.group('type') or 'Size').strip() or 'Size', 'name': m.group('name').strip(),
                    'price': float(_money(m.group('price'), 'Variant price'))})
    return out


def variants_text(variants):
    return '; '.join(f"{v.get('type') or 'Size'}: {v.get('name', '')} = {Decimal(str(v.get('price') or 0)):.2f}".replace('.00', '')
                     for v in (variants or []) if isinstance(v, dict))


# ── menu items ─────────────────────────────────────────────────────────────

def menu_scope(restaurant, branch):
    qs = MenuItem.objects.filter(restaurant=restaurant)
    return qs.filter(branch=branch) if branch else qs


def category_scope(restaurant, branch):
    qs = Category.objects.filter(restaurant=restaurant)
    return qs.filter(branch=branch) if branch else qs


def plan_menu(rows, restaurant, branch, mode='upsert'):
    """What importing these rows would do. mode: upsert | create | update."""
    items = list(menu_scope(restaurant, branch).select_related('category', 'subcategory'))
    by_code = {i.short_code.strip().lower(): i for i in items if i.short_code and i.short_code.strip()}
    by_name = {i.name.strip().lower(): i for i in items}
    cats = {c.name.strip().lower(): c for c in category_scope(restaurant, branch)}
    alcohol_ok = bool(branch and getattr(branch, 'serves_alcohol', False)) or branch is None
    seen_names, seen_codes, new_cats, plan = {}, {}, [], []

    for rowno, r in rows:
        errs, warn, data = [], [], {}
        name = ' '.join(r.get('name', '').split())
        code = r.get('short_code', '').strip()
        entry = {'row': rowno, 'name': name or '—', 'category': r.get('category', ''), 'price': r.get('price', ''),
                 'action': 'error', 'messages': errs, 'warnings': warn}
        plan.append(entry)
        if not name:
            errs.append('Name is missing')
            continue
        if name.lower() in seen_names:
            errs.append(f'Same name as row {seen_names[name.lower()]}')
            continue
        seen_names[name.lower()] = rowno
        if code:
            if code.lower() in seen_codes:
                errs.append(f'Code {code} is also used on row {seen_codes[code.lower()]}')
                continue
            seen_codes[code.lower()] = rowno

        existing = by_code.get(code.lower()) if code else None
        named = by_name.get(name.lower())
        if existing and named and existing.pk != named.pk:
            errs.append(f'Code {code} belongs to “{existing.name}”, but another item is already called “{name}”')
            continue
        existing = existing or named
        if existing and code and existing.short_code and existing.short_code.strip().lower() != code.lower():
            other = by_code.get(code.lower())
            if other and other.pk != existing.pk:
                errs.append(f'Code {code} is already used by “{other.name}”')
                continue
        entry['existing_id'] = existing.pk if existing else None

        # parse every provided cell
        try:
            if 'price' in r:
                data['price'] = _money(r['price'], 'Price')
            for k, label in (('markup_price', 'Online markup'), ('packaging_fee', 'Packaging fee')):
                if k in r:
                    data[k] = _money(r[k], label)
            if 'food_type' in r:
                data['food_type'], raw = _food(r['food_type'])
                if raw == 'jain':
                    data['is_jain'] = True
                if raw == 'vegan':
                    data['is_vegan'] = True
            for k, label in (('is_jain', 'Jain'), ('is_vegan', 'Vegan'), ('is_tax_inclusive', 'Price includes tax'),
                             ('availability', 'Available')):
                if k in r:
                    data[k] = _yes_no(r[k], label)
            if 'tax_type' in r:
                data['tax_type'] = _tax(r['tax_type'])
                if data['tax_type'] != 'GST_FOOD' and not alcohol_ok:
                    warn.append('This outlet does not serve alcohol — tax set to GST Food')
                    data['tax_type'] = 'GST_FOOD'
            if 'preparation_time' in r:
                try:
                    data['preparation_time'] = max(0, int(float(r['preparation_time'])))
                except ValueError:
                    raise ValueError(f'Prep time “{r["preparation_time"]}” must be whole minutes')
            if 'allergens' in r:
                data['allergens'] = _allergens(r['allergens'], warn)
            if 'publishing_channels' in r:
                data['publishing_channels'] = _channels(r['publishing_channels'])
            if 'variants_json' in r:
                data['variants_json'] = _variants(r['variants_json'])
        except ValueError as e:
            errs.append(str(e))
            continue
        for k in ('description', 'sku', 'calories', 'portion_serves'):
            if k in r:
                data[k] = r[k]
        if code:
            data['short_code'] = code
        if name != (existing.name if existing else None):
            data['name'] = name

        food = data.get('food_type', existing.food_type if existing else 'veg')
        if food in ('non_veg', 'egg') and (data.get('is_jain') or data.get('is_vegan')):
            warn.append('Non-veg / egg items cannot be Jain or Vegan — unticked')
            data['is_jain'] = data['is_vegan'] = False

        cat_name = ' '.join(r.get('category', '').split())
        sub_name = ' '.join(r.get('subcategory', '').split())
        if not existing:
            if 'price' not in data:
                errs.append('Price is missing')
            if not cat_name:
                errs.append('Category is missing')
            if errs:
                continue
        if cat_name and cat_name.lower() not in cats and cat_name.lower() not in [c.lower() for c in new_cats]:
            new_cats.append(cat_name)
        data['_category'] = cat_name or None
        data['_subcategory'] = sub_name or None
        entry['category'] = cat_name or (existing.category.name if existing and existing.category else '')
        entry['price'] = str(data.get('price', existing.price if existing else ''))

        if existing:
            if mode == 'create':
                entry['action'] = 'skip'
                warn.append('Already exists — skipped (import mode: only add new)')
                continue
            entry['action'] = 'update' if _menu_changes(existing, data) else 'unchanged'
            if entry['action'] == 'update':
                entry['changes'] = _menu_changes(existing, data)
        else:
            if mode == 'update':
                entry['action'] = 'skip'
                warn.append('Not on the menu yet — skipped (import mode: only update existing)')
                continue
            entry['action'] = 'create'
        entry['data'] = data
    return plan, new_cats


def _menu_changes(item, data):
    changed = []
    for k, v in data.items():
        if k == '_category':
            if v and (not item.category or item.category.name.lower() != v.lower()):
                changed.append('Category')
        elif k == '_subcategory':
            if v and (not item.subcategory or item.subcategory.name.lower() != v.lower()):
                changed.append('Subcategory')
        else:
            cur = getattr(item, k)
            if isinstance(cur, Decimal) or k in ('price', 'markup_price', 'packaging_fee'):
                same = Decimal(str(cur or 0)) == Decimal(str(v or 0))
            else:
                same = (cur or '') == (v or '') if not isinstance(v, (list, bool, int)) else cur == v
            if not same:
                changed.append(dict((c[1], c[0].replace(' *', '')) for c in MENU_COLUMNS).get(k, k))
    return changed


@transaction.atomic
def apply_menu(plan, restaurant, branch):
    cats = {c.name.strip().lower(): c for c in category_scope(restaurant, branch)}
    subs = {}
    counts = {'create': 0, 'update': 0, 'categories': 0}

    def category(name):
        key = name.lower()
        if key not in cats:
            cats[key] = Category.objects.create(restaurant=restaurant, branch=branch, name=name)
            counts['categories'] += 1
        return cats[key]

    def subcategory(cat, name):
        key = (cat.pk, name.lower())
        if key not in subs:
            subs[key] = (SubCategory.objects.filter(category=cat, name__iexact=name).first()
                         or SubCategory.objects.create(category=cat, name=name))
        return subs[key]

    for p in plan:
        if p['action'] not in ('create', 'update'):
            continue
        data = dict(p['data'])
        cat_name, sub_name = data.pop('_category'), data.pop('_subcategory')
        if p['action'] == 'update':
            item = MenuItem.objects.select_for_update().get(pk=p['existing_id'])
        else:
            item = MenuItem(restaurant=restaurant, branch=branch, publishing_channels=['pos', 'qr', 'online'])
        for k, v in data.items():
            setattr(item, k, v)
        if cat_name:
            item.category = category(cat_name)
            if item.subcategory and item.subcategory.category_id != item.category.pk and not sub_name:
                item.subcategory = None
        if sub_name and item.category:
            item.subcategory = subcategory(item.category, sub_name)
        if not item.sku:
            prefix = ''.join(ch for ch in item.name.upper() if ch.isalnum())[:3] or 'ITM'
            item.sku = f'{prefix}-{item.short_code or random.randint(1000, 9999)}'
        item.save()
        counts[p['action']] += 1
    return counts


def menu_export_rows(items):
    for i in items:
        yield [
            i.short_code or '', i.name, i.category.name if i.category else '', i.subcategory.name if i.subcategory else '',
            float(i.price), FOOD_LABELS.get(i.food_type, i.food_type), 'Yes' if i.is_jain else 'No',
            'Yes' if i.is_vegan else 'No', TAX_LABELS.get(i.tax_type, i.tax_type), 'Yes' if i.is_tax_inclusive else 'No',
            'Yes' if i.availability else 'No', i.description or '', i.sku or '', i.calories or '', i.portion_serves or '',
            i.preparation_time, ', '.join(i.allergens or []),
            ', '.join({'pos': 'POS', 'qr': 'QR', 'online': 'Online'}.get(c, c) for c in (i.publishing_channels or [])),
            variants_text(i.variants_json), float(i.markup_price or 0), float(i.packaging_fee or 0),
        ]


MENU_SAMPLE = [
    ['101', 'Paneer Tikka', 'Starters', 'Tandoor', 299, 'Veg', 'No', 'No', 'GST Food', 'No', 'Yes',
     'Cottage cheese marinated and grilled in the tandoor', '', '360', '1 Person', 20, 'Dairy/Milk', 'POS, QR, Online',
     'Size: Half = 180; Size: Full = 299', 30, 20],
    ['102', 'Chicken Biryani', 'Main Course', 'Biryani', 349, 'Non-Veg', 'No', 'No', 'GST Food', 'No', 'Yes',
     'Dum-cooked with saffron rice', '', '650', '1 Person', 25, '', 'POS, QR, Online', '', 35, 20],
]


# ── categories ─────────────────────────────────────────────────────────────

def plan_categories(rows, restaurant, branch, mode='upsert'):
    cats = {c.name.strip().lower(): c for c in category_scope(restaurant, branch).prefetch_related('subcategories')}
    seen, plan = {}, []
    for rowno, r in rows:
        errs, warn = [], []
        name = ' '.join(r.get('name', '').split())
        entry = {'row': rowno, 'name': name or '—', 'action': 'error', 'messages': errs, 'warnings': warn}
        plan.append(entry)
        if not name:
            errs.append('Category name is missing')
            continue
        if name.lower() in seen:
            errs.append(f'Same category as row {seen[name.lower()]}')
            continue
        seen[name.lower()] = rowno
        subs = []
        for s in [' '.join(x.split()) for x in re.split(r'[;|\n]', r.get('subcategories', '')) if x.strip()]:
            if s.lower() not in [x.lower() for x in subs]:
                subs.append(s)
        existing = cats.get(name.lower())
        desc = r.get('description')
        entry.update({'existing_id': existing.pk if existing else None, 'description': desc, 'subcategories': subs})
        if existing:
            have = {s.name.lower() for s in existing.subcategories.all()}
            new_subs = [s for s in subs if s.lower() not in have]
            changes = (['Description'] if desc is not None and desc != (existing.description or '') else []) + \
                      ([f'+{len(new_subs)} subcategor{"y" if len(new_subs) == 1 else "ies"}'] if new_subs else [])
            entry['new_subcategories'] = new_subs
            if mode == 'create':
                entry['action'] = 'skip'
                warn.append('Already exists — skipped (import mode: only add new)')
            else:
                entry['action'] = 'update' if changes else 'unchanged'
                entry['changes'] = changes
        else:
            if mode == 'update':
                entry['action'] = 'skip'
                warn.append('Category does not exist — skipped (import mode: only update existing)')
                continue
            entry['action'] = 'create'
            entry['new_subcategories'] = subs
            if subs:
                entry['changes'] = ['Subcategories: ' + ', '.join(subs)]
    return plan


@transaction.atomic
def apply_categories(plan, restaurant, branch):
    counts = {'create': 0, 'update': 0, 'subcategories': 0}
    for p in plan:
        if p['action'] not in ('create', 'update'):
            continue
        if p['action'] == 'create':
            cat = Category.objects.create(restaurant=restaurant, branch=branch, name=p['name'],
                                          description=p.get('description') or '')
        else:
            cat = Category.objects.select_for_update().get(pk=p['existing_id'])
            if p.get('description') is not None:
                cat.description = p['description']
                cat.save(update_fields=['description'])
        for s in p.get('new_subcategories', []):
            SubCategory.objects.create(category=cat, name=s)
            counts['subcategories'] += 1
        counts[p['action']] += 1
    return counts


def category_export_rows(categories):
    for c in categories:
        yield [c.name, c.description or '', '; '.join(s.name for s in c.subcategories.all()), c.items_count]


def categories_for_export(restaurant, branch):
    return (category_scope(restaurant, branch).prefetch_related('subcategories')
            .annotate(items_count=Count('menu_items', distinct=True)).order_by('name'))


CATEGORY_SAMPLE = [
    ['Starters', 'Hot and cold starters', 'Veg Starters; Non-Veg Starters; Tandoor', ''],
    ['Beverages', 'Tea, coffee, shakes and juices', 'Hot Beverages; Shakes; Fresh Juices', ''],
]


# ── writing files ──────────────────────────────────────────────────────────

def _filename(restaurant, what, ext):
    if re.search(r'\d{4}-\d{2}-\d{2}', what):  # already carries its date range
        return f'{restaurant.slug}-{what}.{ext}'
    return f'{restaurant.slug}-{what}-{timezone.localdate():%Y-%m-%d}.{ext}'


def file_response(restaurant, what, columns, rows, fmt, template=False, lists=None, note=None, extra_sheets=None):
    """An .xlsx (styled headings, drop-down lists, a 'How to import' sheet) or a CSV download.
    lists: {field key: [allowed values]} for Excel drop-downs · note: the 'Matching' line of the guide ·
    extra_sheets: [(title, headers, rows)] added after the main sheet (Excel only)."""
    export_only = lambda c: c[1] == 'items' or c[2].startswith('Export only')  # noqa: E731 (always the last columns)
    headers = [c[0] for c in columns if not (template and export_only(c))]
    plain = (str, int, float, Decimal, bool, type(None))
    rows = [[v if isinstance(v, plain) else str(v) for v in r[:len(headers)]] for r in rows]  # e.g. lazy labels
    if fmt == 'csv':
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(headers)
        w.writerows(rows)
        resp = HttpResponse('﻿' + buf.getvalue(), content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="{_filename(restaurant, what, "csv")}"'
        return resp

    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = what.replace('-', ' ').title()[:31]
    ws.append(headers)
    for r in rows:
        ws.append(r)
    fill = PatternFill('solid', fgColor='1F3A8A')
    for c in ws[1]:
        c.font = Font(bold=True, color='FFFFFF')
        c.fill = fill
        c.alignment = Alignment(vertical='center')
    ws.freeze_panes = 'A2'
    ws.row_dimensions[1].height = 22
    for idx, h in enumerate(headers, start=1):
        longest = max([len(str(h))] + [len(str(r[idx - 1])) for r in rows[:300] if idx - 1 < len(r)])
        ws.column_dimensions[get_column_letter(idx)].width = min(48, max(10, longest + 2))
    if lists is None and columns is MENU_COLUMNS:
        lists = {'food_type': ['Veg', 'Non-Veg', 'Egg'], 'is_jain': ['Yes', 'No'], 'is_vegan': ['Yes', 'No'],
                 'is_tax_inclusive': ['Yes', 'No'], 'availability': ['Yes', 'No'], 'tax_type': list(TAX_LABELS.values())}
    if lists:  # drop-downs so the sheet is filled in with values the import accepts
        last = max(len(rows) + 200, 500)
        for idx, (_, key, _) in enumerate(columns, start=1):
            if key in lists and idx <= len(headers):
                dv = DataValidation(type='list', formula1='"' + ','.join(lists[key]) + '"', allow_blank=True,
                                    showErrorMessage=True)
                ws.add_data_validation(dv)
                col = get_column_letter(idx)
                dv.add(f'{col}2:{col}{last}')
    for title, sheet_headers, sheet_rows in (extra_sheets or []):
        extra = wb.create_sheet(title[:31])
        extra.append(sheet_headers)
        for r in sheet_rows:
            extra.append(r)
        for c in extra[1]:
            c.font = Font(bold=True, color='FFFFFF')
            c.fill = fill
        extra.freeze_panes = 'A2'
        for idx, h in enumerate(sheet_headers, start=1):
            longest = max([len(str(h))] + [len(str(r[idx - 1])) for r in sheet_rows[:300] if idx - 1 < len(r)])
            extra.column_dimensions[get_column_letter(idx)].width = min(48, max(10, longest + 2))
    guide = wb.create_sheet('How to import')
    guide.append(['Column', 'What to enter'])
    for c in guide[1]:
        c.font = Font(bold=True, color='FFFFFF')
        c.fill = fill
    for col in columns:
        if not (template and export_only(col)):
            guide.append([col[0], col[2]])
    guide.append([])
    if note is None:
        note = ('Items match existing ones by Code, else by Name (ignoring case). Blank cells keep the current value '
                'on existing items.' if columns is MENU_COLUMNS else
                'Categories match existing ones by name (ignoring case). Subcategories are only ever added.')
    guide.append(['Matching', note])
    guide.append(['Columns marked *', 'Required for new rows.'])
    guide.column_dimensions['A'].width = 22
    guide.column_dimensions['B'].width = 110
    out = io.BytesIO()
    wb.save(out)
    resp = HttpResponse(out.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = f'attachment; filename="{_filename(restaurant, what, "xlsx")}"'
    return resp


def summarize(plan):
    s = {'create': 0, 'update': 0, 'unchanged': 0, 'skip': 0, 'error': 0}
    for p in plan:
        s[p['action']] += 1
    return s


def preview_rows(plan):
    """The plan as plain JSON for the preview table (errors first, then changes)."""
    order = {'error': 0, 'create': 1, 'update': 2, 'skip': 3, 'unchanged': 4}
    rows = sorted(plan, key=lambda p: (order[p['action']], p['row']))[:PREVIEW_ROWS]
    return [{'row': p['row'], 'name': p['name'], 'action': p['action'], 'category': p.get('category', ''),
             'price': p.get('price', ''), 'changes': p.get('changes', []) or p.get('new_subcategories', []),
             'messages': p['messages'], 'warnings': p['warnings']} for p in rows]
