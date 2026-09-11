from django import forms
from django.contrib.auth.models import User, Group
import re
from decimal import Decimal
from super_admin.models import Restaurant, RestaurantUser, RestaurantBranch, GstRegistration, DocumentSeries, UserSecuritySetting
from core.models import RestaurantLanding, GalleryImage, TeamMember, Review, Event, MenuItem, RestaurantSettings, BlogPost, WhyChooseUs
from restaurant_project import constants


GST_STATE_CODE_MAP = {
    "01": "Jammu & Kashmir",
    "02": "Himachal Pradesh",
    "03": "Punjab",
    "04": "Chandigarh",
    "05": "Uttarakhand",
    "06": "Haryana",
    "07": "Delhi",
    "08": "Rajasthan",
    "09": "Uttar Pradesh",
    "10": "Bihar",
    "11": "Sikkim",
    "12": "Arunachal Pradesh",
    "13": "Nagaland",
    "14": "Manipur",
    "15": "Mizoram",
    "16": "Tripura",
    "17": "Meghalaya",
    "18": "Assam",
    "19": "West Bengal",
    "20": "Jharkhand",
    "21": "Odisha",
    "22": "Chhattisgarh",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "26": "Dadra & Nagar Haveli and Daman & Diu",
    "27": "Maharashtra",
    "29": "Karnataka",
    "30": "Goa",
    "31": "Lakshadweep",
    "32": "Kerala",
    "33": "Tamil Nadu",
    "34": "Puducherry",
    "35": "Andaman & Nicobar Islands",
    "36": "Telangana",
    "37": "Andhra Pradesh",
    "38": "Ladakh",
}

REJECTED_STATE_CODES = {"25", "28", "97", "99"}

STATE_CHOICES = [("", "Select State...")] + [
    (name, f"{code} - {name}") for code, name in sorted(GST_STATE_CODE_MAP.items())
]

STATE_NAME_TO_CODE = {name.lower(): code for code, name in GST_STATE_CODE_MAP.items()}
# Add common variations
STATE_NAME_TO_CODE["jammu and kashmir"] = "01"
STATE_NAME_TO_CODE["andaman and nicobar islands"] = "35"
STATE_NAME_TO_CODE["dadra and nagar haveli and daman and diu"] = "26"
STATE_NAME_TO_CODE["orissa"] = "21"
STATE_NAME_TO_CODE["pondicherry"] = "34"

BUSINESS_TYPE_CHOICES = [
    ("", "Select Business Type..."),
    ("Sole Proprietorship", "Sole Proprietorship"),
    ("Partnership Firm", "Partnership Firm"),
    ("LLP", "Limited Liability Partnership (LLP)"),
    ("Private Limited", "Private Limited Company (Pvt Ltd)"),
    ("Public Limited", "Public Limited Company"),
    ("HUF", "Hindu Undivided Family (HUF)"),
    ("Trust", "Trust"),
    ("AOP", "Association of Persons (AOP)"),
    ("BOI", "Body of Individuals (BOI)"),
    ("Society", "Society / Artificial Juridical Person"),
    ("Government", "Government / Local Authority"),
]

BUSINESS_TYPE_PAN_MAP = {
    "Sole Proprietorship": ["P"],
    "Private Limited": ["C"],
    "Public Limited": ["C"],
    "Partnership Firm": ["F"],
    "Partnership": ["F"],
    "LLP": ["F"],
    "HUF": ["H"],
    "Trust": ["T"],
    "AOP": ["A"],
    "BOI": ["B"],
    "Society": ["J"],
    "Government": ["L", "G"],
}

CHARS_MOD36 = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'


def normalize_pan_or_gstin(value: str) -> str:
    if not value:
        return ""
    return re.sub(r'\s+', '', str(value)).strip().upper()


def normalize_phone(value: str) -> str:
    if not value:
        return ""
    val = re.sub(r'[\s\-\(\)\+]', '', str(value)).strip()
    if val.startswith('91') and len(val) == 12:
        val = val[2:]
    elif val.startswith('0') and len(val) == 11:
        val = val[1:]
    return val


def normalize_digits_only(value: str) -> str:
    if not value:
        return ""
    return re.sub(r'\D', '', str(value)).strip()


def normalize_email(value: str) -> str:
    if not value:
        return ""
    return str(value).strip().lower()


def normalize_text_spaces(value: str) -> str:
    if not value:
        return ""
    return re.sub(r'\s+', ' ', str(value)).strip()


def normalize_city(value: str) -> str:
    if not value:
        return ""
    val = normalize_text_spaces(value)
    return " ".join(
        "-".join(part.capitalize() for part in word.split("-"))
        for word in val.split(" ")
    )


