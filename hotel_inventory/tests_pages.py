"""Every Stores screen renders, every form posts, and no template variable is undefined."""
import datetime
import re
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template import engines
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hotel import folio as F
from hotel.models import FolioCharge, Room, RoomBooking, RoomCategory
from hotel_inventory import services as S
from hotel_inventory.models import (GoodsReceipt, ItemUnit, LaundryManifest, Location, MinibarProfile, PurchaseOrder, Requisition,
                                    StockAdjustment, StockCount, StockLevel, StockMove, StoreCategory, StoreItem, Vendor)
from super_admin.models import Restaurant, RestaurantBranch, RestaurantUser

D = Decimal
INVALID = '@@INVALID[%s]@@'
INVALID_RE = re.compile(r'@@INVALID\[(.*?)\]@@')


class PagesBase(TestCase):
    def setUp(self):
        cache.clear()
        self.engine = engines['django'].engine
        self._old_invalid = self.engine.string_if_invalid
        self.engine.string_if_invalid = INVALID
        self.r = Restaurant.objects.create(name='Lake Palace', slug='lake-palace')
        self.b = RestaurantBranch.objects.create(restaurant=self.r, name='Lake', slug='lake-palace-main', has_hotel_module=True,
                                                 state='Rajasthan')
        self.cat = RoomCategory.objects.create(branch=self.b, name='Deluxe', base_price_per_night=D('4000'))
        self.rooms = [Room.objects.create(branch=self.b, category=self.cat, room_number=str(101 + i)) for i in range(3)]
        self.user = User.objects.create_user('stores', password='x', is_staff=True)
        RestaurantUser.objects.create(user=self.user, restaurant=self.r, branch=self.b, role=RestaurantUser.Role.HOTEL_ADMIN)
        self.client.force_login(self.user)

        self.store = Location.objects.create(branch=self.b, name='Main Store', kind=Location.Kind.STORE)
        self.pantry = Location.objects.create(branch=self.b, name='Floor 1 Pantry', kind=Location.Kind.PANTRY, parent=self.store,
                                              department=Location.Department.HOUSEKEEPING)
        self.vendor = Vendor.objects.create(branch=self.b, name='A-One Supplies', state='Rajasthan', payment_terms_days=30)
        self.amen = StoreCategory.objects.create(branch=self.b, name='Amenities', gst_rate=D('18'), hsn_code='3401')
        self.soap = StoreItem.objects.create(branch=self.b, name='Soap 30g', unit='pc', category=self.amen, barcode='8901',
                                             reorder_level=D('50'), reorder_qty=D('500'), preferred_vendor=self.vendor)
        ItemUnit.objects.create(item=self.soap, name='case', factor=D('100'), is_purchase_default=True)
        self.water = StoreItem.objects.create(branch=self.b, name='Water 500ml', unit='bottle', kind=StoreItem.Kind.CHARGEABLE,
                                              sale_price=D('40'))
        self.towel = StoreItem.objects.create(branch=self.b, name='Bath towel', unit='pc', kind=StoreItem.Kind.CIRCULATING)
        S.move(self.b, self.soap, 3, unit='case', unit_cost=D('1000'), move_type=StockMove.Type.OPENING, dst=self.store, user=self.user)
        S.move(self.b, self.water, 48, unit_cost=D('15'), move_type=StockMove.Type.OPENING, dst=self.store, user=self.user)
        soiled = S.ensure_special_location(self.b, Location.Kind.LINEN_SOILED, 'Linen — soiled')
        S.move(self.b, self.towel, 40, unit_cost=D('300'), move_type=StockMove.Type.OPENING, dst=soiled, user=self.user)
        StockLevel.objects.filter(item=self.soap, location=self.store).update(min_level=D('400'))  # 300 on hand → low

    def tearDown(self):
        self.engine.string_if_invalid = self._old_invalid

    def url(self, name, **kw):
        return reverse(f'hotel_admin:{name}', kwargs={'restaurant_slug': self.r.slug, **kw})

    def page(self, name, query='', **kw):
        resp = self.client.get(self.url(name, **kw) + query)
        self.assertEqual(resp.status_code, 200, f'{name}{query} returned {resp.status_code}')
        html = resp.content.decode()
        bad = set(INVALID_RE.findall(html[html.find('<main') if '<main' in html else 0:]))
        self.assertFalse(bad, f'{name}{query}: undefined template variables {sorted(bad)}')
        return html

    def post(self, name, data, kw=None, ok=True):
        resp = self.client.post(self.url(name, **(kw or {})), data, follow=True)
        self.assertEqual(resp.status_code, 200)
        msgs = [str(m) for m in resp.context['messages']] if resp.context and 'messages' in resp.context else []
        return resp, msgs


