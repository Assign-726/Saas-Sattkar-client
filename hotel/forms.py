from django import forms
from hotel.models import HotelSettings


class HotelSettingsForm(forms.ModelForm):
    class Meta:
        model = HotelSettings
        fields = [
            # General Info
            'hotel_name',
            'contact_email',
            'contact_phone',
            'address',
            'check_in_time',
            'check_out_time',
            'cancellation_grace_hours',
            'cancellation_policy',
            # Taxes & GST
            'gstin',
            'enable_dynamic_gst',
            'tax_rate',
            'service_charge_percent',
            'tax_inclusive_pricing',
            # Payment Gateway
            'enable_online_payment',
            'enable_pay_at_desk',
            'payment_gateway_provider',
            'payment_mode',
            'razorpay_key_id',
            'razorpay_key_secret',
            'stripe_publishable_key',
            'stripe_secret_key',
            'upi_id',
            # Invoicing
            'invoice_prefix',
            'invoice_footer_notes',
            'currency_symbol',
            # Custom SMTP & Mailing
            'email_is_active',
            'email_sender_name',
            'email_host_user',
            'email_host_password',
            'email_host',
            'email_port',
            'email_use_tls',
            'email_use_ssl',
        ]
        widgets = {
            'hotel_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Sharma Luxury Suites & Hotel'}),
            'contact_email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'reservations@sharmahotel.com'}),
            'contact_phone': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '+91 98765 43210'}),
            'address': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Complete property address...'}),
            'check_in_time': forms.TimeInput(attrs={'class': 'form-control', 'type': 'time'}),
            'check_out_time': forms.TimeInput(attrs={'class': 'form-control', 'type': 'time'}),
            'cancellation_grace_hours': forms.NumberInput(attrs={'class': 'form-control', 'min': '0', 'step': '1'}),
            'cancellation_policy': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Cancellation terms communicated to guests...'}),
            
            'gstin': forms.TextInput(attrs={'class': 'form-control text-uppercase font-mono', 'placeholder': 'e.g. 08AAAAA0000A1Z5', 'maxlength': '20'}),
            'enable_dynamic_gst': forms.CheckboxInput(attrs={'class': 'form-check-input', 'role': 'switch'}),
            'tax_rate': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0', 'max': '100'}),
            'service_charge_percent': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0', 'max': '100'}),
            'tax_inclusive_pricing': forms.CheckboxInput(attrs={'class': 'form-check-input', 'role': 'switch'}),

            'enable_online_payment': forms.CheckboxInput(attrs={'class': 'form-check-input', 'role': 'switch'}),
            'enable_pay_at_desk': forms.CheckboxInput(attrs={'class': 'form-check-input', 'role': 'switch'}),
            'payment_gateway_provider': forms.Select(attrs={'class': 'form-select'}),
            'payment_mode': forms.Select(attrs={'class': 'form-select'}),
            'razorpay_key_id': forms.TextInput(attrs={'class': 'form-control font-mono', 'placeholder': 'rzp_test_... or rzp_live_...'}),
            'razorpay_key_secret': forms.PasswordInput(render_value=True, attrs={'class': 'form-control font-mono', 'placeholder': 'Enter secret key...'}),
            'stripe_publishable_key': forms.TextInput(attrs={'class': 'form-control font-mono', 'placeholder': 'pk_test_... or pk_live_...'}),
            'stripe_secret_key': forms.PasswordInput(render_value=True, attrs={'class': 'form-control font-mono', 'placeholder': 'sk_test_... or sk_live_...'}),
            'upi_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. sharmahotel@okhdfcbank'}),

            'invoice_prefix': forms.TextInput(attrs={'class': 'form-control font-mono', 'placeholder': 'e.g. INV-HTL'}),
            'invoice_footer_notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Footer note printed on guest receipts...'}),
            'currency_symbol': forms.TextInput(attrs={'class': 'form-control', 'style': 'max-width: 100px;'}),

            'email_is_active': forms.CheckboxInput(attrs={'class': 'form-check-input', 'role': 'switch'}),
            'email_sender_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Sharma Luxury Suites & Hotel'}),
            'email_host_user': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'e.g. reservations@sharmahotel.com or gmail ID'}),
            'email_host_password': forms.PasswordInput(render_value=True, attrs={'class': 'form-control font-mono', 'placeholder': '16-character App Password'}),
            'email_host': forms.TextInput(attrs={'class': 'form-control font-mono', 'placeholder': 'smtp.gmail.com'}),
            'email_port': forms.NumberInput(attrs={'class': 'form-control font-mono', 'placeholder': '587'}),
            'email_use_tls': forms.CheckboxInput(attrs={'class': 'form-check-input', 'role': 'switch'}),
            'email_use_ssl': forms.CheckboxInput(attrs={'class': 'form-check-input', 'role': 'switch'}),
        }
