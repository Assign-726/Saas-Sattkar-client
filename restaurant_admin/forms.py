from django import forms
from core.models import MenuItem, Category, Order, SubCategory, RestaurantSettings, InventoryItem, InventoryCategory, Coupon
from super_admin.models import RestaurantBranch


class MenuItemForm(forms.ModelForm):
    category = forms.ModelChoiceField(
        queryset=Category.objects.all(),
        required=False,
        label='Category (optional)',
        help_text='Select a category (optional)',
        empty_label='-- Select Category --'
    )
    
    class Meta:
        model = MenuItem
        fields = ['name', 'description', 'price', 'tax_type', 'is_tax_inclusive', 'category', 'subcategory', 'image', 'availability']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'description': forms.Textarea(attrs={'rows': 3, 'class': 'form-control'}),
            'price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'tax_type': forms.Select(attrs={'class': 'form-select'}),
            'category': forms.Select(attrs={'class': 'form-select'}),
            'subcategory': forms.Select(attrs={'class': 'form-select'}),
            'availability': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
    
    def __init__(self, *args, **kwargs):
        self.restaurant = kwargs.pop('restaurant', None)
        self.branch = kwargs.pop('branch', None)
        super().__init__(*args, **kwargs)
        # Limit categories to current restaurant (and branch if set)
        if self.restaurant is not None:
            cat_qs = Category.objects.filter(restaurant=self.restaurant)
            if self.branch:
                cat_qs = cat_qs.filter(branch=self.branch)
            self.fields['category'].queryset = cat_qs.order_by('name')
        self.fields['category'].required = False
        self.fields['category'].label = 'Category (optional)'
        self.fields['category'].help_text = 'Select a category (optional)'
        self.fields['subcategory'].required = False
        self.fields['subcategory'].label = 'Subcategory (optional)'
        self.fields['tax_type'].label = 'Tax Type / Rate'
        self.fields['tax_type'].help_text = 'Select GST Food for meals/soft drinks, or Beer/Spirits/Wine VAT for alcoholic drinks.'
        
        # Set initial category if editing an existing item with a subcategory
        if self.instance and self.instance.subcategory:
            self.fields['category'].initial = self.instance.subcategory.category
        
        # Update subcategory queryset based on selected category
        if 'category' in self.data:
            try:
                category_id = int(self.data.get('category'))
                self.fields['subcategory'].queryset = SubCategory.objects.filter(category_id=category_id)
            except (ValueError, TypeError):
                pass
        elif self.instance.pk and self.instance.subcategory:
            self.fields['subcategory'].queryset = self.instance.subcategory.category.subcategories.all()
        else:
            self.fields['subcategory'].queryset = SubCategory.objects.none()
            
        # Add class to style the form fields
        for field in self.fields:
            self.fields[field].widget.attrs.update({'class': 'form-control'})
        self.fields['availability'].widget.attrs.update({'class': 'form-check-input'})
        self.fields['is_tax_inclusive'].widget.attrs.update({'class': 'form-check-input'})

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.restaurant is not None:
            instance.restaurant = self.restaurant
        # Always stamp the active branch so newly created items are branch-scoped
        if self.branch is not None:
            instance.branch = self.branch
        if commit:
            instance.save()
        return instance

    def clean(self):
        cleaned = super().clean()
        category = cleaned.get('category')
        subcategory = cleaned.get('subcategory')
        if subcategory and category and subcategory.category_id != category.id:
            self.add_error('subcategory', 'Selected subcategory does not belong to the chosen category.')
        return cleaned


