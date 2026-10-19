"""Import / export of Parties (vendors and customers). Reader/writer and preview flow: menu_io.

Matching (ignoring case): GSTIN first, then phone (last 10 digits), then name. A blank cell keeps the
current value on an existing party. GSTINs are fully checked (format, state code, check digit) and fill in
the state / place of supply; an unregistered party never keeps a GSTIN — the same rules as Add Party.
"""
import re
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from core.models import Party
from restaurant_admin.menu_io import _money

TYPES = {'vendor': 'vendor', 'supplier': 'vendor', 'seller': 'vendor', 'creditor': 'vendor',
         'customer': 'customer', 'buyer': 'customer', 'client': 'customer', 'debtor': 'customer'}
REG = {'unregistered': 'unregistered', 'consumer': 'unregistered', 'urd': 'unregistered', 'regular': 'regular',
       'registered': 'regular', 'composition': 'composition', 'overseas': 'overseas', 'foreign': 'overseas'}
BALANCE = {'payable': 'payable', 'to pay': 'payable', 'cr': 'payable', 'credit': 'payable',
           'receivable': 'receivable', 'to receive': 'receivable', 'dr': 'receivable', 'debit': 'receivable'}
REG_LABELS = {'unregistered': 'Unregistered', 'regular': 'Regular', 'composition': 'Composition', 'overseas': 'Overseas'}

COLUMNS = [
    ('Name *', 'name', 'Party name. Required.'),
    ('Type *', 'party_type', 'Vendor or Customer. Required for new parties.'),
    ('Phone', 'phone', 'Used to match an existing party when there is no GSTIN.'),
    ('Email', 'email', ''),
    ('GSTIN', 'gstin', '15 characters; checked and used to match. Fills in the state and place of supply.'),
    ('GST registration', 'gst_registration_type', 'Unregistered, Regular, Composition or Overseas. A GSTIN means Regular when blank.'),
    ('Place of supply', 'place_of_supply', 'State name. Blank = taken from the GSTIN.'),
    ('Address', 'address_line1', 'Building, street, area.'),
    ('City', 'city', ''),
    ('State', 'state', 'Blank = taken from the GSTIN.'),
    ('PIN code', 'pincode', '6 digits.'),
    ('Opening balance (₹)', 'opening_balance', 'Balance carried in from before (0 if none).'),
    ('Balance type', 'balance_type', 'Payable (you owe them / Cr) or Receivable (they owe you / Dr).'),
    ('Current balance (₹)', 'current', 'Export only.'),
]
LISTS = {'party_type': ['Vendor', 'Customer'], 'gst_registration_type': list(REG_LABELS.values()),
         'balance_type': ['Payable', 'Receivable']}
NOTE = ('Parties match existing ones by GSTIN, else phone, else name (ignoring case). Blank cells keep the current '
        'value. Unregistered parties never keep a GSTIN.')
SAMPLE = [['Metro Food Supplies', 'Vendor', '9876543210', 'accounts@metrofood.in', '27AAACM1234F1ZY', 'Regular', '',
           'Plot 12, MIDC', 'Mumbai', '', '400093', 15000, 'Payable'],
          ['Acme Technologies', 'Customer', '9822012345', 'ap@acme.in', '', 'Unregistered', '', '', 'Pune', 'Maharashtra',
           '411001', 0, 'Receivable']]


def _phone_key(v):
    d = re.sub(r'\D', '', str(v or ''))
    return d[-10:] if len(d) >= 10 else ''


def _pick(mapping, v, label, allowed):
    key = re.sub(r'\s+', ' ', str(v).strip().lower().replace('_', ' ').replace('/', ' '))
    if key in mapping:
        return mapping[key]
    for word, code in mapping.items():
        if key.startswith(word):
            return code
    raise ValueError(f'{label} “{v}” must be {allowed}')


def scope(restaurant, branch):
    qs = Party.objects.filter(restaurant=restaurant)
    return qs.filter(Q(branch=branch) | Q(branch__isnull=True)) if branch else qs


