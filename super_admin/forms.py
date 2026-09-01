from django import forms
from django.contrib.auth.models import User, Group
import re
from decimal import Decimal
from super_admin.models import Restaurant, RestaurantUser, RestaurantBranch, GstRegistration, DocumentSeries
from core.models import RestaurantLanding, GalleryImage, TeamMember, Review, Event, MenuItem, RestaurantSettings, BlogPost
from restaurant_project import constants


STATE_CHOICES = [
    ("", "Select..."),
    ("Andhra Pradesh", "Andhra Pradesh"),
    ("Arunachal Pradesh", "Arunachal Pradesh"),
    ("Assam", "Assam"),
    ("Bihar", "Bihar"),
    ("Chhattisgarh", "Chhattisgarh"),
    ("Goa", "Goa"),
    ("Gujarat", "Gujarat"),
    ("Haryana", "Haryana"),
    ("Himachal Pradesh", "Himachal Pradesh"),
    ("Jharkhand", "Jharkhand"),
    ("Karnataka", "Karnataka"),
    ("Kerala", "Kerala"),
    ("Madhya Pradesh", "Madhya Pradesh"),
    ("Maharashtra", "Maharashtra"),
    ("Manipur", "Manipur"),
    ("Meghalaya", "Meghalaya"),
    ("Mizoram", "Mizoram"),
    ("Nagaland", "Nagaland"),
    ("Odisha", "Odisha"),
    ("Punjab", "Punjab"),
    ("Rajasthan", "Rajasthan"),
    ("Sikkim", "Sikkim"),
    ("Tamil Nadu", "Tamil Nadu"),
    ("Telangana", "Telangana"),
    ("Tripura", "Tripura"),
    ("Uttar Pradesh", "Uttar Pradesh"),
    ("Uttarakhand", "Uttarakhand"),
    ("West Bengal", "West Bengal"),
    ("Andaman and Nicobar Islands", "Andaman and Nicobar Islands"),
    ("Chandigarh", "Chandigarh"),
    ("Dadra and Nagar Haveli and Daman and Diu", "Dadra and Nagar Haveli and Daman and Diu"),
    ("Delhi", "Delhi"),
    ("Jammu and Kashmir", "Jammu and Kashmir"),
    ("Ladakh", "Ladakh"),
    ("Lakshadweep", "Lakshadweep"),
    ("Puducherry", "Puducherry"),
]

BUSINESS_TYPE_CHOICES = [
    ("", "Select..."),
    ("Sole Proprietorship", "Sole Proprietorship"),
    ("Partnership", "Partnership"),
    ("Private Limited", "Private Limited Company (Pvt Ltd)"),
    ("Public Limited", "Public Limited Company"),
    ("LLP", "Limited Liability Partnership (LLP)"),
    ("OPC", "One Person Company (OPC)"),
    ("Trust/Society", "Trust / Society / NGO"),
    ("Other", "Other"),
]


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
    username = forms.CharField(max_length=150)
    email = forms.EmailField(required=False)
    password = forms.CharField(widget=forms.PasswordInput)
    restaurant = forms.ModelChoiceField(queryset=Restaurant.objects.all())
    branch = forms.ModelChoiceField(queryset=RestaurantBranch.objects.all(), required=False, help_text="Optional: Restrict admin to a specific branch")
    is_primary_admin = forms.BooleanField(required=False, initial=False)

    def clean_username(self):
        username = self.cleaned_data.get("username")
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("Username already exists")
        return username

    def save(self):
        username = self.cleaned_data["username"]
        email = self.cleaned_data.get("email", "")
        password = self.cleaned_data["password"]
        restaurant = self.cleaned_data["restaurant"]
        branch = self.cleaned_data.get("branch")
        is_primary = self.cleaned_data.get("is_primary_admin", False)

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
        return ru