class EveryScreenRenders(PagesBase):
    def seed_documents(self):
        po = S.create_po(self.b, self.vendor, [{'item': self.soap, 'unit_name': 'case', 'quantity': 2, 'unit_price': 1100}],
                         user=self.user)
        S.approve_po(po, self.user)
        line = po.lines.first()
        grn = S.receive_grn(self.b, vendor=self.vendor, location=self.store, po=po, user=self.user, invoice_number='INV-1',
                            lines_data=[{'item': self.soap, 'po_line': line, 'unit_name': 'case', 'received_qty': 1, 'unit_cost': 1100}])
        S.record_vendor_payment(self.b, self.vendor, 500, grn=grn, user=self.user)
        req = S.create_requisition(self.b, Location.Department.HOUSEKEEPING, self.store, self.pantry,
                                   [{'item': self.soap, 'quantity': 50}], user=self.user)
        S.adjust(self.b, self.soap, self.store, 2, reason=StockAdjustment.Reason.BREAKAGE, user=self.user)
        cnt = S.create_stock_count(self.b, self.store, user=self.user)
        MinibarProfile.objects.create(branch=self.b, room_category=self.cat, item=self.water, par_quantity=D('2'))
        S.minibar_refill(self.b, self.rooms[0], user=self.user)
        out = S.send_linen_to_laundry(self.b, self.vendor, [{'item': self.towel, 'quantity': 20}], user=self.user)
        return po, grn, req, cnt, out

    def test_every_get_screen(self):
        po, grn, req, cnt, out = self.seed_documents()
        for name, query in [('stores', ''), ('stores', '?low=1'), ('stores', f'?loc={self.store.pk}&cat={self.amen.pk}&q=soap'),
                            ('stores_items', ''), ('stores_items', '?tab=categories'), ('stores_items', f'?edit={self.soap.pk}'),
                            ('stores_items', '?show=all&q=8901'), ('stores_locations', ''), ('stores_locations', f'?edit={self.pantry.pk}'),
                            ('stores_vendors', ''), ('stores_vendors', f'?edit={self.vendor.pk}'), ('stores_pos', ''),
                            ('stores_pos', '?status=APPROVED'), ('stores_grns', ''), ('stores_payments', ''),
                            ('stores_requisitions', ''), ('stores_adjustments', ''), ('stores_counts', ''), ('stores_minibar', ''),
                            ('stores_linen', ''), ('stores_barcode', ''), ('stores_ledger', ''),
                            ('stores_ledger', f'?item={self.soap.pk}&type=RECEIPT&from=2020-01-01&to=2099-01-01')]:
            self.page(name, query)
        for tab in ('valuation', 'usage', 'gst', 'vendor', 'forecast'):
            self.page('stores_reports', f'?tab={tab}')
        self.page('stores_reports', '?tab=valuation&as_of=2099-01-01')
        self.page('stores_item', pk=self.soap.pk)
        self.page('stores_po_detail', pk=po.pk)
        self.page('stores_grn_detail', pk=grn.pk)
        self.page('stores_requisition_detail', pk=req.pk)
        self.page('stores_count_detail', pk=cnt.pk)
        for name in ('stores_stock_export', 'stores_ledger_export'):
            self.assertEqual(self.client.get(self.url(name)).status_code, 200)
        self.assertEqual(self.client.get(self.url('stores_csv_template', kind='items')).status_code, 200)
        r = self.client.get(self.url('stores_barcode_lookup_api') + '?code=8901').json()
        self.assertTrue(r['found'])

    def test_empty_hotel_renders(self):
        """A brand-new hotel with no stock, documents or room types still opens every screen."""
        StockMove.objects.all().delete()  # queryset delete bypasses the append-only guard (test setup only)
        for name in ('stores', 'stores_items', 'stores_locations', 'stores_vendors', 'stores_pos', 'stores_grns', 'stores_payments',
                     'stores_requisitions', 'stores_adjustments', 'stores_counts', 'stores_minibar', 'stores_linen', 'stores_barcode',
                     'stores_ledger', 'stores_reports'):
            self.page(name)