def compute_gstin_check_char(first14: str) -> str:
    if not first14 or len(first14) != 14:
        return None
    total_sum = 0
    for i in range(14):
        cp = CHARS_MOD36.find(first14[i])
        if cp < 0:
            return None
        factor = 1 if (i % 2 == 0) else 2
        addend = factor * cp
        addend = (addend // 36) + (addend % 36)
        total_sum += addend
    return CHARS_MOD36[(36 - (total_sum % 36)) % 36]


def is_valid_gstin_checksum(gstin: str) -> bool:
    if not gstin or len(gstin) != 15:
        return False
    expected = compute_gstin_check_char(gstin[:14])
    return expected == gstin[14]


def get_gst_state_code(state_name: str) -> str:
    if not state_name:
        return ""
    clean_name = state_name.strip().lower()
    return STATE_NAME_TO_CODE.get(clean_name, "")


def get_state_3char(state_name):
    if not state_name:
        return "BR"
    clean = re.sub(r'[^A-Za-z]', '', state_name).upper()
    if len(clean) >= 3:
        return clean[:3]
    return (clean + "BR")[:3]


class RestaurantForm(forms.ModelForm):

    state = forms.ChoiceField(choices=STATE_CHOICES, required=False)
    business_type = forms.ChoiceField(choices=BUSINESS_TYPE_CHOICES, required=False)

    class Meta:
        model = Restaurant
        fields = [
            "legal_name",
            "trade_name",
            "name",
            "business_type",
            "pan",
            "is_gst_registered",
            "address_line_1",
            "address_line_2",
            "city",
            "state",
            "pincode",
            "email",
            "mailing_address",
            "phone",
            "gst_type",
            "is_active",
        ]

    def clean(self):
        cleaned_data = super().clean()
        if not self.instance.pk and constants.MAX_ALLOWED_BUSINESSES > 0:
            if Restaurant.objects.count() >= constants.MAX_ALLOWED_BUSINESSES:
                raise forms.ValidationError(
                    f"Only {constants.MAX_ALLOWED_BUSINESSES} registered business is permitted on this system. You can edit the existing business details instead."
                )
        return cleaned_data


class RestaurantAdminCreateForm(forms.Form):
    username = forms.CharField(max_length=150, label="Username*")
    email = forms.EmailField(required=False, label="Email Address")
    password = forms.CharField(widget=forms.PasswordInput, label="Password*")
    restaurant = forms.ModelChoiceField(queryset=Restaurant.objects.all(), label="Assigned Business / Restaurant*")
    branch = forms.ModelChoiceField(queryset=RestaurantBranch.objects.all(), required=False, label="Assigned Branch Outlet (Optional)", help_text="Optional: Restrict admin to a specific branch")
    is_primary_admin = forms.BooleanField(required=False, initial=False, label="Designate as Primary Admin")
    is_2fa_enabled = forms.BooleanField(required=False, initial=True, label="Enable Two-Factor Authentication (OTP on Login)")

    def clean_username(self):
        username = self.cleaned_data.get("username")
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("Username already exists")
        return username

    def clean(self):
        cleaned_data = super().clean()
        is_2fa = cleaned_data.get("is_2fa_enabled")
        email = (cleaned_data.get("email") or "").strip()
        if is_2fa and not email:
            self.add_error("email", "An email address is required when Two-Factor Authentication (OTP) is enabled.")
        return cleaned_data

    def save(self):
        username = self.cleaned_data["username"]
        email = self.cleaned_data.get("email", "").strip()
        password = self.cleaned_data["password"]
        restaurant = self.cleaned_data["restaurant"]
        branch = self.cleaned_data.get("branch")
        is_primary = bool(self.cleaned_data.get("is_primary_admin", False))
        is_2fa = bool(self.cleaned_data.get("is_2fa_enabled", False))

        user = User.objects.create_user(username=username, email=email)
        user.is_staff = True
        user.set_password(password)
        user.save()

        try:
            group, _ = Group.objects.get_or_create(name="RestaurantAdmin")
            user.groups.add(group)
        except Exception:
            pass

        ru = RestaurantUser.objects.create(
            user=user,
            restaurant=restaurant,
            branch=branch,
            is_primary_admin=is_primary,
        )

        UserSecuritySetting.objects.update_or_create(
            user=user,
            defaults={'is_2fa_enabled': is_2fa}
        )

        return ru




class OnboardRestaurantForm(forms.Form):
    # Restaurant / Business fields (§2 Company fields)
    legal_name = forms.CharField(max_length=100, label="Legal Name*")
    trade_name = forms.CharField(max_length=60, required=False, label="Trade / Brand Name")
    business_type = forms.ChoiceField(choices=BUSINESS_TYPE_CHOICES, label="Business Type*")
    pan = forms.CharField(max_length=10, label="PAN*")
    cin_llpin = forms.CharField(max_length=50, required=False, label="CIN / LLPIN")
    restaurant_address_line_1 = forms.CharField(max_length=100, label="Address Line 1*")
    restaurant_address_line_2 = forms.CharField(max_length=100, required=False, label="Address Line 2")
    restaurant_city = forms.CharField(max_length=50, label="City*")
    restaurant_state = forms.ChoiceField(choices=STATE_CHOICES, label="State*")
    restaurant_pincode = forms.CharField(max_length=6, label="Pincode*")
    restaurant_email = forms.EmailField(label="Email*")
    restaurant_phone = forms.CharField(max_length=20, label="Phone*")
    is_gst_registered = forms.BooleanField(required=False, initial=True, label="Is your business GST registered?")
    restaurant_is_active = forms.BooleanField(required=False, initial=True, label="Active")

    def __init__(self, *args, instance=None, **kwargs):
        self.instance = instance
        super().__init__(*args, **kwargs)

    def clean_legal_name(self):
        val = normalize_text_spaces(self.cleaned_data.get("legal_name", ""))
        if not val or len(val) < 3 or len(val) > 100:
            raise forms.ValidationError("Legal Name must be between 3 and 100 characters.")
        if not re.search(r'[A-Za-z]', val):
            raise forms.ValidationError("Legal Name must contain at least one letter.")
        return val

    def clean_trade_name(self):
        val = normalize_text_spaces(self.cleaned_data.get("trade_name", ""))
        if val and (len(val) < 2 or len(val) > 60):
            raise forms.ValidationError("Trade / Brand Name must be between 2 and 60 characters.")
        return val

    def clean_pan(self):
        val = normalize_pan_or_gstin(self.cleaned_data.get("pan", ""))
        if not val:
            raise forms.ValidationError("PAN is required.")
        if not re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', val):
            raise forms.ValidationError("Invalid PAN format (expected e.g. AAACS1234F).")
        return val

    def clean_restaurant_address_line_1(self):
        val = normalize_text_spaces(self.cleaned_data.get("restaurant_address_line_1", ""))
        if not val or len(val) < 5 or len(val) > 100:
            raise forms.ValidationError("Address Line 1 must be between 5 and 100 characters.")
        return val

    def clean_restaurant_address_line_2(self):
        val = normalize_text_spaces(self.cleaned_data.get("restaurant_address_line_2", ""))
        if val and len(val) > 100:
            raise forms.ValidationError("Address Line 2 cannot exceed 100 characters.")
        return val

    def clean_restaurant_city(self):
        val = normalize_city(self.cleaned_data.get("restaurant_city", ""))
        if not val or len(val) < 2 or len(val) > 50:
            raise forms.ValidationError("City must be between 2 and 50 characters.")
        if not re.match(r"^[A-Za-z\s\-']+$", val):
            raise forms.ValidationError("City may only contain letters, spaces, hyphens, and apostrophes.")
        return val

    def clean_restaurant_pincode(self):
        val = normalize_digits_only(self.cleaned_data.get("restaurant_pincode", ""))
        if not val or len(val) != 6:
            raise forms.ValidationError("Pincode must be exactly 6 digits.")
        if val.startswith('0'):
            raise forms.ValidationError("Pincode cannot start with '0'.")
        return val

    def clean_restaurant_phone(self):
        val = normalize_phone(self.cleaned_data.get("restaurant_phone", ""))
        if not val or len(val) != 10 or not re.match(r'^[6-9][0-9]{9}$', val):
            raise forms.ValidationError("Phone number must be a valid 10-digit mobile number starting with 6, 7, 8, or 9.")
        return val

    def clean_restaurant_email(self):
        val = normalize_email(self.cleaned_data.get("restaurant_email", ""))
        if not val:
            raise forms.ValidationError("Email address is required.")
        return val

    def clean(self):
        cleaned = super().clean()
        btype = cleaned.get("business_type")
        pan = cleaned.get("pan") or ""
        legal_name = cleaned.get("legal_name") or ""
        cin = normalize_text_spaces(cleaned.get("cin_llpin") or "").upper()

        if btype and pan and len(pan) == 10:
            # 5.1 PAN 4th character must match Business Type
            fourth_char = pan[3]
            expected_chars = BUSINESS_TYPE_PAN_MAP.get(btype, [])
            if expected_chars and fourth_char not in expected_chars:
                self.add_error(
                    "pan",
                    f"PAN 4th character '{fourth_char}' does not match Business Type '{btype}' (Expected '{'/'.join(expected_chars)}')."
                )

        # 5.6 Legal Name suffix check vs Business Type / PAN
        if legal_name and pan and len(pan) == 10:
            legal_upper = legal_name.upper().strip()
            if re.search(r'\b(PVT\s*LTD|PRIVATE\s+LIMITED|LIMITED|LTD)\b', legal_upper):
                if pan[3] != 'C':
                    self.add_error(
                        "legal_name",
                        "Legal Name indicates a Corporate Entity (Private Limited / Limited), but PAN 4th character is not 'C'."
                    )
            elif re.search(r'\bLLP\b', legal_upper):
                if pan[3] != 'F':
                    self.add_error(
                        "legal_name",
                        "Legal Name indicates an LLP, but PAN 4th character is not 'F'."
                    )

        # Corporate Entities / LLP CIN requirement
        if btype in ["Private Limited", "Public Limited"] and not cin:
            self.add_error("cin_llpin", "CIN is required for Corporate Entities.")
        elif btype == "LLP" and not cin:
            self.add_error("cin_llpin", "LLPIN is required for Limited Liability Partnerships.")

        # 5.11 PAN Uniqueness
        if pan:
            pan_qs = Restaurant.objects.filter(pan__iexact=pan)
            if self.instance and self.instance.pk:
                pan_qs = pan_qs.exclude(pk=self.instance.pk)
            if pan_qs.exists():
                self.add_error("pan", f"Another business is already registered with PAN '{pan}'.")

        # Enforce business limit restriction
        if not self.instance and constants.MAX_ALLOWED_BUSINESSES > 0:
            if Restaurant.objects.count() >= constants.MAX_ALLOWED_BUSINESSES:
                existing = Restaurant.objects.first()
                t_name = (cleaned.get("trade_name") or cleaned.get("legal_name") or "").strip()
                if not existing or existing.name.lower() != t_name.lower():
                    raise forms.ValidationError(
                        f"Only {constants.MAX_ALLOWED_BUSINESSES} registered business is permitted on this system. You can edit the existing business details instead."
                    )
        return cleaned

    def clean_branches_data(self, branches_data, company_pan, is_gst_registered, company_defaults):
        """
        Comprehensive server-side validation for branches JSON according to validation-spec.md
        """
        if not branches_data or not isinstance(branches_data, list):
            raise forms.ValidationError("At least one branch outlet is required.")

        valid_branches = [b for b in branches_data if isinstance(b, dict) and b.get("name")]
        if not valid_branches:
            raise forms.ValidationError("Please provide details for at least one branch outlet.")

        if constants.MAX_ALLOWED_BRANCHES > 0 and len(valid_branches) > constants.MAX_ALLOWED_BRANCHES:
            raise forms.ValidationError(
                f"Branch limit reached. Your license allows a maximum of {constants.MAX_ALLOWED_BRANCHES} branch outlets (attempted {len(valid_branches)})."
            )

        cleaned_branches = []
        gstin_to_reg_type = {}
        seen_branch_names = set()

        for idx, bdata in enumerate(valid_branches):
            b_name = normalize_text_spaces(bdata.get("name", ""))
            if len(b_name) < 2 or len(b_name) > 60:
                raise forms.ValidationError(f"Outlet #{idx+1} name '{b_name}' must be between 2 and 60 characters.")

            if b_name.lower() in seen_branch_names:
                raise forms.ValidationError(f"Duplicate outlet name '{b_name}'. Each branch must have a unique name.")
            seen_branch_names.add(b_name.lower())

            outlet_type = bdata.get("outlet_type", "dine_in")
            if outlet_type not in ["dine_in", "takeaway_qsr", "cloud_kitchen"]:
                outlet_type = "dine_in"

            # Phone validation
            b_phone_raw = bdata.get("phone", "")
            b_phone = normalize_phone(b_phone_raw)
            if b_phone:
                if len(b_phone) != 10 or not re.match(r'^[6-9][0-9]{9}$', b_phone):
                    raise forms.ValidationError(
                        f"Outlet '{b_name}' has invalid phone '{b_phone_raw}'. Must be a 10-digit number starting with 6-9."
                    )

            use_co_addr = bool(bdata.get("use_company_address", True))
            if use_co_addr:
                b_addr1 = company_defaults.get("address_line_1", "")
                b_addr2 = company_defaults.get("address_line_2", "")
                b_city = company_defaults.get("city", "")
                b_state = company_defaults.get("state", "")
                b_pincode = company_defaults.get("pincode", "")
            else:
                b_addr1 = normalize_text_spaces(bdata.get("address_line_1", ""))
                if len(b_addr1) < 5 or len(b_addr1) > 100:
                    raise forms.ValidationError(f"Outlet '{b_name}' Address Line 1 must be between 5 and 100 characters.")

                b_addr2 = normalize_text_spaces(bdata.get("address_line_2", ""))
                if len(b_addr2) > 100:
                    raise forms.ValidationError(f"Outlet '{b_name}' Address Line 2 cannot exceed 100 characters.")

                b_city = normalize_city(bdata.get("city", ""))
                if len(b_city) < 2 or len(b_city) > 50 or not re.match(r"^[A-Za-z\s\-']+$", b_city):
                    raise forms.ValidationError(f"Outlet '{b_name}' City '{b_city}' is invalid.")

                b_state = normalize_text_spaces(bdata.get("state", ""))
                if not b_state or get_gst_state_code(b_state) == "":
                    raise forms.ValidationError(f"Outlet '{b_name}' has invalid State '{b_state}'. Must be selected from the valid GST State table.")

                b_pincode = normalize_digits_only(bdata.get("pincode", ""))
                if len(b_pincode) != 6 or b_pincode.startswith('0'):
                    raise forms.ValidationError(f"Outlet '{b_name}' Pincode must be 6 digits and cannot start with '0'.")

            # FSSAI validation (§4)
            fssai_raw = bdata.get("fssai_license_no", "")
            b_fssai = normalize_digits_only(fssai_raw)
            if b_fssai:
                if len(b_fssai) != 14:
                    raise forms.ValidationError(f"Outlet '{b_name}' FSSAI number must be exactly 14 digits.")
                if b_fssai[0] not in ['1', '2']:
                    raise forms.ValidationError(f"Outlet '{b_name}' FSSAI number must start with '1' (Licence) or '2' (Registration).")

            # GSTIN & Registration Type Validations (§3, §5.2–§5.5, §5.9)
            gstin_val = normalize_pan_or_gstin(bdata.get("gstin", ""))
            reg_type_val = bdata.get("gst_registration_type", "R5")
            is_hotel_tariff = bool(bdata.get("is_hotel_tariff_7500_plus", False))
            if is_hotel_tariff:
                reg_type_val = "R18"

            if is_gst_registered and gstin_val:
                # 5.2 Format check (15 chars, position 14 must be 'Z')
                if len(gstin_val) != 15:
                    raise forms.ValidationError(f"Outlet '{b_name}' GSTIN '{gstin_val}' must be exactly 15 characters.")

                if not re.match(r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$', gstin_val):
                    raise forms.ValidationError(
                        f"Outlet '{b_name}' GSTIN '{gstin_val}' format is invalid (14th character must be 'Z')."
                    )

                gstin_state_code = gstin_val[:2]
                if gstin_state_code in REJECTED_STATE_CODES or gstin_state_code not in GST_STATE_CODE_MAP:
                    raise forms.ValidationError(
                        f"Outlet '{b_name}' GSTIN '{gstin_val}' starts with invalid state code '{gstin_state_code}'."
                    )

                # 5.4 Embedded PAN check
                embedded_pan = gstin_val[2:12]
                if company_pan and embedded_pan != company_pan:
                    raise forms.ValidationError(
                        f"Outlet '{b_name}' GSTIN '{gstin_val}' embedded PAN '{embedded_pan}' does not match Company PAN '{company_pan}'."
                    )

                # 5.5 State code match check
                expected_state_code = get_gst_state_code(b_state)
                if expected_state_code and gstin_state_code != expected_state_code:
                    expected_state_name = GST_STATE_CODE_MAP.get(gstin_state_code, gstin_state_code)
                    raise forms.ValidationError(
                        f"Outlet '{b_name}' GSTIN state code '{gstin_state_code}' ({expected_state_name}) does not match outlet state '{b_state}' (Code {expected_state_code})."
                    )

                # 5.9 Registration Type uniformity per GSTIN
                if gstin_val in gstin_to_reg_type:
                    if gstin_to_reg_type[gstin_val] != reg_type_val:
                        raise forms.ValidationError(
                            f"GSTIN '{gstin_val}' is assigned to multiple outlets with conflicting Registration Types. All outlets sharing a GSTIN must have the exact same Registration Type."
                        )
                else:
                    gstin_to_reg_type[gstin_val] = reg_type_val

            # Alcohol / State VAT validations
            serves_alcohol_val = bool(bdata.get("serves_alcohol", False))
            vat_tin_val = normalize_text_spaces(bdata.get("vat_tin", "")).upper() if serves_alcohol_val else ""
            excise_license_val = normalize_text_spaces(bdata.get("excise_license_no", "")).upper() if serves_alcohol_val else ""

            if serves_alcohol_val:
                if not vat_tin_val:
                    raise forms.ValidationError(f"Outlet '{b_name}' serves alcohol: State VAT TIN is required.")
                if not excise_license_val:
                    raise forms.ValidationError(f"Outlet '{b_name}' serves alcohol: Excise License No. is required.")

            try:
                vat_beer_val = float(bdata.get("vat_beer_rate", 60.00))
            except (ValueError, TypeError):
                vat_beer_val = 60.00
            try:
                vat_imfl_val = float(bdata.get("vat_imfl_rate", 65.00))
            except (ValueError, TypeError):
                vat_imfl_val = 65.00
            try:
                vat_wine_val = float(bdata.get("vat_wine_rate", 60.00))
            except (ValueError, TypeError):
                vat_wine_val = 60.00

            cleaned_branches.append({
                'id': bdata.get("id"),
                'name': b_name,
                'outlet_type': outlet_type,
                'phone': b_phone,
                'use_company_address': use_co_addr,
                'address_line_1': b_addr1,
                'address_line_2': b_addr2,
                'city': b_city,
                'state': b_state,
                'pincode': b_pincode,
                'fssai_license_no': b_fssai,
                'serves_alcohol': serves_alcohol_val,
                'vat_tin': vat_tin_val,
                'excise_license_no': excise_license_val,
                'vat_beer_rate': vat_beer_val,
                'vat_imfl_rate': vat_imfl_val,
                'vat_wine_rate': vat_wine_val,
                'gstin': gstin_val,
                'gst_registration_type': reg_type_val,
                'is_hotel_tariff_7500_plus': is_hotel_tariff,
                'e_invoicing_enabled': bool(bdata.get("e_invoicing_enabled", False)),
            })

        return cleaned_branches

    def save(self, instance=None, branches_data=None):
        trade_name = self.cleaned_data.get("trade_name", "").strip()
        legal_name = self.cleaned_data["legal_name"].strip()
        t_name = trade_name or legal_name
        company_pan = self.cleaned_data.get("pan", "").strip().upper()
        is_gst_reg = self.cleaned_data.get("is_gst_registered", False)

        company_defaults = {
            "address_line_1": self.cleaned_data.get("restaurant_address_line_1", ""),
            "address_line_2": self.cleaned_data.get("restaurant_address_line_2", ""),
            "city": self.cleaned_data.get("restaurant_city", ""),
            "state": self.cleaned_data.get("restaurant_state", ""),
            "pincode": self.cleaned_data.get("restaurant_pincode", ""),
        }

        # Clean & validate branches
        validated_branches = self.clean_branches_data(
            branches_data=branches_data,
            company_pan=company_pan,
            is_gst_registered=is_gst_reg,
            company_defaults=company_defaults,
        )

        target_restaurant = instance or (self.instance if (self.instance and self.instance.pk) else None)
        if target_restaurant:
            restaurant = target_restaurant
            restaurant.legal_name = legal_name
            restaurant.trade_name = trade_name
            restaurant.name = t_name
            restaurant.business_type = self.cleaned_data.get("business_type", "")
            restaurant.pan = company_pan
            restaurant.cin_llpin = (self.cleaned_data.get("cin_llpin") or "").strip().upper()
            restaurant.address_line_1 = company_defaults["address_line_1"]
            restaurant.address_line_2 = company_defaults["address_line_2"]
            restaurant.city = company_defaults["city"]
            restaurant.state = company_defaults["state"]
            restaurant.pincode = company_defaults["pincode"]
            restaurant.email = self.cleaned_data.get("restaurant_email", "")
            restaurant.phone = self.cleaned_data.get("restaurant_phone", "")
            restaurant.is_gst_registered = is_gst_reg
            restaurant.gst_type = Restaurant.GSTType.REGULAR if is_gst_reg else Restaurant.GSTType.UNREGISTERED
            restaurant.is_active = self.cleaned_data.get("restaurant_is_active", True)
            restaurant.save()
        else:
            restaurant = Restaurant.objects.create(
                legal_name=legal_name,
                trade_name=trade_name,
                name=t_name,
                business_type=self.cleaned_data.get("business_type", ""),
                pan=company_pan,
                cin_llpin=(self.cleaned_data.get("cin_llpin") or "").strip().upper(),
                address_line_1=company_defaults["address_line_1"],
                address_line_2=company_defaults["address_line_2"],
                city=company_defaults["city"],
                state=company_defaults["state"],
                pincode=company_defaults["pincode"],
                email=self.cleaned_data.get("restaurant_email", ""),
                phone=self.cleaned_data.get("restaurant_phone", ""),
                is_gst_registered=is_gst_reg,
                gst_type=Restaurant.GSTType.REGULAR if is_gst_reg else Restaurant.GSTType.UNREGISTERED,
                is_active=self.cleaned_data.get("restaurant_is_active", True),
            )

        state_branch_counts = {}
        kept_branch_ids = []

        for bdata in validated_branches:
            b_state = bdata.get("state") or restaurant.state
            gstin_val = bdata.get("gstin", "")
            gst_reg_obj = None

            if restaurant.is_gst_registered and gstin_val:
                reg_type_str = 'composition' if bdata.get("gst_registration_type") == 'C' else 'regular'
                gst_reg_obj, _ = GstRegistration.objects.get_or_create(
                    restaurant=restaurant,
                    gstin=gstin_val,
                    defaults={
                        'reg_type': reg_type_str,
                        'specified_premises': bdata.get("is_hotel_tariff_7500_plus", False),
                        'einvoice_enabled': bdata.get("e_invoicing_enabled", False),
                    }
                )

            branch_id = bdata.get("id")
            branch = None
            if target_restaurant and branch_id and str(branch_id).isdigit():
                branch = RestaurantBranch.objects.filter(id=int(branch_id), restaurant=restaurant).first()

            if branch:
                branch.name = bdata.get("name", "")
                branch.outlet_type = bdata.get("outlet_type", "dine_in")
                branch.phone = bdata.get("phone", "")
                branch.use_company_address = bdata.get("use_company_address", True)
                branch.address_line_1 = bdata.get("address_line_1", "")
                branch.address_line_2 = bdata.get("address_line_2", "")
                branch.city = bdata.get("city", "")
                branch.state = b_state
                branch.pincode = bdata.get("pincode", "")
                branch.fssai_license_no = bdata.get("fssai_license_no", "")
                branch.serves_alcohol = bdata.get("serves_alcohol", False)
                branch.vat_tin = bdata.get("vat_tin", "")
                branch.excise_license_no = bdata.get("excise_license_no", "")
                branch.vat_beer_rate = bdata.get("vat_beer_rate", 60.00)
                branch.vat_imfl_rate = bdata.get("vat_imfl_rate", 65.00)
                branch.vat_wine_rate = bdata.get("vat_wine_rate", 60.00)
                branch.gstin = gstin_val
                branch.gst_registration = gst_reg_obj
                branch.gst_registration_type = bdata.get("gst_registration_type", "R5")
                branch.is_hotel_tariff_7500_plus = bdata.get("is_hotel_tariff_7500_plus", False)
                branch.e_invoicing_enabled = bdata.get("e_invoicing_enabled", False)
                branch.save()
            else:
                branch = RestaurantBranch.objects.create(
                    restaurant=restaurant,
                    gst_registration=gst_reg_obj,
                    name=bdata.get("name", ""),
                    outlet_type=bdata.get("outlet_type", "dine_in"),
                    phone=bdata.get("phone", ""),
                    use_company_address=bdata.get("use_company_address", True),
                    address_line_1=bdata.get("address_line_1", ""),
                    address_line_2=bdata.get("address_line_2", ""),
                    city=bdata.get("city", ""),
                    state=b_state,
                    pincode=bdata.get("pincode", ""),
                    fssai_license_no=bdata.get("fssai_license_no", ""),
                    serves_alcohol=bdata.get("serves_alcohol", False),
                    vat_tin=bdata.get("vat_tin", ""),
                    excise_license_no=bdata.get("excise_license_no", ""),
                    vat_beer_rate=bdata.get("vat_beer_rate", 60.00),
                    vat_imfl_rate=bdata.get("vat_imfl_rate", 65.00),
                    vat_wine_rate=bdata.get("vat_wine_rate", 60.00),
                    gstin=gstin_val,
                    gst_registration_type=bdata.get("gst_registration_type", "R5"),
                    is_hotel_tariff_7500_plus=bdata.get("is_hotel_tariff_7500_plus", False),
                    e_invoicing_enabled=bdata.get("e_invoicing_enabled", False),
                )

            kept_branch_ids.append(branch.id)

            state_code_3 = get_state_3char(b_state)
            state_branch_counts[b_state] = state_branch_counts.get(b_state, 0) + 1
            seq_num = f"{state_branch_counts[b_state]:02d}"
            base_branch_prefix = f"{state_code_3}-{seq_num}"

            voucher_prefix_map = {
                DocumentSeries.VoucherType.SALE: f"{base_branch_prefix}",
                DocumentSeries.VoucherType.CN: f"CN-{base_branch_prefix}",
                DocumentSeries.VoucherType.RCPT: f"RC-{base_branch_prefix}",
                DocumentSeries.VoucherType.ADV: f"ADV-{base_branch_prefix}",
                DocumentSeries.VoucherType.PO: f"PO-{base_branch_prefix}",
                DocumentSeries.VoucherType.GRN: f"GRN-{base_branch_prefix}",
                DocumentSeries.VoucherType.PBILL: f"PB-{base_branch_prefix}",
                DocumentSeries.VoucherType.DN: f"DN-{base_branch_prefix}",
                DocumentSeries.VoucherType.PAY: f"PV-{base_branch_prefix}",
                DocumentSeries.VoucherType.STN: f"STN-{base_branch_prefix}",
                DocumentSeries.VoucherType.ADJ: f"ADJ-{base_branch_prefix}",
                DocumentSeries.VoucherType.VAT_SALE: f"VAT-{base_branch_prefix}",
                DocumentSeries.VoucherType.VAT_CN: f"VCN-{base_branch_prefix}",
            }

            for vtype, pfx in voucher_prefix_map.items():
                DocumentSeries.objects.get_or_create(
                    branch=branch,
                    voucher_type=vtype,
                    defaults={
                        'prefix': pfx,
                        'digits': 4,
                        'next_no': 1,
                        'fy_reset': True,
                        'method': DocumentSeries.Method.AUTO
                    }
                )

        if target_restaurant and kept_branch_ids:
            branches_to_delete = list(RestaurantBranch.objects.filter(restaurant=restaurant).exclude(id__in=kept_branch_ids))
            for b_del in branches_to_delete:
                safe_delete_branch(b_del)

        # Initialize RestaurantSettings with onboarded GST rates
        first_b = restaurant.branches.first()
        c_val = Decimal('2.50')
        s_val = Decimal('2.50')
        if first_b:
            if first_b.gst_registration_type == 'R18' or first_b.is_hotel_tariff_7500_plus:
                c_val = Decimal('9.00')
                s_val = Decimal('9.00')
            elif first_b.gst_registration_type in ['C', 'U']:
                c_val = Decimal('0.00')
                s_val = Decimal('0.00')

        RestaurantSettings.objects.update_or_create(
            restaurant=restaurant,
            defaults={
                'cgst_percentage': c_val,
                'sgst_percentage': s_val,
            }
        )

        return restaurant


def safe_delete_branch(branch):
    """
    Safely deletes a RestaurantBranch instance and all associated records,
    clearing or removing protected child relations (OrderItems, Tables, ChallanItems, PurchaseOrderItems)
    so that ProtectedError is never raised.
    """
    if not branch or not branch.pk:
        return

    from django.db import models, transaction
    from django.apps import apps

    branch_id = branch.id

    try:
        with transaction.atomic():
            def get_m(app_label, model_name):
                try:
                    return apps.get_model(app_label, model_name)
                except Exception:
                    return None

            # 1. Unlink branch admins
            RestaurantUser = get_m('super_admin', 'RestaurantUser')
            if RestaurantUser:
                RestaurantUser.objects.filter(branch_id=branch_id).update(branch=None)

            # 2. Delete Order items and Orders (OrderItem has PROTECT on MenuItem)
            OrderItem = get_m('core', 'OrderItem')
            Order = get_m('core', 'Order')
            if OrderItem:
                OrderItem.objects.filter(
                    models.Q(order__branch_id=branch_id) |
                    models.Q(menu_item__branch_id=branch_id)
                ).delete()
            if Order:
                Order.objects.filter(branch_id=branch_id).delete()

            # 3. Delete Challan items & Challans (DeliveryChallanItem has PROTECT on InventoryItem)
            DeliveryChallanItem = get_m('core', 'DeliveryChallanItem')
            DeliveryChallan = get_m('core', 'DeliveryChallan')
            if DeliveryChallanItem:
                DeliveryChallanItem.objects.filter(
                    models.Q(inventory_item__branch_id=branch_id) |
                    models.Q(menu_item__branch_id=branch_id) |
                    models.Q(challan__from_branch_id=branch_id) |
                    models.Q(challan__to_branch_id=branch_id)
                ).delete()
            if DeliveryChallan:
                DeliveryChallan.objects.filter(
                    models.Q(from_branch_id=branch_id) |
                    models.Q(to_branch_id=branch_id)
                ).delete()

            # 4. Delete Purchase Order items & Purchase Orders (PurchaseOrderItem has PROTECT on InventoryItem)
            PurchaseOrderItem = get_m('core', 'PurchaseOrderItem')
            PurchaseOrder = get_m('core', 'PurchaseOrder')
            if PurchaseOrderItem:
                PurchaseOrderItem.objects.filter(
                    models.Q(purchase_order__branch_id=branch_id) |
                    models.Q(inventory_item__branch_id=branch_id)
                ).delete()
            if PurchaseOrder:
                PurchaseOrder.objects.filter(branch_id=branch_id).delete()

            # 5. Delete Inventory Transactions and Ingredients
            InventoryTransaction = get_m('core', 'InventoryTransaction')
            if InventoryTransaction:
                InventoryTransaction.objects.filter(
                    models.Q(inventory_item__branch_id=branch_id) |
                    models.Q(order__branch_id=branch_id) |
                    models.Q(purchase_order__branch_id=branch_id)
                ).delete()

            MenuItemIngredient = get_m('core', 'MenuItemIngredient')
            if MenuItemIngredient:
                MenuItemIngredient.objects.filter(
                    models.Q(menu_item__branch_id=branch_id) |
                    models.Q(inventory_item__branch_id=branch_id)
                ).delete()

            # 6. Delete Tables (Table has PROTECT on Zone)
            Table = get_m('core', 'Table')
            if Table:
                Table.objects.filter(
                    models.Q(branch_id=branch_id) |
                    models.Q(zone__branch_id=branch_id)
                ).delete()

            # 7. Delete Menu Items and Categories
            MenuItem = get_m('core', 'MenuItem')
            if MenuItem:
                MenuItem.objects.filter(branch_id=branch_id).delete()

            SubCategory = get_m('core', 'SubCategory')
            if SubCategory:
                try:
                    SubCategory.objects.filter(category__branch_id=branch_id).delete()
                except Exception:
                    pass

            Category = get_m('core', 'Category')
            if Category:
                Category.objects.filter(branch_id=branch_id).delete()

            # 8. Delete Inventory Items & Categories
            InventoryItem = get_m('core', 'InventoryItem')
            if InventoryItem:
                InventoryItem.objects.filter(branch_id=branch_id).delete()

            InventoryCategory = get_m('core', 'InventoryCategory')
            if InventoryCategory:
                InventoryCategory.objects.filter(branch_id=branch_id).delete()

            # 9. Delete Zones
            Zone = get_m('core', 'Zone')
            if Zone:
                Zone.objects.filter(branch_id=branch_id).delete()

            # 10. Delete auxiliary branch configurations & documents
            for app_name, mname in [
                ('core', 'Party'),
                ('core', 'Supplier'),
                ('core', 'AdvanceReceipt'),
                ('core', 'Coupon'),
                ('core', 'CustomCharge'),
                ('core', 'RestaurantSettings'),
                ('core', 'DocumentSequenceSetting'),
                ('super_admin', 'DocumentSeries'),
                ('reservations', 'Reservation'),
            ]:
                mod = get_m(app_name, mname)
                if mod:
                    try:
                        mod.objects.filter(branch_id=branch_id).delete()
                    except Exception:
                        pass

            # 11. Finally, delete the branch itself
            RestaurantBranch = get_m('super_admin', 'RestaurantBranch')
            if RestaurantBranch:
                RestaurantBranch.objects.filter(id=branch_id).delete()

    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("Failed safe deletion of branch %s: %s", branch_id, e)
        # Fallback: force delete branch directly
        try:
            RestaurantBranch = apps.get_model('super_admin', 'RestaurantBranch')
            RestaurantBranch.objects.filter(id=branch_id).delete()
        except Exception:
            pass


class RestaurantAdminUpdateForm(forms.Form):
    username = forms.CharField(max_length=150, label="Username*")
    email = forms.EmailField(required=False, label="Email Address")
    password = forms.CharField(widget=forms.PasswordInput, required=False, label="New Password (optional)")
    restaurant = forms.ModelChoiceField(queryset=Restaurant.objects.all(), label="Assigned Business / Restaurant*")
    branch = forms.ModelChoiceField(queryset=RestaurantBranch.objects.all(), required=False, label="Assigned Branch Outlet (Optional)", help_text="Optional: Restrict admin to a specific branch")
    is_primary_admin = forms.BooleanField(required=False, initial=False, label="Designate as Primary Admin")
    is_2fa_enabled = forms.BooleanField(required=False, initial=True, label="Enable Two-Factor Authentication (OTP on Login)")

    def __init__(self, *args, **kwargs):
        self.instance: RestaurantUser = kwargs.pop('instance', None)
        super().__init__(*args, **kwargs)
        if self.instance:
            self.fields['username'].initial = self.instance.user.username
            self.fields['email'].initial = self.instance.user.email
            self.fields['restaurant'].initial = self.instance.restaurant
            self.fields['branch'].initial = self.instance.branch
            self.fields['is_primary_admin'].initial = self.instance.is_primary_admin

            self.initial['username'] = self.instance.user.username
            self.initial['email'] = self.instance.user.email
            self.initial['restaurant'] = self.instance.restaurant
            self.initial['branch'] = self.instance.branch
            self.initial['is_primary_admin'] = self.instance.is_primary_admin

            try:
                sec_setting = UserSecuritySetting.objects.filter(user=self.instance.user).first()
                is_enabled = sec_setting.is_2fa_enabled if sec_setting is not None else True
            except Exception:
                is_enabled = True

            self.fields['is_2fa_enabled'].initial = is_enabled
            self.initial['is_2fa_enabled'] = is_enabled

    def clean_username(self):
        username = self.cleaned_data.get('username')
        qs = User.objects.filter(username__iexact=username)
        if self.instance:
            qs = qs.exclude(pk=self.instance.user.pk)
        if qs.exists():
            raise forms.ValidationError('Username already exists')
        return username

    def clean(self):
        cleaned_data = super().clean()
        is_2fa = cleaned_data.get("is_2fa_enabled")
        email = (cleaned_data.get("email") or "").strip()
        if is_2fa and not email:
            self.add_error("email", "An email address is required when Two-Factor Authentication (OTP) is enabled.")
        return cleaned_data

    def save(self):
        user = self.instance.user
        user.username = self.cleaned_data['username']
        user.email = (self.cleaned_data.get('email') or '').strip()
        pwd = self.cleaned_data.get('password')
        if pwd:
            user.set_password(pwd)
        user.save()

        self.instance.restaurant = self.cleaned_data['restaurant']
        self.instance.branch = self.cleaned_data.get('branch')
        self.instance.is_primary_admin = self.cleaned_data.get('is_primary_admin', False)
        self.instance.save()

        is_2fa = bool(self.cleaned_data.get('is_2fa_enabled', False))
        try:
            sec_setting, _ = UserSecuritySetting.objects.update_or_create(
                user=user,
                defaults={'is_2fa_enabled': is_2fa}
            )
            if hasattr(user, '_security_setting_cache'):
                user._security_setting_cache = sec_setting
        except Exception:
            pass

        return self.instance



class RestaurantLandingForm(forms.ModelForm):
    class Meta:
        model = RestaurantLanding
        fields = [
            'logo', 'favicon', 'hero_title', 'hero_subtitle', 'hero_image', 'hero_video',
            'cta_text', 'cta_link',
            'about_heading', 'about_italic', 'about_bullet_1', 'about_bullet_2', 'about_bullet_3', 'about_body', 'about_image',
            'about_video_url',
            'about_stat_num_1', 'about_stat_label_1', 'about_stat_desc_1',
            'about_stat_num_2', 'about_stat_label_2', 'about_stat_desc_2',
            'about_stat_num_3', 'about_stat_label_3', 'about_stat_desc_3',
            'about_stat_num_4', 'about_stat_label_4', 'about_stat_desc_4',
            'address', 'phone', 'email', 'opening_hours',
            'seo_title', 'seo_description', 'seo_keywords', 'seo_canonical_url',
            'og_title', 'og_description', 'og_url', 'og_site_name', 'og_locale', 'og_type',
            'twitter_card_type', 'twitter_title', 'twitter_description', 'pages_seo', 'pages_faq',
            'show_hero', 'show_about', 'show_why_us', 'show_specials', 'show_events', 'show_gallery', 'show_team', 'show_reviews',
            'why_us_title_1', 'why_us_description_1',
            'why_us_title_2', 'why_us_description_2',
            'why_us_title_3', 'why_us_description_3',
            'map_iframe_url',
            'privacy_policy_title', 'privacy_policy_updated_at', 'privacy_policy_content',
            'terms_of_service_title', 'terms_of_service_updated_at', 'terms_of_service_content',
            'facebook_url', 'instagram_url', 'twitter_url', 'youtube_url'
        ]


class GalleryImageForm(forms.ModelForm):
    class Meta:
        model = GalleryImage
        fields = ['image', 'caption', 'branch', 'display_order']

    def __init__(self, *args, **kwargs):
        restaurant = kwargs.pop('restaurant', None)
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and 'image' in self.fields:
            self.fields['image'].required = False
        if 'branch' in self.fields:
            self.fields['branch'].required = False
            self.fields['branch'].empty_label = "All Outlets / Global"
            if restaurant:
                self.fields['branch'].queryset = restaurant.branches.all()


class TeamMemberForm(forms.ModelForm):
    class Meta:
        model = TeamMember
        fields = ['photo', 'name', 'position', 'branch', 'display_order', 'active']

    def __init__(self, *args, **kwargs):
        restaurant = kwargs.pop('restaurant', None)
        super().__init__(*args, **kwargs)
        if 'branch' in self.fields:
            self.fields['branch'].required = False
            self.fields['branch'].empty_label = "All Outlets / Global"
            if restaurant:
                self.fields['branch'].queryset = restaurant.branches.all()


class ReviewForm(forms.ModelForm):
    class Meta:
        model = Review
        fields = ['name', 'stars', 'description', 'branch', 'display_order', 'active']

    def __init__(self, *args, **kwargs):
        restaurant = kwargs.pop('restaurant', None)
        super().__init__(*args, **kwargs)
        if 'branch' in self.fields:
            self.fields['branch'].required = False
            self.fields['branch'].empty_label = "All Outlets / Global"
            if restaurant:
                self.fields['branch'].queryset = restaurant.branches.all()


class EventForm(forms.ModelForm):
    class Meta:
        model = Event
        fields = ['title', 'price', 'image', 'branch', 'description', 'bullet_1', 'bullet_2', 'bullet_3', 'display_order', 'active']

    def __init__(self, *args, **kwargs):
        restaurant = kwargs.pop('restaurant', None)
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and 'image' in self.fields:
            self.fields['image'].required = False
        if 'branch' in self.fields:
            self.fields['branch'].required = False
            self.fields['branch'].empty_label = "All Outlets / Global"
            if restaurant:
                self.fields['branch'].queryset = restaurant.branches.all()


class WhyChooseUsForm(forms.ModelForm):
    class Meta:
        model = WhyChooseUs
        fields = ['title', 'description', 'branch', 'icon', 'display_order', 'active']

    def __init__(self, *args, **kwargs):
        restaurant = kwargs.pop('restaurant', None)
        super().__init__(*args, **kwargs)
        if 'branch' in self.fields:
            self.fields['branch'].required = False
            self.fields['branch'].empty_label = "All Outlets / Global"
            if restaurant:
                self.fields['branch'].queryset = restaurant.branches.all()


class BranchContactSocialForm(forms.ModelForm):
    class Meta:
        model = RestaurantBranch
        fields = [
            'phone', 'email', 'opening_hours', 'address_line_1', 'address_line_2',
            'city', 'state', 'pincode', 'map_iframe_url',
            'facebook_url', 'instagram_url', 'twitter_url', 'youtube_url'
        ]


class BlogPostForm(forms.ModelForm):
    class Meta:
        model = BlogPost
        fields = [
            'title', 'slug', 'category', 'author', 'cover_image', 'shape_pattern',
            'excerpt', 'intro_content', 'section_1_title', 'section_1_text', 'quote',
            'section_2_title', 'section_2_text', 'gallery_image_1', 'gallery_image_2',
            'tags', 'seo_title', 'seo_description', 'is_published', 'display_order'
        ]


class AdminSetupForm(forms.Form):
    first_name = forms.CharField(max_length=150, required=False, label="First Name")
    last_name = forms.CharField(max_length=150, required=False, label="Last Name")
    username = forms.CharField(max_length=150, required=True, label="Username")
    email = forms.EmailField(required=False, label="Email Address", disabled=True)
    is_2fa_enabled = forms.BooleanField(required=False, initial=True, label="Enable Two-Factor Authentication (OTP)")

    old_password = forms.CharField(widget=forms.PasswordInput, required=False, label="Current Password")
    new_password = forms.CharField(widget=forms.PasswordInput, required=False, label="New Password")
    confirm_password = forms.CharField(widget=forms.PasswordInput, required=False, label="Confirm New Password")

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('instance_user', None)
        self.request = kwargs.pop('request', None)
        super().__init__(*args, **kwargs)

        if self.user:
            self.fields['first_name'].initial = self.user.first_name
            self.fields['last_name'].initial = self.user.last_name
            self.fields['username'].initial = self.user.username
            self.fields['email'].initial = self.user.email

            self.initial['first_name'] = self.user.first_name
            self.initial['last_name'] = self.user.last_name
            self.initial['username'] = self.user.username
            self.initial['email'] = self.user.email

            try:
                sec_setting = UserSecuritySetting.objects.filter(user=self.user).first()
                is_enabled = sec_setting.is_2fa_enabled if sec_setting is not None else True
            except Exception:
                is_enabled = True

            self.fields['is_2fa_enabled'].initial = is_enabled
            self.initial['is_2fa_enabled'] = is_enabled

    def clean_username(self):
        username = self.cleaned_data.get('username')
        if not username:
            raise forms.ValidationError("Username cannot be blank.")
        qs = User.objects.filter(username__iexact=username)
        if self.user:
            qs = qs.exclude(pk=self.user.pk)
        if qs.exists():
            raise forms.ValidationError("This username is already in use by another administrator.")
        return username

    def clean(self):
        cleaned_data = super().clean()
        old_password = cleaned_data.get('old_password')
        new_password = cleaned_data.get('new_password')
        confirm_password = cleaned_data.get('confirm_password')

        # Check if user wants to change password
        if old_password or new_password or confirm_password:
            if not old_password:
                self.add_error('old_password', "Please enter your current password to set a new password.")
            elif not self.user.check_password(old_password):
                self.add_error('old_password', "Current password is incorrect.")

            if not new_password:
                self.add_error('new_password', "Please enter a new password.")
            elif len(new_password) < 6:
                self.add_error('new_password', "New password must be at least 6 characters long.")

            if new_password and confirm_password and new_password != confirm_password:
                self.add_error('confirm_password', "New passwords do not match.")

        return cleaned_data

    def save(self):
        if not self.user:
            return None

        self.user.first_name = self.cleaned_data.get('first_name', '')
        self.user.last_name = self.cleaned_data.get('last_name', '')
        self.user.username = self.cleaned_data.get('username', self.user.username)

        new_password = self.cleaned_data.get('new_password')
        if new_password:
            self.user.set_password(new_password)

        self.user.save()

        # Update or create 2FA setting
        is_2fa = bool(self.cleaned_data.get('is_2fa_enabled', False))
        try:
            sec_setting, _ = UserSecuritySetting.objects.update_or_create(
                user=self.user,
                defaults={'is_2fa_enabled': is_2fa}
            )
            if hasattr(self.user, '_security_setting_cache'):
                self.user._security_setting_cache = sec_setting
        except Exception:
            pass

        # Keep the user session active if password was updated
        if new_password and self.request:
            from django.contrib.auth import update_session_auth_hash
            update_session_auth_hash(self.request, self.user)

        return self.user