class CouponForm(forms.ModelForm):
    class Meta:
        model = Coupon
        fields = [
            'code', 'description', 'discount_type', 'value', 'min_spend',
            'start_at', 'end_at', 'active', 'max_uses'
        ]
        widgets = {
            'description': forms.Textarea(attrs={'rows': 2}),
            'start_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
            'end_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        }

    def __init__(self, *args, **kwargs):
        self.restaurant = kwargs.pop('restaurant', None)
        self.branch = kwargs.pop('branch', None)
        super().__init__(*args, **kwargs)
        for f in self.fields:
            widget = self.fields[f].widget
            css = widget.attrs.get('class', '')
            widget.attrs['class'] = (css + ' form-control').strip()

    def clean(self):
        cleaned_data = super().clean()
        code = (cleaned_data.get('code') or '').strip().upper()
        if code:
            cleaned_data['code'] = code
            qs = Coupon.objects.filter(code__iexact=code)
            if self.restaurant:
                qs = qs.filter(restaurant=self.restaurant)
                if self.branch:
                    qs = qs.filter(branch=self.branch)
            else:
                qs = qs.filter(restaurant__isnull=True)
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                self.add_error('code', 'A coupon with this code already exists for this branch.')
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.restaurant is not None:
            instance.restaurant = self.restaurant
        if self.branch is not None:
            instance.branch = self.branch
        if commit:
            instance.save()
        return instance


class InventoryCategoryForm(forms.ModelForm):
    class Meta:
        model = InventoryCategory
        fields = ['name', 'parent', 'tax_configuration', 'taxability_type', 'hsn_sac_code', 'gst_rate']
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': 'e.g. Dairy, Beverages, Packaging'}),
            'tax_configuration': forms.Select(attrs={'class': 'form-select'}),
            'hsn_sac_code': forms.TextInput(attrs={'placeholder': 'e.g. 0401, 2202'}),
            'gst_rate': forms.NumberInput(attrs={'step': '0.01', 'placeholder': '0.00'}),
        }

    def __init__(self, *args, **kwargs):
        self.restaurant = kwargs.pop('restaurant', None)
        self.branch = kwargs.pop('branch', None)
        super().__init__(*args, **kwargs)

        parents = InventoryCategory.objects.all()
        if self.restaurant:
            parents = parents.filter(restaurant=self.restaurant)
        if self.branch:
            parents = parents.filter(branch=self.branch)
        if self.instance and self.instance.pk:
            parents = parents.exclude(pk=self.instance.pk)

        self.fields['parent'].queryset = parents
        self.fields['parent'].empty_label = "-- None (Top-Level Category) --"
        self.fields['parent'].required = False
        self.fields['tax_configuration'].required = False
        self.fields['tax_configuration'].initial = 'inherit'
        self.fields['taxability_type'].required = False
        self.fields['gst_rate'].required = False
        self.fields['hsn_sac_code'].required = False

        if not (self.instance and self.instance.pk and self.instance.tax_configuration == 'override'):
            self.initial['gst_rate'] = None

        for f, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs['class'] = 'form-check-input'
            elif isinstance(widget, forms.Select):
                widget.attrs['class'] = 'form-select'
            else:
                css = widget.attrs.get('class', '')
                widget.attrs['class'] = (css + ' form-control').strip()

    def save(self, commit=True):
        instance = super().save(commit=False)
        if not instance.tax_configuration:
            instance.tax_configuration = 'inherit'
        if instance.tax_configuration == 'inherit':
            instance.gst_rate = None
            instance.hsn_sac_code = None
            instance.taxability_type = None
            instance.default_gst_rate = None
            instance.default_hsn_code = None
            instance.taxability = None
        else:
            if instance.gst_rate is not None:
                instance.default_gst_rate = instance.gst_rate
            if instance.hsn_sac_code:
                instance.default_hsn_code = instance.hsn_sac_code
            if instance.taxability_type:
                instance.taxability = instance.taxability_type

        if self.restaurant is not None:
            instance.restaurant = self.restaurant
        if self.branch is not None:
            instance.branch = self.branch
        if commit:
            instance.save()
        return instance