class FormsWork(PagesBase):
    def test_item_category_unit_level_location_vendor_forms(self):
        _, m = self.post('stores_category_save', {'name': 'Cleaning', 'gst_rate': '18'})
        self.assertTrue(StoreCategory.objects.filter(branch=self.b, name='Cleaning').exists(), m)
        _, m = self.post('stores_item_save', {'name': 'Floor cleaner', 'unit': 'L', 'kind': 'CONSUMABLE', 'pack_unit': 'can',
                                              'pack_factor': '5', 'reorder_level': '10', 'track_expiry': 'on'})
        item = StoreItem.objects.get(branch=self.b, name='Floor cleaner')
        self.assertTrue(item.track_expiry)
        self.assertEqual(item.units.get().factor, D('5'))
        _, m = self.post('stores_item_save', {'pk': item.pk, 'name': 'Floor cleaner', 'unit': 'L', 'kind': 'CONSUMABLE'})
        item.refresh_from_db()
        self.post('stores_unit_save', {'item': item.pk, 'name': 'drum', 'factor': '50'})
        self.assertEqual(item.units.count(), 2)
        self.post('stores_level_save', {'item': item.pk, 'location': self.pantry.pk, 'min_level': '5', 'par_level': '20'})
        self.assertEqual(StockLevel.objects.get(item=item, location=self.pantry).par_level, D('20'))
        self.post('stores_location_save', {'name': 'Cart A', 'kind': 'CART', 'parent': self.pantry.pk})
        self.assertTrue(Location.objects.filter(branch=self.b, name='Cart A', parent=self.pantry).exists())
        self.post('stores_vendor_save', {'name': 'Linen Co', 'gstin': '08ABCDE1234F1Z5', 'payment_terms_days': '15'})
        self.assertTrue(Vendor.objects.filter(branch=self.b, name='Linen Co').exists())

    def test_opening_and_csv_imports(self):
        _, m = self.post('stores_opening_save', {'item': self.water.pk, 'location': self.pantry.pk, 'quantity': '12',
                                                 'unit_cost': '16'})
        self.assertEqual(StockLevel.objects.get(item=self.water, location=self.pantry).quantity, D('12'), m)
        csv_items = SimpleUploadedFile('i.csv', b'name,category,unit,pack_unit,pack_factor\nShampoo 30ml,Amenities,pc,case,200\n')
        self.post('stores_items_import', {'file': csv_items})
        self.assertTrue(StoreItem.objects.filter(branch=self.b, name='Shampoo 30ml').exists())
        csv_open = SimpleUploadedFile('o.csv', b'item,location,quantity,unit,unit_cost\nShampoo 30ml,Main Store,2,case,1600\n')
        self.post('stores_opening_import', {'file': csv_open})
        sh = StoreItem.objects.get(branch=self.b, name='Shampoo 30ml')
        self.assertEqual(sh.on_hand(), D('400'))
        self.assertEqual(sh.wac, D('8'))

    def test_purchase_cycle_through_screens(self):
        self.post('stores_po_save', {'vendor': self.vendor.pk, 'item_id': [self.soap.pk], 'quantity': ['2'],
                                     'unit_price': ['1200'], 'unit_name': ['case']})
        po = PurchaseOrder.objects.get(branch=self.b)
        self.post('stores_po_action', {'op': 'approve'}, {'pk': po.pk})
        line = po.lines.get()
        _, m = self.post('stores_grn_save', {'po': po.pk, 'location': self.store.pk, 'item_id': [self.soap.pk],
                                             'po_line_id': [line.pk], 'received_qty': ['2'], 'rejected_qty': ['0'],
                                             'unit_cost': ['1200'], 'unit_name': ['case'], 'is_itc_eligible': 'on'})
        grn = GoodsReceipt.objects.get(branch=self.b)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.Status.RECEIVED, m)
        self.assertEqual(self.soap.on_hand(), D('500'))
        self.assertEqual(grn.total_amount, D('2832.00'))  # 2400 + 18%
        self.post('stores_payment_save', {'grn': grn.pk, 'amount': '1000', 'payment_method': 'UPI'})
        self.assertEqual(grn.due_amount, D('1832.00'))

    def test_rejected_goods_do_not_enter_stock(self):
        _, m = self.post('stores_grn_save', {'vendor': self.vendor.pk, 'location': self.store.pk, 'item_id': [self.water.pk],
                                             'received_qty': ['24'], 'rejected_qty': ['4'], 'unit_cost': ['15'], 'unit_name': ['']})
        grn = GoodsReceipt.objects.get(branch=self.b)
        self.assertEqual(StockLevel.objects.get(item=self.water, location=self.store).quantity, D('68'), m)  # 48 + 20 accepted
        self.assertEqual(grn.subtotal, D('300.00'))  # billed for accepted only

    def test_requisition_adjustment_count_flow(self):
        self.post('stores_requisition_save', {'department': 'HOUSEKEEPING', 'from_location': self.store.pk,
                                              'to_location': self.pantry.pk, 'item_id': [self.soap.pk], 'quantity': ['40'],
                                              'unit_name': ['pc']})
        req = Requisition.objects.get(branch=self.b)
        for op in ('submit', 'approve', 'issue'):
            self.post('stores_requisition_action', {'op': op}, {'pk': req.pk})
        req.refresh_from_db()
        self.assertEqual(req.status, Requisition.Status.ISSUED)
        self.assertEqual(StockLevel.objects.get(item=self.soap, location=self.pantry).quantity, D('40'))
        _, m = self.post('stores_requisition_action', {'op': 'reject'}, {'pk': req.pk})
        req.refresh_from_db()
        self.assertEqual(req.status, Requisition.Status.ISSUED, 'an issued requisition cannot be rejected')

        self.post('stores_adjustment_save', {'item': self.soap.pk, 'location': self.pantry.pk, 'quantity': '3', 'reason': 'BREAKAGE'})
        self.assertEqual(StockLevel.objects.get(item=self.soap, location=self.pantry).quantity, D('37'))

        self.post('stores_count_create', {'location': self.pantry.pk})
        cnt = StockCount.objects.get(branch=self.b)
        line = cnt.lines.get(item=self.soap)
        S.move(self.b, self.soap, 7, move_type=StockMove.Type.ISSUE, src=self.pantry)  # used while the count was open → 30
        self.post('stores_count_post', {f'counted_{line.pk}': '28'}, {'pk': cnt.pk})
        self.assertEqual(StockLevel.objects.get(item=self.soap, location=self.pantry).quantity, D('28'),
                         'posting a count sets the shelf to what was counted')

    def test_minibar_and_linen_screens(self):
        self.post('stores_minibar_profile_save', {'room_category': self.cat.pk, 'item': self.water.pk, 'par_quantity': '2'})
        self.assertTrue(MinibarProfile.objects.filter(room_category=self.cat, item=self.water).exists())
        self.post('stores_minibar_refill_action', {}, {'room_id': self.rooms[0].pk})
        mini = Location.objects.get(branch=self.b, kind=Location.Kind.MINIBAR, room=self.rooms[0])
        self.assertEqual(StockLevel.objects.get(item=self.water, location=mini).quantity, D('2'))

        stay = RoomBooking.objects.create(room=self.rooms[0], branch=self.b, guest_name='Asha', guest_email='a@x.com',
                                          guest_phone='9876543210', check_in_date=timezone.localdate(),
                                          check_out_date=timezone.localdate() + datetime.timedelta(days=1), booking_status='CHECKED_IN')
        charge, err = F.post_charge(stay, self.user, FolioCharge.Kind.MINIBAR, 'Water 500ml', D('40'), 1, 18)
        self.assertIsNone(err)
        self.assertEqual(StockLevel.objects.get(item=self.water, location=mini).quantity, D('1'))
        F.void_charge(charge, self.user, 'Not consumed')
        self.assertEqual(StockLevel.objects.get(item=self.water, location=mini).quantity, D('2'))

        self.post('stores_linen_par_save', {'room_category': self.cat.pk, 'item': self.towel.pk, 'par_per_room': '4'})
        self.post('stores_linen_send', {'vendor': self.vendor.pk, 'item_id': [self.towel.pk], 'quantity': ['30']})
        out = LaundryManifest.objects.get(branch=self.b, manifest_type='OUTBOUND')
        self.post('stores_linen_receive', {'outbound_manifest': out.pk, 'item_id': [self.towel.pk], 'returned_qty': ['27'],
                                           'damaged_qty': ['2'], 'missing_qty': ['1']})
        clean = Location.objects.get(branch=self.b, kind=Location.Kind.LINEN_CLEAN)
        self.assertEqual(StockLevel.objects.get(item=self.towel, location=clean).quantity, D('27'))
        self.post('stores_linen_retire', {'item': self.towel.pk, 'location': clean.pk, 'quantity': '1', 'reason': 'Stained'})
        self.assertEqual(StockLevel.objects.get(item=self.towel, location=clean).quantity, D('26'))

    def test_view_only_role_cannot_post(self):
        from super_admin.models import CustomRole
        role = CustomRole.objects.create(restaurant=self.r, name='Stores auditor', permissions={'stores': 'view'})
        prof = self.user.restaurant_profile
        prof.custom_role = role
        prof.save()
        self.page('stores')
        before = StockMove.objects.count()
        self.client.post(self.url('stores_opening_save'), {'item': self.water.pk, 'location': self.store.pk, 'quantity': '1'})
        self.assertEqual(StockMove.objects.count(), before)
