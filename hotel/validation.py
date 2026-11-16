"""Guest detail validation shared by the folio page and the New Reservation form."""
import datetime
import re

from django.utils import timezone

MAX_UPLOAD = 5 * 1024 * 1024
PHONE_RE = re.compile(r'^(?:\+?91)?[6-9]\d{9}$|^\+[1-9]\d{6,14}$')
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
GSTIN_RE = re.compile(r'^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]$')
ID_RULES = {
    'AADHAAR': (re.compile(r'^[2-9]\d{11}$'), "Aadhaar numbers have 12 digits and don't start with 0 or 1."),
    'PAN': (re.compile(r'^[A-Z]{5}\d{4}[A-Z]$'), 'PAN looks like ABCDE1234F.'),
    'PASSPORT': (re.compile(r'^[A-Z0-9]{6,9}$'), 'Passport numbers are 6-9 letters and digits.'),
    'VOTER_ID': (re.compile(r'^[A-Z]{3}\d{7}$'), 'Voter ID (EPIC) looks like ABC1234567.'),
    'DRIVING_LICENCE': (re.compile(r'^[A-Z]{2}[0-9A-Z-]{8,16}$'), 'Driving licence numbers start with the 2-letter state code, e.g. MH1420110012345.'),
}
INDIAN = ('indian', 'india', 'in')


def clean_phone(value):
    return re.sub(r'[\s()-]', '', (value or '').strip())[:30]


def clean_id_number(value):
    return re.sub(r'\s', '', (value or '').strip()).upper()[:60]


def phone_error(phone, required=True):
    if not phone:
        return 'A contact number is required.' if required else None
    if not PHONE_RE.match(phone):
        return 'Enter a valid phone number (10-digit Indian mobile, or + and country code).'
    return None


def email_error(email):
    return 'Enter a valid email address.' if email and not EMAIL_RE.match(email) else None


def id_errors(id_type, id_number, nationality, require=False):
    """Errors keyed by field for the ID type / number pair."""
    errors = {}
    foreign = bool(nationality) and nationality.strip().lower() not in INDIAN
    if require and not id_type:
        errors['id_type'] = 'Choose the ID type.'
    if require and not id_number:
        errors['id_number'] = 'Enter the ID number.'
    if id_number and not id_type:
        errors['id_type'] = 'Choose the ID type.'
    if id_type and not id_number:
        errors['id_number'] = 'Enter the ID number.'
    elif id_type and id_number:
        rule = ID_RULES.get(id_type)
        if rule and not rule[0].match(id_number):
            errors['id_number'] = rule[1]
    if foreign and id_type and id_type != 'PASSPORT':
        errors['id_type'] = 'Foreign nationals must register with a passport (Form C).'
    return errors


def dob_error(raw, dob):
    if raw and not dob:
        return 'Enter a valid date.'
    today = timezone.localdate()
    if dob and not (today.replace(year=today.year - 120) <= dob <= today):
        return 'Date of birth must be in the past.'
    return None


def document_error(doc):
    if not doc:
        return None
    if doc.size > MAX_UPLOAD:
        return 'The ID scan must be 5 MB or smaller.'
    if not (doc.content_type or '').startswith(('image/', 'application/pdf')):
        return 'Upload the ID as an image or PDF.'
    return None


def parse_date(value):
    try:
        return datetime.date.fromisoformat((value or '').strip())
    except ValueError:
        return None
