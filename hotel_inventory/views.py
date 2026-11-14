"""Hotel admin screens for stores & inventory (Phase 1: masters, stock, opening stock, ledger).
Business rules live in ``hotel_inventory.services``; these views only read forms and render."""
import csv
import datetime
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from hotel.models import Room, RoomCategory
from hotel.paging import paginate
from hotel.views_admin import hotel_admin_required
from hotel.views_pms import _back, _can, _date, _render, _report
from hotel_inventory import services as S
from hotel_inventory.models import (
    ItemUnit, Location, StockLevel, StockMove, StoreCategory, StoreItem, Vendor,
    PurchaseOrder, POLine, GoodsReceipt, GRNLine, VendorPayment,
    Requisition, RequisitionLine, StockAdjustment, StockCount, StockCountLine,
    MinibarProfile, LinenPar, LaundryManifest, LaundryManifestLine, PurchaseBill
)

AREA = 'stores'
TABS = [
    ('stores', 'Stock'),
    ('stores_items', 'Items'),
    ('stores_locations', 'Locations'),
    ('stores_vendors', 'Vendors'),
    ('stores_pos', 'POs'),
    ('stores_grns', 'GRNs'),
    ('stores_bills', 'Bills'),
    ('stores_payments', 'Payables'),
    ('stores_requisitions', 'Requisitions'),
    ('stores_adjustments', 'Adjustments'),
    ('stores_counts', 'Stock Count'),
    ('stores_minibar', 'Minibar'),
    ('stores_linen', 'Linen'),
    ('stores_reports', 'Reports'),
    ('stores_barcode', 'Barcode'),
    ('stores_ledger', 'Ledger'),
]


def _branch(ctx):
    b = ctx['current_branch']
    if b is None:
        raise Http404('No hotel branch selected')
    return b


def _url(ctx, name, **kw):
    return reverse(f'hotel_admin:{name}', kwargs={'restaurant_slug': ctx['rslug'], **kw})


def _page(request, ctx, partial, title, nav, **extra):
    # active_menu = the tab, so the matching sidebar link lights up (Stock Overview, Store Items, …)
    return _render(request, ctx, f'hotel_inventory/{partial}', nav, title, stores_tab=nav,
                   stores_tabs=[(name, label, _url(ctx, name)) for name, label in TABS],
                   can_edit=_can(request, AREA), **extra)


def _deny(request, ctx):
    _report(request, 'Your role can only view Stores & inventory.')
    return redirect(_url(ctx, 'stores'))


def _locations(branch, active=True):
    qs = Location.objects.filter(branch=branch).select_related('parent', 'parent__parent', 'room')
    if active:
        qs = qs.filter(is_active=True)
    return sorted(qs, key=lambda l: l.path.lower())


def _get(model, branch, pk):
    if not str(pk or '').isdigit():
        return None
    return model.objects.filter(branch=branch, pk=pk).first()


# ============================================================ stock overview