class OnboardRestaurantForm(forms.Form):
    # Restaurant / Business fields
    legal_name = forms.CharField(max_length=255, label="Legal Name*")
    trade_name = forms.CharField(max_length=255, label="Trade / Brand Name*")
    business_type = forms.ChoiceField(choices=BUSINESS_TYPE_CHOICES, label="Business Type*")
    pan = forms.CharField(max_length=20, label="PAN*")
    cin_llpin = forms.CharField(max_length=50, required=False, label="CIN / LLPIN")
    restaurant_address_line_1 = forms.CharField(max_length=255, label="Address Line 1*")
    restaurant_address_line_2 = forms.CharField(max_length=255, required=False, label="Address Line 2")
    restaurant_city = forms.CharField(max_length=100, label="City*")
    restaurant_state = forms.ChoiceField(choices=STATE_CHOICES, label="State*")
    restaurant_pincode = forms.CharField(max_length=20, label="Pincode*")
    restaurant_email = forms.EmailField(label="Email*")
    restaurant_phone = forms.CharField(max_length=20, label="Phone*")
    is_gst_registered = forms.BooleanField(required=False, initial=True, label="Is your business GST registered?")
    restaurant_is_active = forms.BooleanField(required=False, initial=True, label="Active")

    def __init__(self, *args, instance=None, **kwargs):
        self.instance = instance
        super().__init__(*args, **kwargs)

    def clean_pan(self):
        pan = (self.cleaned_data.get("pan") or "").strip().upper()
        if not pan:
            return pan
        if not re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', pan):
            raise forms.ValidationError("Invalid PAN format (expected e.g. AAACS1234F)")

        btype = self.cleaned_data.get("business_type", "")
        fourth_char = pan[3]
        expected = []
        if btype == "Sole Proprietorship":
            expected = ["P"]
        elif btype in ["Partnership", "LLP"]:
            expected = ["F"]
        elif btype in ["Private Limited", "Public Limited", "OPC"]:
            expected = ["C"]
        elif btype == "Trust/Society":
            expected = ["T", "A"]

        if expected and fourth_char not in expected:
            raise forms.ValidationError(
                f"PAN 4th character '{fourth_char}' does not match Business Type '{btype}' (Expected '{'/'.join(expected)}')"
            )
        return pan

    def clean(self):
        cleaned = super().clean()
        btype = cleaned.get("business_type")
        cin = (cleaned.get("cin_llpin") or "").strip()
        if btype in ["Private Limited", "Public Limited", "OPC"] and not cin:
            self.add_error("cin_llpin", "CIN is required for Corporate Entities.")
        elif btype == "LLP" and not cin:
            self.add_error("cin_llpin", "LLPIN is required for Limited Liability Partnerships.")

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

    def save(self, instance=None, branches_data=None):
        trade_name = self.cleaned_data["trade_name"]
        legal_name = self.cleaned_data["legal_name"]
        t_name = trade_name or legal_name

        if branches_data and isinstance(branches_data, list) and constants.MAX_ALLOWED_BRANCHES > 0:
            valid_branches = [b for b in branches_data if b.get("name")]
            if len(valid_branches) > constants.MAX_ALLOWED_BRANCHES:
                raise forms.ValidationError(
                    f"Branch limit reached. Your license allows a maximum of {constants.MAX_ALLOWED_BRANCHES} branch outlets (attempted {len(valid_branches)})."
                )

        target_restaurant = instance or self.instance or Restaurant.objects.filter(name__iexact=t_name).first()
        if target_restaurant:
            restaurant = target_restaurant
            restaurant.legal_name = legal_name
            restaurant.trade_name = trade_name
            restaurant.name = t_name
            restaurant.business_type = self.cleaned_data.get("business_type", "")
            restaurant.pan = self.cleaned_data.get("pan", "")
            restaurant.cin_llpin = self.cleaned_data.get("cin_llpin", "")
            restaurant.address_line_1 = self.cleaned_data.get("restaurant_address_line_1", "")
            restaurant.address_line_2 = self.cleaned_data.get("restaurant_address_line_2", "")
            restaurant.city = self.cleaned_data.get("restaurant_city", "")
            restaurant.state = self.cleaned_data.get("restaurant_state", "")
            restaurant.pincode = self.cleaned_data.get("restaurant_pincode", "")
            restaurant.email = self.cleaned_data.get("restaurant_email", "")
            restaurant.phone = self.cleaned_data.get("restaurant_phone", "")
            restaurant.is_gst_registered = self.cleaned_data.get("is_gst_registered", False)
            restaurant.gst_type = Restaurant.GSTType.REGULAR if self.cleaned_data.get("is_gst_registered") else Restaurant.GSTType.UNREGISTERED
            restaurant.is_active = self.cleaned_data.get("restaurant_is_active", True)
            restaurant.save()
        else:
            restaurant = Restaurant.objects.create(
                legal_name=legal_name,
                trade_name=trade_name,
                name=t_name,
                business_type=self.cleaned_data.get("business_type", ""),
                pan=self.cleaned_data.get("pan", ""),
                cin_llpin=self.cleaned_data.get("cin_llpin", ""),
                address_line_1=self.cleaned_data.get("restaurant_address_line_1", ""),
                address_line_2=self.cleaned_data.get("restaurant_address_line_2", ""),
                city=self.cleaned_data.get("restaurant_city", ""),
                state=self.cleaned_data.get("restaurant_state", ""),
                pincode=self.cleaned_data.get("restaurant_pincode", ""),
                email=self.cleaned_data.get("restaurant_email", ""),
                phone=self.cleaned_data.get("restaurant_phone", ""),
                is_gst_registered=self.cleaned_data.get("is_gst_registered", False),
                gst_type=Restaurant.GSTType.REGULAR if self.cleaned_data.get("is_gst_registered") else Restaurant.GSTType.UNREGISTERED,
                is_active=self.cleaned_data.get("restaurant_is_active", True),
            )

        state_branch_counts = {}

        if branches_data and isinstance(branches_data, list):
            kept_branch_ids = []
            for bdata in branches_data:
                if not bdata.get("name"):
                    continue

                b_state = bdata.get("state") or restaurant.state
                gstin_val = (bdata.get("gstin") or "").strip().upper()
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

                is_hotel_tariff = bool(bdata.get("is_hotel_tariff_7500_plus", False))
                gst_reg_type = bdata.get("gst_registration_type", RestaurantBranch.GSTRegType.REGULAR_5)
                if is_hotel_tariff:
                    gst_reg_type = RestaurantBranch.GSTRegType.REGULAR_18
                elif gst_reg_type == RestaurantBranch.GSTRegType.REGULAR_18:
                    is_hotel_tariff = True

                if branch:
                    branch.name = bdata.get("name", "")
                    branch.phone = bdata.get("phone", "")
                    branch.use_company_address = bdata.get("use_company_address", True)
                    branch.address_line_1 = bdata.get("address_line_1", "")
                    branch.address_line_2 = bdata.get("address_line_2", "")
                    branch.city = bdata.get("city", "")
                    branch.state = b_state
                    branch.pincode = bdata.get("pincode", "")
                    branch.fssai_license_no = bdata.get("fssai_license_no", "")
                    branch.serves_alcohol = bdata.get("serves_alcohol", False)
                    branch.gstin = gstin_val
                    branch.gst_registration = gst_reg_obj
                    branch.gst_registration_type = gst_reg_type
                    branch.is_hotel_tariff_7500_plus = is_hotel_tariff
                    branch.e_invoicing_enabled = bdata.get("e_invoicing_enabled", False)
                    branch.save()
                else:
                    branch = RestaurantBranch.objects.create(
                        restaurant=restaurant,
                        gst_registration=gst_reg_obj,
                        name=bdata.get("name", ""),
                        phone=bdata.get("phone", ""),
                        use_company_address=bdata.get("use_company_address", True),
                        address_line_1=bdata.get("address_line_1", ""),
                        address_line_2=bdata.get("address_line_2", ""),
                        city=bdata.get("city", ""),
                        state=b_state,
                        pincode=bdata.get("pincode", ""),
                        fssai_license_no=bdata.get("fssai_license_no", ""),
                        serves_alcohol=bdata.get("serves_alcohol", False),
                        gstin=gstin_val,
                        gst_registration_type=gst_reg_type,
                        is_hotel_tariff_7500_plus=is_hotel_tariff,
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
                RestaurantBranch.objects.filter(restaurant=restaurant).exclude(id__in=kept_branch_ids).delete()

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




class RestaurantAdminUpdateForm(forms.Form):
    username = forms.CharField(max_length=150, label="Username*")
    email = forms.EmailField(required=False, label="Email")
    password = forms.CharField(widget=forms.PasswordInput, required=False, label="New Password (optional)")
    restaurant = forms.ModelChoiceField(queryset=Restaurant.objects.all(), label="Restaurant")
    branch = forms.ModelChoiceField(queryset=RestaurantBranch.objects.all(), required=False, label="Assigned Branch", help_text="Optional: Restrict admin to a specific branch")
    is_primary_admin = forms.BooleanField(required=False, initial=False, label="Primary admin")

    def __init__(self, *args, **kwargs):
        self.instance: RestaurantUser = kwargs.pop('instance', None)
        super().__init__(*args, **kwargs)
        if self.instance:
            self.fields['username'].initial = self.instance.user.username
            self.fields['email'].initial = self.instance.user.email
            self.fields['restaurant'].initial = self.instance.restaurant
            self.fields['branch'].initial = self.instance.branch
            self.fields['is_primary_admin'].initial = self.instance.is_primary_admin

    def clean_username(self):
        username = self.cleaned_data.get('username')
        qs = User.objects.filter(username=username)
        if self.instance:
            qs = qs.exclude(pk=self.instance.user.pk)
        if qs.exists():
            raise forms.ValidationError('Username already exists')
        return username

    def save(self):
        user = self.instance.user
        user.username = self.cleaned_data['username']
        user.email = self.cleaned_data.get('email', '')
        pwd = self.cleaned_data.get('password')
        if pwd:
            user.set_password(pwd)
        user.save()

        self.instance.restaurant = self.cleaned_data['restaurant']
        self.instance.branch = self.cleaned_data.get('branch')
        self.instance.is_primary_admin = self.cleaned_data.get('is_primary_admin', False)
        self.instance.save()
        return self.instance


class RestaurantLandingForm(forms.ModelForm):
    class Meta:
        model = RestaurantLanding
        fields = [
            'logo', 'hero_title', 'hero_subtitle', 'hero_image', 'hero_video',
            'cta_text', 'cta_link',
            'about_heading', 'about_italic', 'about_bullet_1', 'about_bullet_2', 'about_bullet_3', 'about_body', 'about_image',
            'about_video_url',
            'about_stat_num_1', 'about_stat_label_1', 'about_stat_desc_1',
            'about_stat_num_2', 'about_stat_label_2', 'about_stat_desc_2',
            'about_stat_num_3', 'about_stat_label_3', 'about_stat_desc_3',
            'about_stat_num_4', 'about_stat_label_4', 'about_stat_desc_4',
            'address', 'phone', 'email', 'opening_hours',
            'seo_title', 'seo_description',
            'show_hero', 'show_about', 'show_why_us', 'show_specials', 'show_events', 'show_gallery', 'show_team', 'show_reviews',
            'why_us_title_1', 'why_us_description_1',
            'why_us_title_2', 'why_us_description_2',
            'why_us_title_3', 'why_us_description_3',
            'map_iframe_url'
        ]


class GalleryImageForm(forms.ModelForm):
    class Meta:
        model = GalleryImage
        fields = ['image', 'caption', 'display_order']


class TeamMemberForm(forms.ModelForm):
    class Meta:
        model = TeamMember
        fields = ['photo', 'name', 'position', 'display_order', 'active']


class ReviewForm(forms.ModelForm):
    class Meta:
        model = Review
        fields = ['name', 'stars', 'description', 'display_order', 'active']


class EventForm(forms.ModelForm):
    class Meta:
        model = Event
        fields = ['title', 'price', 'image', 'description', 'bullet_1', 'bullet_2', 'bullet_3', 'display_order', 'active']


class BlogPostForm(forms.ModelForm):
    class Meta:
        model = BlogPost
        fields = [
            'title', 'slug', 'category', 'author', 'cover_image', 'shape_pattern',
            'excerpt', 'intro_content', 'section_1_title', 'section_1_text', 'quote',
            'section_2_title', 'section_2_text', 'gallery_image_1', 'gallery_image_2',
            'tags', 'seo_title', 'seo_description', 'is_published', 'display_order'
        ]

