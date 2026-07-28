from django import forms
from core.models import MenuItem, Category, Order, SubCategory, RestaurantSettings, InventoryItem, Coupon
from super_admin.models import RestaurantBranch


class MenuItemForm(forms.ModelForm):
    category = forms.ModelChoiceField(
        queryset=Category.objects.all(),
        required=True,
        label='Category *',
        help_text='Select a category (required)'
    )
    
    class Meta:
        model = MenuItem
        fields = ['name', 'description', 'price', 'tax_type', 'category', 'subcategory', 'image', 'availability']
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
        super().__init__(*args, **kwargs)
        # Limit categories to current restaurant
        if self.restaurant is not None:
            self.fields['category'].queryset = Category.objects.filter(restaurant=self.restaurant).order_by('name')
        # Category required (already enforced by field), subcategory optional
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

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.restaurant is not None:
            instance.restaurant = self.restaurant
        if commit:
            instance.save()
        return instance

    def clean(self):
        cleaned = super().clean()
        category = cleaned.get('category')
        subcategory = cleaned.get('subcategory')
        if not category:
            self.add_error('category', 'Category is required.')
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
            else:
                qs = qs.filter(restaurant__isnull=True)
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                self.add_error('code', 'A coupon with this code already exists for this restaurant.')
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.restaurant is not None:
            instance.restaurant = self.restaurant
        if commit:
            instance.save()
        return instance


class InventoryItemForm(forms.ModelForm):
    category = forms.ChoiceField(
        choices=[
            ('raw_ingredients', 'Raw Ingredients'),
            ('beverages', 'Beverages'),
            ('packaging', 'Packaging & Tissue'),
            ('bar_stock', 'Bar Stock'),
        ],
        required=True
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
            'taxability_type', 'hsn_sac_code', 'gst_rate', 'hsn_description',
            'is_active', 'quantity'
        ]

    def __init__(self, *args, **kwargs):
        self.restaurant = kwargs.pop('restaurant', None)
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
        if instance.cost_price and not instance.price_per_unit:
            instance.price_per_unit = instance.cost_price
        elif instance.price_per_unit and not instance.cost_price:
            instance.cost_price = instance.price_per_unit
        if commit:
            instance.save()
        return instance

class CategoryForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        self.restaurant = kwargs.pop('restaurant', None)
        super().__init__(*args, **kwargs)

    class Meta:
        model = Category
        fields = ['name', 'description', 'image']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
        }

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.restaurant is not None:
            instance.restaurant = self.restaurant
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
        super().__init__(*args, **kwargs)

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.restaurant:
            instance.restaurant = self.restaurant
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
        super().__init__(*args, **kwargs)
        if self.restaurant:
            self.fields['zone'].queryset = Zone.objects.filter(restaurant=self.restaurant).order_by('name')

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.restaurant:
            instance.restaurant = self.restaurant
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