@hotel_admin_required
def stock(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    S.ensure_main_store(branch)
    location = _get(Location, branch, request.GET.get('loc'))
    category = _get(StoreCategory, branch, request.GET.get('cat'))
    q = (request.GET.get('q') or '').strip()
    low_only = request.GET.get('low') == '1'
    rows = S.stock_rows(branch, location=location, category=category, search=q, low_only=low_only)
    low_rows, low_items = S.low_stock(branch)
    kpis = {'items': StoreItem.objects.filter(branch=branch, is_active=True).count(),
            'value': S.stock_value(branch, location),
            'low': len({l.item_id for l in low_rows} | {i.pk for i in low_items}),
            'locations': Location.objects.filter(branch=branch, is_active=True).count()}
    page_obj = paginate(request, rows, per_page=50)
    return _page(request, ctx, '_stock.html', 'Stores & inventory', 'stores', rows=page_obj.object_list, page_obj=page_obj,
                 kpis=kpis, locations=_locations(branch), categories=StoreCategory.objects.filter(branch=branch).select_related('parent'),
                 location=location, category=category, q=q, low_only=low_only, low_rows=low_rows[:12], low_items=low_items[:12],
                 all_items=StoreItem.objects.filter(branch=branch, is_active=True).prefetch_related('units'),
                 today=timezone.localdate())


@hotel_admin_required
@require_POST
def opening_save(request, restaurant_slug, ctx):
    """Post opening stock (or a stock-in without a GRN) for one item."""
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    item = _get(StoreItem, branch, request.POST.get('item'))
    loc = _get(Location, branch, request.POST.get('location'))
    if item is None or loc is None:
        _report(request, 'Choose an item and a location.')
        return _back(request, _url(ctx, 'stores'))
    try:
        mv = S.move(branch, item, request.POST.get('quantity'), move_type=StockMove.Type.OPENING, dst=loc,
                    unit=request.POST.get('unit', ''), unit_cost=S.dec(request.POST.get('unit_cost')), user=request.user,
                    note=request.POST.get('note', '').strip() or 'Opening stock')
        _report(request, None, f'Added {S.fmt_qty(mv.quantity)} {item.unit} of {item.name} to {loc.path}.')
    except S.StockError as e:
        _report(request, str(e))
    return _back(request, _url(ctx, 'stores'))


@hotel_admin_required
@require_POST
def opening_import(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    upload = request.FILES.get('file')
    if not upload:
        _report(request, 'Choose a CSV file.')
        return redirect(_url(ctx, 'stores'))
    try:
        posted, errors = S.import_opening(branch, upload, request.user)
    except S.StockError as e:
        _report(request, str(e))
        return redirect(_url(ctx, 'stores'))
    _report(request, None, f'Opening stock posted for {posted} line{"s" if posted != 1 else ""}.')
    for e in errors[:10]:
        _report(request, e)
    return redirect(_url(ctx, 'stores'))


@hotel_admin_required
def stock_export(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    location = _get(Location, branch, request.GET.get('loc'))
    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="stock-{timezone.localdate():%Y%m%d}.csv"'
    resp.write('﻿')
    w = csv.writer(resp)
    w.writerow(['Item', 'SKU', 'Category', 'Location', 'Quantity', 'Unit', 'WAC', 'Value'])
    levels = (StockLevel.objects.filter(item__branch=branch).exclude(quantity=0)
              .select_related('item', 'item__category', 'location', 'location__parent').order_by('item__name'))
    if location is not None:
        levels = levels.filter(location_id__in=location.descendant_ids())
    for l in levels:
        w.writerow([l.item.name, l.item.sku, l.item.category or '', l.location.path, S.fmt_qty(l.quantity), l.item.unit,
                    l.item.wac, (l.quantity * l.item.wac).quantize(S.CENT)])
    return resp


@hotel_admin_required
def csv_template(request, restaurant_slug, ctx, kind):
    cols, sample = {'items': (S.ITEM_COLUMNS, ['Bath soap 30g', 'Amenities', 'pc', 'CONSUMABLE', 'AM-001', '', '3401', '18', '500',
                                                '2000', '', 'case', '500']),
                    'opening': (S.OPENING_COLUMNS, ['Bath soap 30g', 'Main Store', '4', 'case', '1250'])}.get(kind, (None, None))
    if cols is None:
        raise Http404
    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{kind}-template.csv"'
    w = csv.writer(resp)
    w.writerow(cols)
    w.writerow(sample)
    return resp


# ============================================================ items

@hotel_admin_required
def items(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    tab = request.GET.get('tab', 'items')
    q = (request.GET.get('q') or '').strip()
    qs = (StoreItem.objects.filter(branch=branch).select_related('category', 'category__parent', 'preferred_vendor')
          .annotate(n_units=Count('units')).order_by('name'))
    if request.GET.get('show') != 'all':
        qs = qs.filter(is_active=True)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(sku__iexact=q) | Q(barcode=q))
    page_obj = paginate(request, qs, per_page=50)
    edit = _get(StoreItem, branch, request.GET.get('edit'))
    categories = list(StoreCategory.objects.filter(branch=branch).select_related('parent').annotate(n=Count('items')))
    return _page(request, ctx, '_items.html', 'Store items', 'stores_items', tab=tab, rows=page_obj.object_list, page_obj=page_obj,
                 q=q, show=request.GET.get('show', ''), edit=edit, categories=categories, kinds=StoreItem.Kind.choices,
                 vendors=Vendor.objects.filter(branch=branch, is_active=True))


def _item_from_post(item, post, branch):
    item.name = post.get('name', '').strip()[:150]
    item.sku = post.get('sku', '').strip()[:50]
    item.barcode = post.get('barcode', '').strip()[:64]
    item.unit = (post.get('unit', '').strip() or 'pc')[:20]
    item.kind = post.get('kind') if post.get('kind') in StoreItem.Kind.values else StoreItem.Kind.CONSUMABLE
    item.category = _get(StoreCategory, branch, post.get('category'))
    item.preferred_vendor = _get(Vendor, branch, post.get('preferred_vendor'))
    item.hsn_code = post.get('hsn_code', '').strip()[:20]
    item.gst_rate = S.dec(post.get('gst_rate'))
    item.sale_price = S.dec(post.get('sale_price'))
    item.reorder_level = S.dec(post.get('reorder_level'), S.D0)
    item.reorder_qty = S.dec(post.get('reorder_qty'), S.D0)
    item.track_expiry = post.get('track_expiry') == 'on'
    if not item.name:
        return 'Give the item a name.'
    if any(v is not None and v < 0 for v in (item.gst_rate, item.sale_price, item.reorder_level, item.reorder_qty)):
        return 'Rates and levels cannot be negative.'
    if item.gst_rate is not None and item.gst_rate > 28:
        return 'GST rate looks wrong (more than 28%).'
    return None


@hotel_admin_required
@require_POST
def item_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    op = request.POST.get('op', 'save')
    item = _get(StoreItem, branch, request.POST.get('pk'))
    fallback = _url(ctx, 'stores_items')
    if op in ('deactivate', 'activate') and item:
        item.is_active = op == 'activate'
        item.save(update_fields=['is_active', 'updated_at'])
        _report(request, None, f'{item.name} {"restored" if item.is_active else "hidden from lists"}.')
        return _back(request, fallback)
    if op == 'delete' and item:
        if item.moves.exists():
            _report(request, f'{item.name} has stock history, so it can only be hidden.')
        else:
            item.delete()
            _report(request, None, 'Item removed.')
        return redirect(fallback)
    is_new = item is None
    item = item or StoreItem(branch=branch)
    err = _item_from_post(item, request.POST, branch)
    if not is_new and item.moves.exists():
        old = StoreItem.objects.get(pk=item.pk)
        if old.unit != item.unit:
            err = err or f'Stock is already kept in {old.unit}; the base unit cannot change now.'
    if not err:
        try:
            with transaction.atomic():
                item.save()
                pu, pf = request.POST.get('pack_unit', '').strip()[:20], S.dec(request.POST.get('pack_factor'))
                if is_new and pu and pf and pf > 0 and pu.lower() != item.unit.lower():
                    ItemUnit.objects.create(item=item, name=pu, factor=pf, is_purchase_default=True)
        except IntegrityError:
            err = 'Another item already has this name or barcode.'
    if err:
        _report(request, err)
        return _back(request, fallback)
    _report(request, None, f'{item.name} {"added" if is_new else "saved"}.')
    return redirect(_url(ctx, 'stores_item', pk=item.pk)) if not is_new else _back(request, fallback)


@hotel_admin_required
def item_detail(request, restaurant_slug, ctx, pk):
    branch = _branch(ctx)
    item = get_object_or_404(StoreItem.objects.select_related('category', 'preferred_vendor'), branch=branch, pk=pk)
    levels = list(item.levels.select_related('location', 'location__parent').order_by('location__name'))
    level_locs = {l.location_id for l in levels}
    total = sum((l.quantity for l in levels), S.D0)
    moves = (item.moves.select_related('from_location', 'from_location__parent', 'to_location', 'to_location__parent', 'performed_by')
             .order_by('-created_at', '-id'))
    page_obj = paginate(request, moves, per_page=25)
    return _page(request, ctx, '_item.html', item.name, 'stores_items', item=item, levels=levels, total=total,
                 value=(total * item.wac).quantize(S.CENT), units=item.units.all(), moves=page_obj.object_list, page_obj=page_obj,
                 other_locations=[l for l in _locations(branch) if l.pk not in level_locs], categories=StoreCategory.objects.filter(branch=branch).select_related('parent'),
                 kinds=StoreItem.Kind.choices, vendors=Vendor.objects.filter(branch=branch, is_active=True), edit=item)


@hotel_admin_required
@require_POST
def unit_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    item = get_object_or_404(StoreItem, branch=branch, pk=request.POST.get('item'))
    back = _url(ctx, 'stores_item', pk=item.pk)
    if request.POST.get('op') == 'delete':
        ItemUnit.objects.filter(item=item, pk=request.POST.get('pk')).delete()
        _report(request, None, 'Pack size removed.')
        return redirect(back)
    name, factor = request.POST.get('name', '').strip()[:20], S.dec(request.POST.get('factor'))
    if not name or factor is None or factor <= 0:
        _report(request, 'Give the pack a name and how many base units it holds.')
    elif name.lower() == item.unit.lower():
        _report(request, f'{item.unit} is already the base unit.')
    else:
        default = request.POST.get('is_purchase_default') == 'on'
        with transaction.atomic():
            if default:
                item.units.update(is_purchase_default=False)
            ItemUnit.objects.update_or_create(item=item, name=name, defaults={'factor': factor, 'is_purchase_default': default})
        _report(request, None, f'1 {name} = {S.fmt_qty(factor)} {item.unit}.')
    return redirect(back)


@hotel_admin_required
@require_POST
def level_save(request, restaurant_slug, ctx):
    """Set the minimum / par for an item at a location (creates the level row when new)."""
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    item = get_object_or_404(StoreItem, branch=branch, pk=request.POST.get('item'))
    loc = _get(Location, branch, request.POST.get('location'))
    mn, par = S.dec(request.POST.get('min_level'), S.D0), S.dec(request.POST.get('par_level'), S.D0)
    if loc is None:
        _report(request, 'Choose a location.')
    elif mn < 0 or par < 0:
        _report(request, 'Levels cannot be negative.')
    elif par and par < mn:
        _report(request, 'Par level should be at least the minimum.')
    else:
        lvl, _ = S._level(item, loc)
        StockLevel.objects.filter(pk=lvl.pk).update(min_level=mn, par_level=par, updated_at=timezone.now())
        _report(request, None, f'Levels saved for {loc.path}.')
    return redirect(_url(ctx, 'stores_item', pk=item.pk))


@hotel_admin_required
@require_POST
def items_import(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    upload = request.FILES.get('file')
    if not upload:
        _report(request, 'Choose a CSV file.')
        return redirect(_url(ctx, 'stores_items'))
    try:
        created, updated, errors = S.import_items(branch, upload)
    except S.StockError as e:
        _report(request, str(e))
        return redirect(_url(ctx, 'stores_items'))
    _report(request, None, f'{created} item{"s" if created != 1 else ""} added, {updated} updated.')
    for e in errors[:10]:
        _report(request, e)
    return redirect(_url(ctx, 'stores_items'))


@hotel_admin_required
@require_POST
def category_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    back = _url(ctx, 'stores_items') + '?tab=categories'
    cat = _get(StoreCategory, branch, request.POST.get('pk'))
    if request.POST.get('op') == 'delete' and cat:
        if cat.items.exists() or cat.children.exists():
            _report(request, f'{cat.name} still has items or sub-categories.')
        else:
            cat.delete()
            _report(request, None, 'Category removed.')
        return redirect(back)
    cat = cat or StoreCategory(branch=branch)
    cat.name = request.POST.get('name', '').strip()[:100]
    parent = _get(StoreCategory, branch, request.POST.get('parent'))
    cat.parent = parent if parent and parent.pk != cat.pk and parent.parent_id is None else None
    cat.hsn_code = request.POST.get('hsn_code', '').strip()[:20]
    cat.gst_rate = S.dec(request.POST.get('gst_rate'))
    if not cat.name:
        _report(request, 'Give the category a name.')
        return redirect(back)
    try:
        cat.save()
        _report(request, None, f'Category {cat} saved.')
    except IntegrityError:
        _report(request, 'A category with this name already exists there.')
    return redirect(back)


# ============================================================ locations

@hotel_admin_required
def locations(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    S.ensure_main_store(branch)
    rows = _locations(branch, active=request.GET.get('show') != 'all')
    stats = {r['location']: r for r in StockLevel.objects.filter(location__branch=branch).values('location')
             .annotate(n=Count('id', filter=Q(quantity__gt=0)), qty=Sum('quantity'))}
    values = {}
    for l in StockLevel.objects.filter(location__branch=branch, quantity__gt=0).select_related('item').only('location_id', 'quantity', 'item__wac'):
        values[l.location_id] = values.get(l.location_id, S.D0) + l.quantity * l.item.wac
    for r in rows:
        r.n_items = (stats.get(r.pk) or {}).get('n') or 0
        r.value = values.get(r.pk, S.D0).quantize(S.CENT)
        r.depth = r.path.count('›')
    edit = _get(Location, branch, request.GET.get('edit'))
    return _page(request, ctx, '_locations.html', 'Store locations', 'stores_locations', rows=rows, edit=edit,
                 parents=_locations(branch), kinds=Location.Kind.choices, departments=Location.Department.choices,
                 rooms=Room.objects.filter(branch=branch).order_by('room_number'), show=request.GET.get('show', ''))


@hotel_admin_required
@require_POST
def location_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    back = _url(ctx, 'stores_locations')
    loc = _get(Location, branch, request.POST.get('pk'))
    op = request.POST.get('op', 'save')
    if op in ('close', 'reopen') and loc:
        if op == 'close' and loc.levels.filter(quantity__gt=0).exists():
            _report(request, f'{loc.path} still holds stock — transfer it out first.')
        elif op == 'close' and loc.children.filter(is_active=True).exists():
            _report(request, f'Close the locations under {loc.name} first.')
        else:
            loc.is_active = op == 'reopen'
            loc.save(update_fields=['is_active'])
            _report(request, None, f'{loc.path} {"reopened" if loc.is_active else "closed"}.')
        return redirect(back)
    loc = loc or Location(branch=branch)
    loc.name = request.POST.get('name', '').strip()[:100]
    loc.kind = request.POST.get('kind') if request.POST.get('kind') in Location.Kind.values else Location.Kind.STORE
    loc.department = request.POST.get('department') if request.POST.get('department') in Location.Department.values else ''
    loc.room = _get(Room, branch, request.POST.get('room')) if loc.kind == Location.Kind.MINIBAR else None
    parent = _get(Location, branch, request.POST.get('parent'))
    if parent and loc.pk and parent.pk in loc.descendant_ids():
        _report(request, 'A location cannot sit inside itself.')
        return redirect(back)
    loc.parent = parent
    loc.sort_order = int(request.POST.get('sort_order') or 0) if str(request.POST.get('sort_order') or '0').isdigit() else 0
    if not loc.name:
        _report(request, 'Give the location a name.')
        return redirect(back)
    try:
        loc.save()
        _report(request, None, f'{loc.path} saved.')
    except IntegrityError:
        _report(request, 'A location with this name already exists there.')
    return redirect(back)


# ============================================================ vendors

@hotel_admin_required
def vendors(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    qs = Vendor.objects.filter(branch=branch).annotate(n_items=Count('items')).order_by('name')
    if request.GET.get('show') != 'all':
        qs = qs.filter(is_active=True)
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(gstin__iexact=q) | Q(phone__icontains=q))
    page_obj = paginate(request, qs)
    return _page(request, ctx, '_vendors.html', 'Vendors', 'stores_vendors', rows=page_obj.object_list, page_obj=page_obj,
                 edit=_get(Vendor, branch, request.GET.get('edit')), q=q, show=request.GET.get('show', ''))


@hotel_admin_required
@require_POST
def vendor_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    back = _url(ctx, 'stores_vendors')
    v = _get(Vendor, branch, request.POST.get('pk'))
    if request.POST.get('op') in ('deactivate', 'activate') and v:
        v.is_active = request.POST['op'] == 'activate'
        v.save(update_fields=['is_active'])
        _report(request, None, f'{v.name} {"restored" if v.is_active else "hidden"}.')
        return redirect(back)
    v = v or Vendor(branch=branch)
    p = request.POST
    v.name = p.get('name', '').strip()[:150]
    v.contact_person = p.get('contact_person', '').strip()[:120]
    v.phone = p.get('phone', '').strip()[:30]
    v.email = p.get('email', '').strip()[:254]
    v.gstin = p.get('gstin', '').strip().upper()[:15]
    v.state = p.get('state', '').strip()[:60]
    v.address = p.get('address', '').strip()
    terms = p.get('payment_terms_days', '0').strip()
    v.payment_terms_days = min(int(terms), 365) if terms.isdigit() else 0
    if not v.name:
        _report(request, 'Give the vendor a name.')
        return redirect(back)
    if v.gstin and len(v.gstin) != 15:
        _report(request, 'A GSTIN has 15 characters.')
        return redirect(back)
    try:
        v.save()
        _report(request, None, f'{v.name} saved.')
    except IntegrityError:
        _report(request, 'A vendor with this name already exists.')
    return redirect(back)


# ============================================================ ledger

def _ledger_qs(request, branch):
    qs = (StockMove.objects.filter(branch=branch)
          .select_related('item', 'from_location', 'from_location__parent', 'to_location', 'to_location__parent', 'performed_by'))
    item = _get(StoreItem, branch, request.GET.get('item'))
    loc = _get(Location, branch, request.GET.get('loc'))
    kind = request.GET.get('type')
    start, end = _date(request.GET.get('from')), _date(request.GET.get('to'))
    if item:
        qs = qs.filter(item=item)
    if loc:
        qs = qs.filter(Q(from_location=loc) | Q(to_location=loc))
    if kind in StockMove.Type.values:
        qs = qs.filter(move_type=kind)
    if start:
        qs = qs.filter(created_at__date__gte=start)
    if end:
        qs = qs.filter(created_at__date__lte=end)
    return qs, {'item': item, 'loc': loc, 'type': kind or '', 'from': start, 'to': end}


@hotel_admin_required
def ledger(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    qs, f = _ledger_qs(request, branch)
    page_obj = paginate(request, qs, per_page=50)
    return _page(request, ctx, '_ledger.html', 'Stock ledger', 'stores_ledger', rows=page_obj.object_list, page_obj=page_obj, f=f,
                 items=StoreItem.objects.filter(branch=branch).only('id', 'name'), locations=_locations(branch, active=False),
                 types=StockMove.Type.choices, query=request.GET.urlencode())


@hotel_admin_required
def ledger_export(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    qs, _ = _ledger_qs(request, branch)
    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="stock-ledger-{timezone.localdate():%Y%m%d}.csv"'
    resp.write('﻿')
    w = csv.writer(resp)
    w.writerow(['#', 'When', 'Type', 'Item', 'From', 'To', 'Quantity', 'Unit', 'Unit cost', 'Value', 'Left at source',
                'At destination', 'Reference', 'Reason', 'Note', 'By'])
    for m in qs.iterator(chunk_size=500):
        w.writerow([m.pk, timezone.localtime(m.created_at).strftime('%Y-%m-%d %H:%M'), m.get_move_type_display(), m.item.name,
                    m.from_location.path if m.from_location else '', m.to_location.path if m.to_location else '',
                    S.fmt_qty(m.quantity), m.item.unit, m.unit_cost, m.value,
                    S.fmt_qty(m.from_balance) if m.from_balance is not None else '',
                    S.fmt_qty(m.to_balance) if m.to_balance is not None else '', m.ref_label, m.reason, m.note,
                    m.performed_by.get_username() if m.performed_by else ''])
    return resp


@hotel_admin_required
@require_POST
def move_reverse(request, restaurant_slug, ctx, pk):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    mv = get_object_or_404(StockMove, branch=branch, pk=pk)
    if mv.ref_type:
        _report(request, f'This move belongs to {mv.ref_label or mv.ref_type} — undo it from there.')
        return _back(request, _url(ctx, 'stores_ledger'))
    try:
        S.reverse_move(mv, request.user, request.POST.get('note', '').strip())
        _report(request, None, f'Move #{mv.pk} reversed.')
    except S.StockError as e:
        _report(request, str(e))
    return _back(request, _url(ctx, 'stores_ledger'))


# ============================================================ Phase 2: POs & Procurement

@hotel_admin_required
def pos(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    qs = PurchaseOrder.objects.filter(branch=branch).select_related('vendor', 'created_by', 'approved_by')
    status_filter = request.GET.get('status')
    if status_filter in PurchaseOrder.Status.values:
        qs = qs.filter(status=status_filter)
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(po_number__icontains=q) | Q(vendor__name__icontains=q))
    page_obj = paginate(request, qs)
    return _page(request, ctx, '_pos.html', 'Purchase Orders', 'stores_pos', rows=page_obj.object_list, page_obj=page_obj,
                 status_filter=status_filter, q=q, statuses=PurchaseOrder.Status.choices,
                 vendors=Vendor.objects.filter(branch=branch, is_active=True),
                 items=StoreItem.objects.filter(branch=branch, is_active=True).prefetch_related('units'))


@hotel_admin_required
def po_detail(request, restaurant_slug, ctx, pk):
    branch = _branch(ctx)
    po = get_object_or_404(PurchaseOrder.objects.select_related('vendor', 'created_by', 'approved_by'), branch=branch, pk=pk)
    lines = po.lines.select_related('item').all()
    return _page(request, ctx, '_po_detail.html', f'PO {po.po_number}', 'stores_pos', po=po, lines=lines)


@hotel_admin_required
@require_POST
def po_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    vendor = _get(Vendor, branch, request.POST.get('vendor'))
    if not vendor:
        _report(request, 'Select a vendor.')
        return redirect(_url(ctx, 'stores_pos'))
    item_ids = request.POST.getlist('item_id')
    quantities = request.POST.getlist('quantity')
    unit_prices = request.POST.getlist('unit_price')
    unit_names = request.POST.getlist('unit_name')
    lines_data = []
    for i_id, qty_s, price_s, u_name in zip(item_ids, quantities, unit_prices, unit_names):
        item = _get(StoreItem, branch, i_id)
        qty = S.dec(qty_s)
        price = S.dec(price_s)
        if item and qty and qty > 0 and price is not None and price >= 0:
            lines_data.append({
                'item': item,
                'quantity': qty,
                'unit_price': price,
                'unit_name': u_name,
                'tax_percent': item.effective_gst_rate or S.D0
            })
    if not lines_data:
        _report(request, 'Provide at least one valid item line with quantity and price.')
        return redirect(_url(ctx, 'stores_pos'))
    try:
        po = S.create_po(branch, vendor, lines_data, user=request.user,
                         expected_date=_date(request.POST.get('expected_date')),
                         notes=request.POST.get('notes', '').strip())
        _report(request, None, f'Purchase Order {po.po_number} created.')
        return redirect(_url(ctx, 'stores_po_detail', pk=po.pk))
    except S.StockError as e:
        _report(request, str(e))
        return redirect(_url(ctx, 'stores_pos'))


@hotel_admin_required
@require_POST
def po_action(request, restaurant_slug, ctx, pk):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    po = get_object_or_404(PurchaseOrder, branch=branch, pk=pk)
    op = request.POST.get('op')
    try:
        if op == 'approve':
            S.approve_po(po, user=request.user)
            _report(request, None, f'PO {po.po_number} approved.')
        elif op == 'cancel':
            S.cancel_po(po, user=request.user)
            _report(request, None, f'PO {po.po_number} cancelled.')
    except S.StockError as e:
        _report(request, str(e))
    return redirect(_url(ctx, 'stores_po_detail', pk=po.pk))


@hotel_admin_required
@require_POST
def po_autodraft(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    try:
        drafts = S.auto_draft_pos(branch, user=request.user)
        if drafts:
            _report(request, None, f'Drafted {len(drafts)} purchase order{"s" if len(drafts) != 1 else ""} for low stock items.')
        else:
            _report(request, 'No low stock items with preferred vendors found to draft POs for.')
    except S.StockError as e:
        _report(request, str(e))
    return redirect(_url(ctx, 'stores_pos'))


@hotel_admin_required
def grns(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    qs = GoodsReceipt.objects.filter(branch=branch).select_related('vendor', 'location', 'po', 'received_by')
    v_id = request.GET.get('vendor')
    if v_id and v_id.isdigit():
        qs = qs.filter(vendor_id=int(v_id))
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(grn_number__icontains=q) | Q(invoice_number__icontains=q) | Q(vendor__name__icontains=q))
    page_obj = paginate(request, qs)
    approved_pos = list(PurchaseOrder.objects.filter(branch=branch, status__in=[PurchaseOrder.Status.APPROVED, PurchaseOrder.Status.PARTIALLY_RECEIVED])
                        .select_related('vendor').prefetch_related('lines__item'))
    # What the form fills in when a PO is picked: its vendor and each line still to come, at the PO rate
    po_lines = {str(po.pk): {'vendor': po.vendor_id, 'lines': [
        {'po_line': l.pk, 'item': l.item_id, 'qty': str(l.pending_qty.normalize()), 'unit': l.unit_name or l.item.unit,
         'rate': str(l.unit_price.normalize())} for l in po.lines.all() if l.pending_qty > 0]} for po in approved_pos}
    return _page(request, ctx, '_grns.html', 'Goods Receipts (GRN)', 'stores_grns', rows=page_obj.object_list, page_obj=page_obj,
                 vendors=Vendor.objects.filter(branch=branch, is_active=True), locations=_locations(branch),
                 approved_pos=approved_pos, po_lines=po_lines,
                 items=StoreItem.objects.filter(branch=branch, is_active=True).prefetch_related('units'), q=q, vendor_id=v_id)


@hotel_admin_required
def grn_detail(request, restaurant_slug, ctx, pk):
    branch = _branch(ctx)
    grn = get_object_or_404(GoodsReceipt.objects.select_related('vendor', 'location', 'po', 'received_by'), branch=branch, pk=pk)
    lines = grn.lines.select_related('item').all()
    payments = grn.payments.all()
    return _page(request, ctx, '_grn_detail.html', f'GRN {grn.grn_number}', 'stores_grns', grn=grn, lines=lines, payments=payments)


@hotel_admin_required
@require_POST
def grn_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    loc = _get(Location, branch, request.POST.get('location'))
    po = _get(PurchaseOrder, branch, request.POST.get('po'))
    vendor = po.vendor if po else _get(Vendor, branch, request.POST.get('vendor'))
    if not vendor or not loc:
        _report(request, 'Select a vendor and a destination store location.')
        return redirect(_url(ctx, 'stores_grns'))
    item_ids = request.POST.getlist('item_id')
    po_line_ids = request.POST.getlist('po_line_id')
    received_qtys = request.POST.getlist('received_qty')
    rejected_qtys = request.POST.getlist('rejected_qty')
    unit_costs = request.POST.getlist('unit_cost')
    unit_names = request.POST.getlist('unit_name')
    batch_numbers = request.POST.getlist('batch_number')
    expiry_dates = request.POST.getlist('expiry_date')

    lines_data = []
    for idx, i_id in enumerate(item_ids):
        item = _get(StoreItem, branch, i_id)
        if not item:
            continue
        rec_q = S.dec(received_qtys[idx] if idx < len(received_qtys) else 0, S.D0)
        rej_q = S.dec(rejected_qtys[idx] if idx < len(rejected_qtys) else 0, S.D0)
        cost = S.dec(unit_costs[idx] if idx < len(unit_costs) else 0, S.D0)
        u_name = unit_names[idx] if idx < len(unit_names) else item.unit
        batch = batch_numbers[idx] if idx < len(batch_numbers) else ''
        exp_raw = expiry_dates[idx] if idx < len(expiry_dates) else ''
        po_l = None
        if po_line_ids and idx < len(po_line_ids) and po_line_ids[idx].isdigit():
            po_l = POLine.objects.filter(po=po, pk=int(po_line_ids[idx])).first()

        if rec_q > 0 or rej_q > 0:
            lines_data.append({
                'item': item,
                'po_line': po_l,
                'received_qty': rec_q,
                'rejected_qty': rej_q,
                'unit_cost': cost,
                'unit_name': u_name,
                'batch_number': batch,
                'expiry_date': _date(exp_raw),
                'tax_percent': item.effective_gst_rate or S.D0
            })
    if not lines_data:
        _report(request, 'Enter received quantities for at least one item.')
        return redirect(_url(ctx, 'stores_grns'))
    try:
        grn = S.receive_grn(
            branch, vendor=vendor, location=loc, lines_data=lines_data, po=po,
            invoice_number=request.POST.get('invoice_number', '').strip(),
            invoice_date=_date(request.POST.get('invoice_date')),
            is_itc_eligible=request.POST.get('is_itc_eligible') == 'on',
            notes=request.POST.get('notes', '').strip(),
            user=request.user
        )
        _report(request, None, f'Goods Receipt {grn.grn_number} posted. Stock & WAC updated.')
        return redirect(_url(ctx, 'stores_grn_detail', pk=grn.pk))
    except S.StockError as e:
        _report(request, str(e))
        return redirect(_url(ctx, 'stores_grns'))


@hotel_admin_required
def payments(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    payables = S.vendor_payables(branch)
    recent_payments = VendorPayment.objects.filter(branch=branch).select_related('vendor', 'grn', 'bill', 'created_by')[:50]
    total_balance = sum((p['balance'] for p in payables), S.D0)
    unpaid_bills = [b for b in PurchaseBill.objects.filter(branch=branch, status=PurchaseBill.Status.POSTED).select_related('vendor')
                    if not b.is_paid]
    # Deliveries whose bill has not come yet can still be paid (an advance against the GRN)
    unpaid_grns = [g for g in S.pending_grns(branch) if not g.is_paid]
    return _page(request, ctx, '_payments.html', 'Vendor Payables & Payments', 'stores_payments',
                 payables=payables, recent_payments=recent_payments, total_balance=total_balance,
                 vendors=Vendor.objects.filter(branch=branch, is_active=True), unpaid_grns=unpaid_grns, unpaid_bills=unpaid_bills,
                 preselect_bill=request.GET.get('bill', ''),
                 methods=VendorPayment.Method.choices, today=timezone.localdate())


@hotel_admin_required
@require_POST
def payment_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    nxt = request.POST.get('next') or ''
    back = nxt if nxt.startswith('/') and not nxt.startswith('//') else _url(ctx, 'stores_payments')
    vendor = _get(Vendor, branch, request.POST.get('vendor'))
    # One "against" dropdown: "bill:<id>" or "grn:<id>" (plain grn=<id> still accepted)
    against = request.POST.get('against', '')
    bill = _get(PurchaseBill, branch, against[5:]) if against.startswith('bill:') else _get(PurchaseBill, branch, request.POST.get('bill'))
    grn = _get(GoodsReceipt, branch, against[4:]) if against.startswith('grn:') else _get(GoodsReceipt, branch, request.POST.get('grn'))
    doc = bill or grn
    if doc and not vendor:
        vendor = doc.vendor
    if not vendor:
        _report(request, 'Select a vendor.')
        return redirect(back)
    if doc and doc.vendor_id != vendor.pk:
        _report(request, f'That document is from {doc.vendor.name}, not {vendor.name}.')
        return redirect(back)
    amount = S.dec(request.POST.get('amount'))
    if bill and amount and amount > bill.due_amount:
        _report(request, f'Only ₹{bill.due_amount} is still due on bill {bill.invoice_number}.')
        return redirect(back)
    if grn and not grn.bill_id and amount and amount > grn.due_amount:
        _report(request, f'Only ₹{grn.due_amount} is still due on {grn.grn_number}.')
        return redirect(back)
    method = request.POST.get('payment_method')
    try:
        p = S.record_vendor_payment(
            branch, vendor=vendor, amount=amount,
            payment_method=method if method in VendorPayment.Method.values else VendorPayment.Method.BANK,
            payment_date=_date(request.POST.get('payment_date')),
            reference=request.POST.get('reference', '').strip(),
            notes=request.POST.get('notes', '').strip(),
            grn=grn, bill=bill, user=request.user
        )
        _report(request, None, f'Payment of ₹{p.amount} to {vendor.name} recorded.')
    except S.StockError as e:
        _report(request, str(e))
    return redirect(back)


# ============================================================ Phase 3: Internal Flow

@hotel_admin_required
def requisitions(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    qs = Requisition.objects.filter(branch=branch).select_related('from_location', 'to_location', 'requested_by', 'approved_by')
    status_filter = request.GET.get('status')
    if status_filter in Requisition.Status.values:
        qs = qs.filter(status=status_filter)
    dept_filter = request.GET.get('department')
    if dept_filter in Location.Department.values:
        qs = qs.filter(department=dept_filter)
    page_obj = paginate(request, qs)
    return _page(request, ctx, '_requisitions.html', 'Department Requisitions', 'stores_requisitions',
                 rows=page_obj.object_list, page_obj=page_obj, status_filter=status_filter, dept_filter=dept_filter,
                 statuses=Requisition.Status.choices, departments=Location.Department.choices,
                 locations=_locations(branch), items=StoreItem.objects.filter(branch=branch, is_active=True).prefetch_related('units'))


@hotel_admin_required
def requisition_detail(request, restaurant_slug, ctx, pk):
    branch = _branch(ctx)
    req = get_object_or_404(Requisition.objects.select_related('from_location', 'to_location', 'requested_by', 'approved_by', 'issued_by'), branch=branch, pk=pk)
    lines = req.lines.select_related('item').all()
    # Check source on hand for each line
    for l in lines:
        lvl = StockLevel.objects.filter(item=l.item, location=req.from_location).first()
        l.source_on_hand = lvl.quantity if lvl else S.D0
    return _page(request, ctx, '_requisition_detail.html', f'Requisition {req.req_number}', 'stores_requisitions', req=req, lines=lines)


@hotel_admin_required
@require_POST
def requisition_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    from_loc = _get(Location, branch, request.POST.get('from_location'))
    to_loc = _get(Location, branch, request.POST.get('to_location'))
    dept = request.POST.get('department')
    if not from_loc or not to_loc or dept not in Location.Department.values:
        _report(request, 'Select department and source/destination locations.')
        return redirect(_url(ctx, 'stores_requisitions'))
    if from_loc.pk == to_loc.pk:
        _report(request, 'Stock has to go to a different location than it comes from.')
        return redirect(_url(ctx, 'stores_requisitions'))
    item_ids = request.POST.getlist('item_id')
    quantities = request.POST.getlist('quantity')
    unit_names = request.POST.getlist('unit_name')
    lines_data = []
    for i_id, qty_s, u_name in zip(item_ids, quantities, unit_names):
        item = _get(StoreItem, branch, i_id)
        qty = S.dec(qty_s)
        if item and qty and qty > 0:
            lines_data.append({'item': item, 'quantity': qty, 'unit_name': u_name})
    if not lines_data:
        _report(request, 'Add at least one item with quantity.')
        return redirect(_url(ctx, 'stores_requisitions'))
    try:
        req = S.create_requisition(
            branch, department=dept, from_location=from_loc, to_location=to_loc,
            lines_data=lines_data, user=request.user, notes=request.POST.get('notes', '').strip()
        )
        _report(request, None, f'Requisition {req.req_number} drafted.')
        return redirect(_url(ctx, 'stores_requisition_detail', pk=req.pk))
    except S.StockError as e:
        _report(request, str(e))
        return redirect(_url(ctx, 'stores_requisitions'))


@hotel_admin_required
@require_POST
def requisition_action(request, restaurant_slug, ctx, pk):
    branch = _branch(ctx)
    req = get_object_or_404(Requisition, branch=branch, pk=pk)
    op = request.POST.get('op')
    try:
        if op == 'submit':
            S.submit_requisition(req, user=request.user)
            _report(request, None, f'Requisition {req.req_number} submitted for approval.')
        elif op == 'approve':
            if not _can(request, AREA):
                return _deny(request, ctx)
            app_dict = {}
            for line in req.lines.all():
                val = request.POST.get(f'approved_qty_{line.pk}')
                if val is not None:
                    app_dict[line.pk] = val
            S.approve_requisition(req, user=request.user, approved_lines=app_dict if app_dict else None)
            _report(request, None, f'Requisition {req.req_number} approved.')
        elif op == 'issue':
            if not _can(request, AREA):
                return _deny(request, ctx)
            issue_dict = {}
            for line in req.lines.all():
                val = request.POST.get(f'issue_qty_{line.pk}')
                if val is not None:
                    issue_dict[line.pk] = val
            S.issue_requisition(req, user=request.user, issue_quantities=issue_dict if issue_dict else None)
            _report(request, None, f'Goods issued for requisition {req.req_number}. Stock transferred.')
        elif op == 'reject':
            if not _can(request, AREA):
                return _deny(request, ctx)
            reason = request.POST.get('reason', '').strip()
            S.reject_requisition(req, user=request.user, reason=reason)
            _report(request, None, f'Requisition {req.req_number} rejected.')
    except S.StockError as e:
        _report(request, str(e))
    return redirect(_url(ctx, 'stores_requisition_detail', pk=req.pk))


@hotel_admin_required
def adjustments(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    qs = StockAdjustment.objects.filter(branch=branch).select_related('location', 'item', 'room', 'created_by', 'expense')
    reason_filter = request.GET.get('reason')
    if reason_filter in StockAdjustment.Reason.values:
        qs = qs.filter(reason=reason_filter)
    page_obj = paginate(request, qs)
    return _page(request, ctx, '_adjustments.html', 'Stock Adjustments & Wastage', 'stores_adjustments',
                 rows=page_obj.object_list, page_obj=page_obj, reason_filter=reason_filter,
                 reasons=StockAdjustment.Reason.choices, locations=_locations(branch),
                 rooms=Room.objects.filter(branch=branch).order_by('room_number'),
                 items=StoreItem.objects.filter(branch=branch, is_active=True).prefetch_related('units'))


@hotel_admin_required
@require_POST
def adjustment_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    item = _get(StoreItem, branch, request.POST.get('item'))
    loc = _get(Location, branch, request.POST.get('location'))
    qty = S.dec(request.POST.get('quantity'))
    reason = request.POST.get('reason', StockAdjustment.Reason.WASTAGE)
    room = _get(Room, branch, request.POST.get('room'))
    post_exp = request.POST.get('post_to_expense') == 'on'
    photo = request.FILES.get('photo')
    if not item or not loc or not qty or qty <= 0:
        _report(request, 'Select an item, location and quantity.')
        return redirect(_url(ctx, 'stores_adjustments'))
    if reason not in StockAdjustment.Reason.values:
        _report(request, 'Choose why the stock is being written off.')
        return redirect(_url(ctx, 'stores_adjustments'))
    if photo is not None:
        if photo.size > 5 * 1024 * 1024 or not (photo.content_type or '').startswith('image/'):
            _report(request, 'The photo must be an image of 5 MB or less.')
            return redirect(_url(ctx, 'stores_adjustments'))
    try:
        adj = S.adjust(
            branch, item=item, location=loc, qty=qty, reason=reason,
            unit=request.POST.get('unit', ''), room=room, photo=photo,
            notes=request.POST.get('notes', '').strip(), user=request.user,
            post_to_expense=post_exp
        )
        _report(request, None, f'Adjustment {adj.adjustment_number} posted ({adj.get_reason_display()}).')
    except S.StockError as e:
        _report(request, str(e))
    return redirect(_url(ctx, 'stores_adjustments'))


@hotel_admin_required
def counts(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    qs = StockCount.objects.filter(branch=branch).select_related('location', 'counted_by')
    page_obj = paginate(request, qs)
    return _page(request, ctx, '_counts.html', 'Physical Stock Counts', 'stores_counts',
                 rows=page_obj.object_list, page_obj=page_obj, locations=_locations(branch))


@hotel_admin_required
@require_POST
def count_create(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    loc = _get(Location, branch, request.POST.get('location'))
    if not loc:
        _report(request, 'Select a location to count.')
        return redirect(_url(ctx, 'stores_counts'))
    try:
        cnt = S.create_stock_count(branch, loc, user=request.user, notes=request.POST.get('notes', '').strip())
        _report(request, None, f'Stock Count {cnt.count_number} started for {loc.path}.')
        return redirect(_url(ctx, 'stores_count_detail', pk=cnt.pk))
    except S.StockError as e:
        _report(request, str(e))
        return redirect(_url(ctx, 'stores_counts'))


@hotel_admin_required
def count_detail(request, restaurant_slug, ctx, pk):
    branch = _branch(ctx)
    cnt = get_object_or_404(StockCount.objects.select_related('location', 'counted_by'), branch=branch, pk=pk)
    lines = cnt.lines.select_related('item').all()
    return _page(request, ctx, '_count_detail.html', f'Stock Count {cnt.count_number}', 'stores_counts', cnt=cnt, lines=lines)


@hotel_admin_required
@require_POST
def count_post(request, restaurant_slug, ctx, pk):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    cnt = get_object_or_404(StockCount, branch=branch, pk=pk)
    counts_dict = {}
    for line in cnt.lines.all():
        val = request.POST.get(f'counted_{line.pk}')
        if val is not None:
            counts_dict[str(line.pk)] = val
    try:
        S.post_stock_count(cnt, counts_dict, user=request.user)
        _report(request, None, f'Stock count {cnt.count_number} finalized and variance adjustments posted.')
    except S.StockError as e:
        _report(request, str(e))
    return redirect(_url(ctx, 'stores_count_detail', pk=cnt.pk))


# ============================================================ Phase 4: Hotel-specific (Minibar & Linen)

@hotel_admin_required
def minibar(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    categories = RoomCategory.objects.filter(branch=branch).order_by('name')
    profiles = MinibarProfile.objects.filter(branch=branch).select_related('room_category', 'item')
    by_cat = {}
    for p in profiles:
        by_cat.setdefault(p.room_category_id, []).append(p)
    for c in categories:
        c.profile_items = by_cat.get(c.pk, [])
    rooms = Room.objects.filter(branch=branch, is_active=True).select_related('category').order_by('room_number')
    minibar_locs = {l.room_id: l for l in Location.objects.filter(branch=branch, kind=Location.Kind.MINIBAR, room__isnull=False)}
    levels_by_loc = {}
    for lvl in StockLevel.objects.filter(location__in=minibar_locs.values()).select_related('item'):
        levels_by_loc.setdefault(lvl.location_id, []).append(lvl)
    room_data = []
    for r in rooms:
        loc = minibar_locs.get(r.pk)
        levels = levels_by_loc.get(loc.pk, []) if loc else []
        profs = by_cat.get(r.category_id, [])
        is_short = any(lvl.quantity < next((p.par_quantity for p in profs if p.item_id == lvl.item_id), S.D0) for lvl in levels) if profs else False
        room_data.append({'room': r, 'location': loc, 'levels': levels, 'has_deficit': is_short, 'profile_count': len(profs)})
    chargeable_items = StoreItem.objects.filter(branch=branch, is_active=True, kind=StoreItem.Kind.CHARGEABLE)
    return _page(request, ctx, '_minibar.html', 'Minibar Management', 'stores_minibar',
                 categories=categories, by_cat=by_cat, room_data=room_data, chargeable_items=chargeable_items)


@hotel_admin_required
@require_POST
def minibar_profile_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    op = request.POST.get('op')
    if op == 'delete':
        MinibarProfile.objects.filter(branch=branch, pk=request.POST.get('pk')).delete()
        _report(request, None, 'Minibar item removed from profile.')
        return redirect(_url(ctx, 'stores_minibar'))
    cat = _get(RoomCategory, branch, request.POST.get('room_category'))
    item = _get(StoreItem, branch, request.POST.get('item'))
    qty = S.dec(request.POST.get('par_quantity'), Decimal('1'))
    if not cat or not item or qty <= 0:
        _report(request, 'Select room category, item, and positive par quantity.')
        return redirect(_url(ctx, 'stores_minibar'))
    MinibarProfile.objects.update_or_create(
        branch=branch, room_category=cat, item=item, defaults={'par_quantity': qty, 'is_active': True}
    )
    _report(request, None, f'Par for {item.name} set to {S.fmt_qty(qty)} in {cat.name}.')
    return redirect(_url(ctx, 'stores_minibar'))


@hotel_admin_required
@require_POST
def minibar_refill_action(request, restaurant_slug, ctx, room_id):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    room = get_object_or_404(Room, branch=branch, pk=room_id)
    try:
        refilled = S.minibar_refill(branch, room, user=request.user)
        if refilled > 0:
            _report(request, None, f'Room {room.room_number} minibar refilled ({refilled} item types topped up).')
        else:
            _report(request, None, f'Room {room.room_number} minibar is already at par.')
    except S.StockError as e:
        _report(request, str(e))
    return redirect(_url(ctx, 'stores_minibar'))


@hotel_admin_required
def linen(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    categories = RoomCategory.objects.filter(branch=branch).order_by('name')
    pars = LinenPar.objects.filter(branch=branch).select_related('room_category', 'item')
    by_cat = {}
    for p in pars:
        by_cat.setdefault(p.room_category_id, []).append(p)
    for c in categories:
        c.par_rows = by_cat.get(c.pk, [])
    manifests = LaundryManifest.objects.filter(branch=branch).select_related('vendor', 'related_outbound', 'created_by')[:50]
    circulating_items = StoreItem.objects.filter(branch=branch, is_active=True, kind=StoreItem.Kind.CIRCULATING)

    # State locations
    soiled = S.ensure_special_location(branch, Location.Kind.LINEN_SOILED, 'Linen — soiled')
    laundry = S.ensure_special_location(branch, Location.Kind.LINEN_LAUNDRY, 'Linen — at laundry')
    clean = S.ensure_special_location(branch, Location.Kind.LINEN_CLEAN, 'Linen — clean store')
    in_use = S.ensure_special_location(branch, Location.Kind.LINEN_IN_USE, 'Linen — in rooms')
    scrap = S.ensure_special_location(branch, Location.Kind.SCRAP, 'Scrap / retired')

    state_levels = {}
    for lvl in StockLevel.objects.filter(location__in=[soiled, laundry, clean, in_use, scrap]).select_related('item', 'location'):
        state_levels.setdefault(lvl.item_id, {})[lvl.location.kind] = lvl.quantity

    item_summaries = []
    for it in circulating_items:
        st = state_levels.get(it.pk, {})
        tot_active = sum(st.get(k, S.D0) for k in [Location.Kind.LINEN_CLEAN, Location.Kind.LINEN_IN_USE, Location.Kind.LINEN_SOILED, Location.Kind.LINEN_LAUNDRY])
        item_summaries.append({
            'item': it,
            'clean': st.get(Location.Kind.LINEN_CLEAN, S.D0),
            'in_use': st.get(Location.Kind.LINEN_IN_USE, S.D0),
            'soiled': st.get(Location.Kind.LINEN_SOILED, S.D0),
            'laundry': st.get(Location.Kind.LINEN_LAUNDRY, S.D0),
            'scrap': st.get(Location.Kind.SCRAP, S.D0),
            'total_active': tot_active
        })

    open_outbound = LaundryManifest.objects.filter(
        branch=branch, manifest_type=LaundryManifest.ManifestType.OUTBOUND, status=LaundryManifest.Status.SENT
    )

    return _page(request, ctx, '_linen.html', 'Linen & Laundry Tracking', 'stores_linen',
                 categories=categories, by_cat=by_cat, manifests=manifests, items=circulating_items,
                 item_summaries=item_summaries, vendors=Vendor.objects.filter(branch=branch, is_active=True),
                 open_outbound=open_outbound, locations=_locations(branch))


@hotel_admin_required
@require_POST
def linen_par_save(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    op = request.POST.get('op')
    if op == 'delete':
        LinenPar.objects.filter(branch=branch, pk=request.POST.get('pk')).delete()
        _report(request, None, 'Linen par removed.')
        return redirect(_url(ctx, 'stores_linen'))
    cat = _get(RoomCategory, branch, request.POST.get('room_category'))
    item = _get(StoreItem, branch, request.POST.get('item'))
    qty = S.dec(request.POST.get('par_per_room'), Decimal('1'))
    if not cat or not item or qty <= 0:
        _report(request, 'Select room category, item and par.')
        return redirect(_url(ctx, 'stores_linen'))
    LinenPar.objects.update_or_create(
        branch=branch, room_category=cat, item=item, defaults={'par_per_room': qty, 'is_active': True}
    )
    _report(request, None, f'Linen par for {item.name} set to {S.fmt_qty(qty)} per room in {cat.name}.')
    return redirect(_url(ctx, 'stores_linen'))


@hotel_admin_required
@require_POST
def linen_send(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    vendor = _get(Vendor, branch, request.POST.get('vendor'))
    item_ids = request.POST.getlist('item_id')
    quantities = request.POST.getlist('quantity')
    lines_data = []
    for i_id, qty_s in zip(item_ids, quantities):
        item = _get(StoreItem, branch, i_id)
        qty = S.dec(qty_s)
        if item and qty and qty > 0:
            lines_data.append({'item': item, 'quantity': qty})
    if not lines_data:
        _report(request, 'Enter quantities of soiled linen to send.')
        return redirect(_url(ctx, 'stores_linen'))
    try:
        mnf = S.send_linen_to_laundry(branch, vendor, lines_data, user=request.user, notes=request.POST.get('notes', '').strip())
        _report(request, None, f'Outbound manifest {mnf.manifest_number} created and soiled linen transferred to laundry.')
    except S.StockError as e:
        _report(request, str(e))
    return redirect(_url(ctx, 'stores_linen'))


@hotel_admin_required
@require_POST
def linen_receive(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    outbound = _get(LaundryManifest, branch, request.POST.get('outbound_manifest'))
    item_ids = request.POST.getlist('item_id')
    returned_qtys = request.POST.getlist('returned_qty')
    damaged_qtys = request.POST.getlist('damaged_qty')
    missing_qtys = request.POST.getlist('missing_qty')
    lines_data = []
    for idx, i_id in enumerate(item_ids):
        item = _get(StoreItem, branch, i_id)
        if not item:
            continue
        ret_q = S.dec(returned_qtys[idx] if idx < len(returned_qtys) else 0, S.D0)
        dmg_q = S.dec(damaged_qtys[idx] if idx < len(damaged_qtys) else 0, S.D0)
        mis_q = S.dec(missing_qtys[idx] if idx < len(missing_qtys) else 0, S.D0)
        if ret_q > 0 or dmg_q > 0 or mis_q > 0:
            lines_data.append({
                'item': item,
                'returned_qty': ret_q,
                'damaged_qty': dmg_q,
                'missing_qty': mis_q
            })
    if not lines_data:
        _report(request, 'Enter returned quantities for at least one linen item.')
        return redirect(_url(ctx, 'stores_linen'))
    try:
        mnf = S.receive_linen_from_laundry(branch, outbound, lines_data, user=request.user, notes=request.POST.get('notes', '').strip())
        _report(request, None, f'Inbound manifest {mnf.manifest_number} recorded. Clean linen restored to clean store.')
    except S.StockError as e:
        _report(request, str(e))
    return redirect(_url(ctx, 'stores_linen'))


@hotel_admin_required
@require_POST
def linen_retire(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    if not _can(request, AREA):
        return _deny(request, ctx)
    item = _get(StoreItem, branch, request.POST.get('item'))
    loc = _get(Location, branch, request.POST.get('location'))
    qty = S.dec(request.POST.get('quantity'))
    reason = request.POST.get('reason', 'Torn / Stained').strip()
    if not item or not loc or not qty or qty <= 0:
        _report(request, 'Select item, location and positive quantity.')
        return redirect(_url(ctx, 'stores_linen'))
    try:
        S.retire_linen(branch, item, qty, loc, reason=reason, user=request.user)
        _report(request, None, f'{S.fmt_qty(qty)} {item.unit} of {item.name} retired to scrap.')
    except S.StockError as e:
        _report(request, str(e))
    return redirect(_url(ctx, 'stores_linen'))


# ============================================================ Phase 5: Reports & Barcode

@hotel_admin_required
def reports(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    tab = request.GET.get('tab', 'valuation')
    today = timezone.localdate()
    start_date = _date(request.GET.get('from'), today - datetime.timedelta(days=30))
    end_date = _date(request.GET.get('to'), today)

    valuation_rows, valuation_total = [], S.D0
    usage_rows = []
    gst_rows, gst_summary = [], {}
    vendor_stats = []
    forecast_data = {}
    as_of = _date(request.GET.get('as_of'))
    dept = request.GET.get('department') if request.GET.get('department') in Location.Department.values else ''

    if tab == 'valuation':
        valuation_rows, valuation_total = S.valuation_report(branch, as_of)
    elif tab == 'usage':
        usage_rows = S.department_usage_report(branch, start_date, end_date, department=dept or None)
    elif tab == 'gst':
        gst_rows, gst_summary = S.gst_input_register(branch, start_date, end_date)
    elif tab == 'vendor':
        vendor_stats = S.vendor_performance_report(branch)
    elif tab == 'forecast':
        days = int(request.GET.get('days')) if str(request.GET.get('days', '')).isdigit() else 7
        forecast_data = S.consumption_forecast(branch, days=min(max(days, 1), 90))

    return _page(request, ctx, '_reports.html', 'Stores Reports & Analytics', 'stores_reports',
                 tab=tab, start_date=start_date, end_date=end_date, today=today, as_of=as_of, dept=dept,
                 valuation_rows=valuation_rows, valuation_total=valuation_total,
                 usage_rows=usage_rows, departments=Location.Department.choices,
                 gst_rows=gst_rows, gst_summary=gst_summary,
                 vendor_stats=vendor_stats, forecast_data=forecast_data)


@hotel_admin_required
def barcode(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    items = StoreItem.objects.filter(branch=branch, is_active=True).select_related('category').prefetch_related('levels')
    return _page(request, ctx, '_barcode.html', 'Barcode Scanner & Label Printing', 'stores_barcode',
                 items=items, locations=_locations(branch))


@hotel_admin_required
def barcode_lookup_api(request, restaurant_slug, ctx):
    branch = _branch(ctx)
    code = (request.GET.get('code') or '').strip()
    if not code:
        return JsonResponse({'error': 'No barcode provided'}, status=400)
    item = StoreItem.objects.filter(branch=branch, is_active=True).filter(Q(barcode=code) | Q(sku__iexact=code)).first()
    if not item:
        return JsonResponse({'found': False, 'message': f'No item found for code "{code}".'})
    levels = [{
        'location': l.location.path,
        'quantity': S.fmt_qty(l.quantity),
        'min_level': S.fmt_qty(l.min_level),
        'par_level': S.fmt_qty(l.par_level)
    } for l in item.levels.select_related('location')]
    return JsonResponse({
        'found': True,
        'item': {
            'id': item.pk,
            'name': item.name,
            'sku': item.sku,
            'barcode': item.barcode,
            'unit': item.unit,
            'kind': item.get_kind_display(),
            'wac': str(item.wac),
            'sale_price': str(item.sale_price) if item.sale_price else '',
            'total_on_hand': S.fmt_qty(item.on_hand()),
            'levels': levels
        }
    })

