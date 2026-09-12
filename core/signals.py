import logging
import os
from django.db.models.signals import post_save, pre_save, post_delete
from django.dispatch import receiver
from django.core.files.uploadedfile import UploadedFile

from core.models import (
    Order,
    RestaurantLanding,
    RestaurantSettings,
    GalleryImage,
    TeamMember,
    Event,
    BlogPost,
    MenuItem,
    Category,
)
from core.sse import publish
from core.image_utils import convert_image_to_webp

logger = logging.getLogger(__name__)


# ── ORDER NOTIFICATION SIGNAL ────────────────────────────────────────────────
@receiver(post_save, sender=Order)
def order_created_signal(sender, instance: Order, created: bool, **kwargs):
    try:
        restaurant = instance.restaurant
        slug = getattr(restaurant, 'slug', None)
        if not slug:
            return
        # Publish only on create
        if created:
            publish(slug, {
                'type': 'new_order',
                'order_id': instance.id,
                'at': instance.order_time.isoformat() if instance.order_time else ''
            })
    except Exception:
        # best-effort, do not break request cycle
        pass


# ── CONFIGURATION FOR AUTO-WEBP AND AUTO-DELETE ──────────────────────────────
# Image fields that will be automatically converted to .webp on upload
WEBP_CONVERSION_MAP = {
    RestaurantLanding: [
        ('logo', 800),
        ('hero_image', 1920),
        ('about_image', 1920),
    ],
    GalleryImage: [
        ('image', 1920),
    ],
    TeamMember: [
        ('photo', 1000),
    ],
    Event: [
        ('image', 1920),
    ],
    BlogPost: [
        ('cover_image', 1920),
        ('gallery_image_1', 1920),
        ('gallery_image_2', 1920),
    ],
    MenuItem: [
        ('image', 1200),
    ],
    Category: [
        ('image', 1200),
    ],
}

# ALL media fields (images, video, audio, favicons) that must automatically
# delete their previous file on replacement, clearing, or model deletion
ALL_MEDIA_FIELDS_MAP = {
    RestaurantSettings: ['notification_sound'],
    RestaurantLanding: ['logo', 'favicon', 'hero_image', 'hero_video', 'about_image'],
    GalleryImage: ['image'],
    TeamMember: ['photo'],
    Event: ['image'],
    BlogPost: ['cover_image', 'gallery_image_1', 'gallery_image_2'],
    MenuItem: ['image'],
    Category: ['image'],
}


def _safe_delete_storage_file(storage, file_name, exclude_instance_pk=None, model_cls=None, field_name=None):
    """
    Safely delete a file from storage, ensuring no other record in the same table is pointing to it.
    """
    if not file_name or not storage.exists(file_name):
        return

    # Check if another record in this model still references the same file
    if model_cls and field_name and exclude_instance_pk:
        other_exists = model_cls.objects.filter(**{field_name: file_name}).exclude(pk=exclude_instance_pk).exists()
        if other_exists:
            logger.info(f"Skipping deletion of {file_name}: shared with another record.")
            return

    try:
        storage.delete(file_name)
        logger.info(f"Successfully deleted replaced/orphaned file: {file_name}")
    except Exception as e:
        logger.warning(f"Could not delete file {file_name}: {e}")


def _handle_media_pre_save(sender, instance, **kwargs):
    """
    Unified pre-save hook for all media-holding models:
    1. If an existing media file is REPLACED or CLEARED, deletes the old file from disk.
    2. If an image field received a new upload, converts it to optimized WebP.
    """
    media_fields = ALL_MEDIA_FIELDS_MAP.get(sender, [])
    webp_fields_dict = dict(WEBP_CONVERSION_MAP.get(sender, []))

    if not media_fields:
        return

    old_instance = None
    if instance.pk:
        try:
            old_instance = sender.objects.filter(pk=instance.pk).first()
        except Exception:
            old_instance = None

    for field_name in media_fields:
        try:
            current_field_file = getattr(instance, field_name, None)
            has_current_file = bool(current_field_file)
            current_name = getattr(current_field_file, 'name', '') or ''

            old_field_file = getattr(old_instance, field_name, None) if old_instance else None
            old_name = getattr(old_field_file, 'name', '') or ''

            # Check if this is a newly assigned/uploaded file
            is_new_upload = False
            if has_current_file:
                try:
                    file_obj = getattr(current_field_file, 'file', None)
                    is_new_upload = (
                        isinstance(file_obj, UploadedFile) or
                        (hasattr(current_field_file, '_committed') and not current_field_file._committed)
                    )
                except Exception:
                    is_new_upload = (hasattr(current_field_file, '_committed') and not current_field_file._committed)

            # ── 1. CLEAN UP PREVIOUS FILE ON REPLACEMENT OR CLEAR ────────────
            if old_instance and old_name:
                # File was cleared (now empty) OR a new file is uploaded to replace it
                is_cleared = not has_current_file or not current_name
                is_replaced = is_new_upload and (old_name != current_name)

                if is_cleared or is_replaced:
                    _safe_delete_storage_file(
                        old_field_file.storage,
                        old_name,
                        exclude_instance_pk=instance.pk,
                        model_cls=sender,
                        field_name=field_name
                    )

            # ── 2. CONVERT IMAGES TO WEBP ────────────────────────────────────
            if is_new_upload and current_field_file and (field_name in webp_fields_dict):
                max_dim = webp_fields_dict[field_name]
                webp_content = convert_image_to_webp(current_field_file, max_dimension=max_dim)
                if webp_content and webp_content != current_field_file:
                    setattr(instance, field_name, webp_content)
                    logger.info(f"Converted '{field_name}' on {sender.__name__} to WebP.")

        except Exception as exc:
            logger.error(f"Error handling media field '{field_name}' on {sender.__name__}: {exc}", exc_info=True)


def _handle_media_post_delete(sender, instance, **kwargs):
    """
    Post-delete hook to delete physical files from storage when records are removed.
    """
    media_fields = ALL_MEDIA_FIELDS_MAP.get(sender, [])
    if not media_fields:
        return

    for field_name in media_fields:
        try:
            field_file = getattr(instance, field_name, None)
            if field_file and field_file.name:
                _safe_delete_storage_file(
                    field_file.storage,
                    field_file.name,
                    exclude_instance_pk=instance.pk,
                    model_cls=sender,
                    field_name=field_name
                )
        except Exception as e:
            logger.warning(f"Error cleaning up file on {sender.__name__} deletion: {e}")


# Register pre_save and post_delete hooks for all media-holding models
for model_cls in ALL_MEDIA_FIELDS_MAP.keys():
    pre_save.connect(_handle_media_pre_save, sender=model_cls, weak=False)
    post_delete.connect(_handle_media_post_delete, sender=model_cls, weak=False)