class InventoryItemForm(forms.ModelForm):
    category = forms.ModelChoiceField(
        queryset=InventoryCategory.objects.none(),
        required=False,
        empty_label="-- Select Inventory Category (Optional) --"
    )
    unit = forms.ChoiceField(
        choices=[
            ('kg', 'Kilogram (kg)'),
            ('gm', 'Gram (gm)'),
            ('litre', 'Litre (L)'),
            ('ml', 'Millilitre (ml)'),
            ('piece', 'Piece'),
            ('pack', 'Pack'),
            ('box', 'Box'),
            ('crate', 'Crate'),
            ('bottle', 'Bottle'),
        ],
        required=True
    )
    gst_rate = forms.ChoiceField(
        choices=[
            ('', '-- Select Rate or Inherit --'),
            ('0.00', '0% GST'),
            ('5.00', '5% GST'),
            ('12.00', '12% GST'),
            ('18.00', '18% GST'),
            ('28.00', '28% GST'),
        ],
        required=False
    )

    class Meta:
        model = InventoryItem
        fields = [
            'name', 'sku', 'category', 'unit', 'reorder_level',
            'cost_price', 'price_per_unit', 'selling_price',
            'tax_configuration', 'taxability_type', 'hsn_sac_code', 'gst_rate', 'hsn_description',
            'is_active', 'quantity'
        ]
        widgets = {
            'tax_configuration': forms.Select(attrs={'class': 'form-select'}),
        }

    def __init__(self, *args, **kwargs):
        self.restaurant = kwargs.pop('restaurant', None)
        self.branch = kwargs.pop('branch', None)
        super().__init__(*args, **kwargs)

        cats = InventoryCategory.objects.all()
        if self.restaurant:
            cats = cats.filter(restaurant=self.restaurant)
        if self.branch:
            cats = cats.filter(branch=self.branch)
        self.fields['category'].queryset = cats
        self.fields['category'].required = False

        self.fields['cost_price'].required = False
        self.fields['price_per_unit'].required = False
        self.fields['selling_price'].required = False
        self.fields['tax_configuration'].required = False
        self.fields['tax_configuration'].initial = 'inherit'
        self.fields['taxability_type'].required = False
        self.fields['hsn_sac_code'].required = False
        self.fields['gst_rate'].required = False

        for f, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs['class'] = 'form-check-input'
            elif isinstance(widget, forms.Select):
                widget.attrs['class'] = 'form-select'
            else:
                css = widget.attrs.get('class', '')
                widget.attrs['class'] = (css + ' form-control').strip()

    def save(self, commit=True):
        instance = super().save(commit=False)
        if not instance.tax_configuration:
            instance.tax_configuration = 'inherit'
        if self.restaurant is not None:
            instance.restaurant = self.restaurant
        if self.branch is not None:
            instance.branch = self.branch
        if instance.cost_price and not instance.price_per_unit:
            instance.price_per_unit = instance.cost_price
        elif instance.price_per_unit and not instance.cost_price:
            instance.cost_price = instance.price_per_unit
        if commit:
            instance.save()
        return instance

class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ['name', 'description', 'image']
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': 'e.g. Starters, Main Course, Beverages, Desserts'}),
            'description': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Optional brief description of this category...'}),
        }

    def __init__(self, *args, **kwargs):
        self.restaurant = kwargs.pop('restaurant', None)
        self.branch = kwargs.pop('branch', None)
        super().__init__(*args, **kwargs)
        for f, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs['class'] = 'form-check-input'
            elif isinstance(widget, forms.Select):
                widget.attrs['class'] = 'form-select'
            else:
                css = widget.attrs.get('class', '')
                widget.attrs['class'] = (css + ' form-control').strip()

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.restaurant is not None:
            instance.restaurant = self.restaurant
        if self.branch is not None:
            instance.branch = self.branch
        if commit:
            instance.save()
        return instance

class OrderStatusForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = ['status']
        widgets = {
            'status': forms.Select(attrs={'class': 'form-select'}),
        }

class SubCategoryForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        self.restaurant = kwargs.pop('restaurant', None)
        super().__init__(*args, **kwargs)
        # Scope category choices to restaurant
        if self.restaurant is not None:
            self.fields['category'].queryset = Category.objects.filter(restaurant=self.restaurant).order_by('name')
        else:
            self.fields['category'].queryset = Category.objects.all().order_by('name')

    class Meta:
        model = SubCategory
        fields = ['name', 'category']
        widgets = {
            'category': forms.Select(attrs={'class': 'form-select'}),
        }
        
    

import urllib.request
import base64

def verify_razorpay_credentials(key_id, key_secret):
    url = "https://api.razorpay.com/v1/orders?count=1"
    req = urllib.request.Request(url)
    auth_str = f"{key_id}:{key_secret}"
    auth_b64 = base64.b64encode(auth_str.encode('utf-8')).decode('utf-8')
    req.add_header("Authorization", f"Basic {auth_b64}")
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status == 200
    except Exception as e:
        if hasattr(e, 'code') and e.code == 401:
            return False
        if hasattr(e, 'code') and e.code != 401:
            return True
        return False

