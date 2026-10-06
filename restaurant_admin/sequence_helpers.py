from django.db import transaction as db_transaction
from django.db.models import F, Q
from django.utils import timezone
from django.core.exceptions import ValidationError
from core.models import DocumentSequenceSetting, Order, PurchaseOrder, AdvanceReceipt, DeliveryChallan


def get_document_sequence_info(restaurant, document_type, branch=None):
    """
    Returns sequence configuration and next formatted voucher number for display in forms.
    """
    if not restaurant:
        return {
            'mode': 'manual',
            'prefix': '',
            'suffix': '',
            'padding_digits': 3,
            'valid_from': '',
            'valid_to': '',
            'current_counter': 1,
            'next_number': '',
            'readonly': False,
            'required': True,
        }

    setting = DocumentSequenceSetting.get_sequence_setting(restaurant, document_type, branch=branch)
    today = timezone.now().date()

    # Check date validity
    is_date_valid = True
    if setting.valid_from and today < setting.valid_from:
        is_date_valid = False
    if setting.valid_to and today > setting.valid_to:
        is_date_valid = False

    next_num = ""
    if setting.generation_mode in ['automatic', 'manual_override'] and is_date_valid:
        next_num = setting.format_number()

    return {
        'mode': setting.generation_mode,
        'prefix': setting.prefix or '',
        'suffix': setting.suffix or '',
        'padding_digits': setting.padding_digits,
        'valid_from': setting.valid_from.strftime('%Y-%m-%d') if setting.valid_from else '',
        'valid_to': setting.valid_to.strftime('%Y-%m-%d') if setting.valid_to else '',
        'current_counter': setting.current_counter,
        'next_number': next_num,
        'readonly': setting.generation_mode == 'automatic',
        'required': setting.generation_mode == 'manual',
        'is_date_valid': is_date_valid,
    }


def validate_unique_document_number(restaurant, document_type, number, branch=None, exclude_pk=None):
    """
    Enforces strict database-level validation to reject duplicate voucher numbers for the active outlet/branch.
    """
    if not restaurant or not number:
        return

    clean_number = str(number).strip()
    if not clean_number:
        return

    duplicate = False

    if document_type == 'sales':
        qs = Order.objects.filter(restaurant=restaurant)
        if branch:
            qs = qs.filter(branch=branch)
        qs = qs.filter(Q(gstin_no__iexact=clean_number) | Q(excise_bill_no__iexact=clean_number))
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        duplicate = qs.exists()

    elif document_type == 'vat_bill':
        qs = Order.objects.filter(restaurant=restaurant)
        if branch:
            qs = qs.filter(branch=branch)
        qs = qs.filter(excise_bill_no__iexact=clean_number)
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        duplicate = qs.exists()

    elif document_type == 'purchase':
        qs = PurchaseOrder.objects.filter(restaurant=restaurant)
        if branch:
            qs = qs.filter(branch=branch)
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        duplicate = qs.filter(Q(po_number__iexact=clean_number) | Q(invoice_number__iexact=clean_number)).exists()

    elif document_type == 'receipt':
        qs = AdvanceReceipt.objects.filter(restaurant=restaurant, receipt_no__iexact=clean_number)
        if branch:
            qs = qs.filter(branch=branch)
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        duplicate = qs.exists()

    elif document_type == 'challan':
        qs = DeliveryChallan.objects.filter(restaurant=restaurant, challan_no__iexact=clean_number)
        if branch:
            qs = qs.filter(from_branch=branch)
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        duplicate = qs.exists()

    if duplicate:
        doc_label = dict(DocumentSequenceSetting.DocumentType.choices).get(document_type, document_type)
        branch_name = f" in {branch.name}" if branch else ""
        raise ValidationError(f"Duplicate voucher number error: '{clean_number}' already exists for {doc_label}{branch_name}.")


def get_and_increment_document_number(restaurant, document_type, branch=None, user_input_no=None, exclude_pk=None):
    """
    Safely retrieves the document number and increments current_counter using DB lock and F() expressions.
    """
    if not restaurant:
        clean_input = (user_input_no or '').strip()
        if not clean_input:
            raise ValidationError("Voucher number is required.")
        return clean_input

    with db_transaction.atomic():
        lookup_kwargs = {
            'restaurant': restaurant,
            'branch': branch,
            'document_type': document_type,
        }
        setting, _ = DocumentSequenceSetting.objects.select_for_update().get_or_create(
            **lookup_kwargs,
            defaults={
                'generation_mode': DocumentSequenceSetting.GenerationMode.AUTOMATIC,
                'prefix': DocumentSequenceSetting.DEFAULT_PREFIXES.get(document_type, ''),
                'suffix': '',
                'padding_digits': 3,
                'current_counter': 1,
            }
        )

        today = timezone.now().date()
        if setting.valid_from and today < setting.valid_from:
            raise ValidationError(f"Sequence configuration for {setting.get_document_type_display()} is not valid until {setting.valid_from}.")
        if setting.valid_to and today > setting.valid_to:
            raise ValidationError(f"Sequence configuration for {setting.get_document_type_display()} expired on {setting.valid_to}.")

        mode = setting.generation_mode
        final_number = ""

        if mode == DocumentSequenceSetting.GenerationMode.MANUAL:
            clean_input = (user_input_no or '').strip()
            if not clean_input:
                raise ValidationError(f"Voucher number is required for {setting.get_document_type_display()} in Manual mode.")
            final_number = clean_input

        elif mode == DocumentSequenceSetting.GenerationMode.AUTOMATIC:
            clean_input = (user_input_no or '').strip()
            if clean_input:
                final_number = clean_input
                expected_auto = setting.format_number(setting.current_counter)
                if clean_input == expected_auto:
                    DocumentSequenceSetting.objects.filter(pk=setting.pk).update(current_counter=F('current_counter') + 1)
                    setting.refresh_from_db()
            else:
                final_number = setting.format_number(setting.current_counter)
                DocumentSequenceSetting.objects.filter(pk=setting.pk).update(current_counter=F('current_counter') + 1)
                setting.refresh_from_db()

        elif mode == DocumentSequenceSetting.GenerationMode.MANUAL_OVERRIDE:
            clean_input = (user_input_no or '').strip()
            expected_auto = setting.format_number(setting.current_counter)
            if clean_input:
                final_number = clean_input
                if clean_input == expected_auto:
                    DocumentSequenceSetting.objects.filter(pk=setting.pk).update(current_counter=F('current_counter') + 1)
                    setting.refresh_from_db()
            else:
                final_number = expected_auto
                DocumentSequenceSetting.objects.filter(pk=setting.pk).update(current_counter=F('current_counter') + 1)
                setting.refresh_from_db()

        # Strict duplicate check before returning
        validate_unique_document_number(restaurant, document_type, final_number, branch=branch, exclude_pk=exclude_pk)
        return final_number