def plan(rows, restaurant, branch, mode='upsert'):
    from super_admin.forms import GST_STATE_CODE_MAP, is_valid_gstin_checksum, normalize_pan_or_gstin
    parties = list(scope(restaurant, branch))
    by_gstin = {p.gstin.strip().upper(): p for p in parties if p.gstin and p.gstin.strip()}
    by_phone = {}
    for p in parties:
        if _phone_key(p.phone):
            by_phone.setdefault(_phone_key(p.phone), p)
    by_name = {p.name.strip().lower(): p for p in parties}
    seen, matched, out = {}, {}, []
    for rowno, r in rows:
        errs, warn, data = [], [], {}
        name = ' '.join(r.get('name', '').split())
        entry = {'row': rowno, 'name': name or '—', 'category': r.get('party_type', ''), 'price': r.get('gstin', ''),
                 'action': 'error', 'messages': errs, 'warnings': warn}
        out.append(entry)
        if not name:
            errs.append('Name is missing')
            continue
        gstin = normalize_pan_or_gstin(r.get('gstin', ''))
        phone_key = _phone_key(r.get('phone'))
        for key, val in (('name', name.lower()), ('gstin', gstin), ('phone', phone_key)):
            if val and (key, val) in seen:
                errs.append(f'Same {key} as row {seen[(key, val)]}')
        if errs:
            continue
        for key, val in (('name', name.lower()), ('gstin', gstin), ('phone', phone_key)):
            if val:
                seen[(key, val)] = rowno
        try:
            if 'party_type' in r:
                data['party_type'] = _pick(TYPES, r['party_type'], 'Type', 'Vendor or Customer')
            if 'gst_registration_type' in r:
                data['gst_registration_type'] = _pick(REG, r['gst_registration_type'], 'GST registration',
                                                      'Unregistered, Regular, Composition or Overseas')
            if 'balance_type' in r:
                data['balance_type'] = _pick(BALANCE, r['balance_type'], 'Balance type', 'Payable or Receivable')
            if 'opening_balance' in r:
                data['opening_balance'] = _money(r['opening_balance'], 'Opening balance')
            if gstin and data.get('gst_registration_type') != 'unregistered':  # unregistered: GSTIN is dropped anyway
                if not re.match(r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$', gstin):
                    raise ValueError(f'GSTIN “{gstin}” is not a valid 15-character GSTIN')
                if gstin[:2] not in GST_STATE_CODE_MAP:
                    raise ValueError(f'GSTIN “{gstin}” starts with an unknown state code')
                if not is_valid_gstin_checksum(gstin):
                    raise ValueError(f'GSTIN “{gstin}” fails the check digit — please re-check it')
            email = r.get('email', '').strip()
            if email:
                from django.core.exceptions import ValidationError
                from django.core.validators import validate_email
                try:
                    validate_email(email)
                except ValidationError:
                    raise ValueError(f'Email “{email}” is not valid')
                data['email'] = email
            pin = r.get('pincode', '').strip()
            if pin:
                if not re.fullmatch(r'\d{6}', pin):
                    raise ValueError(f'PIN code “{pin}” must be 6 digits')
                data['pincode'] = pin
        except ValueError as e:
            errs.append(str(e))
            continue
        for k in ('phone', 'address_line1', 'city', 'state', 'place_of_supply'):
            if r.get(k, '').strip():
                data[k] = ' '.join(r[k].split())
        existing = (by_gstin.get(gstin) if gstin else None) or (by_phone.get(phone_key) if phone_key else None) \
            or by_name.get(name.lower())
        if existing and existing.pk in matched:
            errs.append(f'Same party as row {matched[existing.pk]} (matched by GSTIN, phone or name)')
            continue
        if existing:
            matched[existing.pk] = rowno
        if gstin:
            data['gstin'] = gstin
            if not (existing and (existing.gstin or '').upper() == gstin):  # new or changed GSTIN: fill in from it
                data.setdefault('gst_registration_type', 'regular')
                data.setdefault('state', GST_STATE_CODE_MAP[gstin[:2]])
                data.setdefault('place_of_supply', GST_STATE_CODE_MAP[gstin[:2]])
        if data.get('gst_registration_type') == 'unregistered':
            if gstin:
                warn.append('Unregistered party — GSTIN not saved')
            data['gstin'] = ''
            data['place_of_supply'] = ''
        if existing and name != existing.name:
            data['name'] = name
        entry['existing_id'] = existing.pk if existing else None
        entry['category'] = {'vendor': 'Vendor', 'customer': 'Customer'}.get(
            data.get('party_type', existing.party_type if existing else ''), '')
        if not existing:
            if 'party_type' not in data:
                errs.append('Type is missing (Vendor or Customer)')
                continue
            if mode == 'update':
                entry['action'] = 'skip'
                warn.append('Not in Parties yet — skipped (import mode: only update existing)')
                continue
            data['name'] = name
            entry['action'] = 'create'
            if data.get('opening_balance'):
                entry['changes'] = [f"Opening ₹{data['opening_balance']} {data.get('balance_type', 'payable')}"]
        else:
            if mode == 'create':
                entry['action'] = 'skip'
                warn.append('Already in Parties — skipped (import mode: only add new)')
                continue
            labels = {c[1]: c[0].replace(' *', '') for c in COLUMNS}
            changes = []
            for k, v in data.items():
                cur = getattr(existing, k)
                same = Decimal(str(cur or 0)) == v if isinstance(v, Decimal) else (cur or '') == (v or '')
                if not same:
                    changes.append(labels.get(k, k))
            entry['action'] = 'update' if changes else 'unchanged'
            entry['changes'] = changes
            if 'Opening balance (₹)' in changes:
                warn.append('Opening balance changes this party’s ledger balance')
        entry['data'] = data
    return out


@transaction.atomic
def apply(plan_rows, restaurant, branch, user=None):
    counts = {'create': 0, 'update': 0}
    for p in plan_rows:
        if p['action'] not in ('create', 'update'):
            continue
        if p['action'] == 'update':
            party = Party.objects.select_for_update().get(pk=p['existing_id'])
            if not party.branch and branch:
                party.branch = branch
        else:
            party = Party(restaurant=restaurant, branch=branch, opening_balance=Decimal('0.00'),
                          balance_type=Party.BalanceType.PAYABLE)
        for k, v in p['data'].items():
            setattr(party, k, v)
        party.address = ', '.join(x for x in [party.address_line1, party.city, party.state, party.pincode] if x) or party.address
        party.save()
        counts[p['action']] += 1
    return counts


def export_rows(parties):
    for p in parties:
        try:
            current = float(p.current_balance)
        except Exception:
            current = ''
        yield [p.name, {'vendor': 'Vendor', 'customer': 'Customer'}.get(p.party_type, p.party_type), p.phone or '',
               p.email or '', p.gstin or '', REG_LABELS.get(p.gst_registration_type or 'unregistered', ''),
               p.place_of_supply or '', p.address_line1 or '', p.city or '', p.state or '', p.pincode or '',
               float(p.opening_balance or 0), 'Receivable' if p.balance_type == 'receivable' else 'Payable', current]