class RestaurantSettingsForm(forms.ModelForm):
    class Meta:
        model = RestaurantSettings
        fields = [
            'cgst_percentage', 'sgst_percentage', 'service_charge_percentage', 'notification_sound',
            'enable_customer_ordering', 'enable_online_payment', 'enable_pay_at_counter', 'razorpay_key_id', 'razorpay_key_secret'
        ]
        widgets = {
            'cgst_percentage': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'sgst_percentage': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'service_charge_percentage': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'notification_sound': forms.ClearableFileInput(attrs={'accept': 'audio/*'}),
            'razorpay_key_id': forms.TextInput(attrs={'placeholder': 'rzp_test_...'}),
            'razorpay_key_secret': forms.PasswordInput(render_value=True, attrs={'placeholder': 'Razorpay Key Secret'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in ['cgst_percentage', 'sgst_percentage', 'service_charge_percentage', 'razorpay_key_id', 'razorpay_key_secret']:
            if field in self.fields:
                self.fields[field].widget.attrs.update({'class': 'form-control'})
        for field in ['enable_customer_ordering', 'enable_online_payment', 'enable_pay_at_counter']:
            if field in self.fields:
                self.fields[field].widget.attrs.update({'class': 'form-check-input'})

    def clean(self):
        cleaned = super().clean()
        for field in ['cgst_percentage', 'sgst_percentage', 'service_charge_percentage']:
            val = cleaned.get(field)
            if val is not None and val < 0:
                self.add_error(field, 'Value cannot be negative.')
        
        enable_online = cleaned.get('enable_online_payment')
        enable_counter = cleaned.get('enable_pay_at_counter')
        key_id = cleaned.get('razorpay_key_id')
        key_secret = cleaned.get('razorpay_key_secret')

        if not enable_online and not enable_counter:
            raise forms.ValidationError("You must enable at least one payment method (Online Payment or Pay at Counter).")

        if enable_online:
            if not key_id:
                self.add_error('razorpay_key_id', 'Razorpay Key ID is required to enable online payments.')
            if not key_secret:
                self.add_error('razorpay_key_secret', 'Razorpay Key Secret is required to enable online payments.')
            
            if key_id and key_secret:
                if not verify_razorpay_credentials(key_id, key_secret):
                    raise forms.ValidationError("Failed to verify Razorpay credentials. Please check your Key ID and Key Secret.")
        
        return cleaned

from core.models import Zone, Table

class ZoneForm(forms.ModelForm):
    class Meta:
        model = Zone
        fields = ['name']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
        }
    
    def __init__(self, *args, **kwargs):
        self.restaurant = kwargs.pop('restaurant', None)
        self.branch = kwargs.pop('branch', None)
        super().__init__(*args, **kwargs)

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.restaurant:
            instance.restaurant = self.restaurant
        if self.branch is not None:
            instance.branch = self.branch
        if commit:
            instance.save()
        return instance

class TableForm(forms.ModelForm):
    class Meta:
        model = Table
        fields = ['name', 'table_number', 'capacity', 'zone', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'table_number': forms.NumberInput(attrs={'class': 'form-control'}),
            'capacity': forms.NumberInput(attrs={'class': 'form-control'}),
            'zone': forms.Select(attrs={'class': 'form-select'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def __init__(self, *args, **kwargs):
        self.restaurant = kwargs.pop('restaurant', None)
        self.branch = kwargs.pop('branch', None)
        super().__init__(*args, **kwargs)
        # Scope zone choices to current restaurant / branch
        if self.restaurant is not None:
            from django.db.models import Q
            zone_qs = Zone.objects.filter(restaurant=self.restaurant)
            if self.branch:
                zone_qs = zone_qs.filter(Q(branch=self.branch) | Q(branch__isnull=True))
            self.fields['zone'].queryset = zone_qs.order_by('name')

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.restaurant is not None:
            instance.restaurant = self.restaurant
        if self.branch is not None:
            instance.branch = self.branch
        if commit:
            instance.save()
        return instance


class RestaurantBranchComplianceForm(forms.ModelForm):
    class Meta:
        model = RestaurantBranch
        fields = [
            'fssai_license_no', 'vat_tin', 'excise_license_no',
            'vat_beer_rate', 'vat_imfl_rate', 'vat_wine_rate'
        ]
        widgets = {
            'fssai_license_no': forms.TextInput(attrs={'placeholder': '14-digit FSSAI License No.'}),
            'vat_tin': forms.TextInput(attrs={'placeholder': 'State VAT TIN'}),
            'excise_license_no': forms.TextInput(attrs={'placeholder': 'State Excise License No.'}),
            'vat_beer_rate': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'vat_imfl_rate': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'vat_wine_rate': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields:
            self.fields[field].widget.attrs.update({'class': 'form-control'})
            self.fields[field].required = False

