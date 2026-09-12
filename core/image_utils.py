import io
import os
import logging
from PIL import Image, ImageOps
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import UploadedFile

logger = logging.getLogger(__name__)

# Standard maximum dimensions for responsive web delivery
DEFAULT_MAX_DIMENSION = 1920
DEFAULT_WEBP_QUALITY = 82


def convert_image_to_webp(
    field_file,
    max_dimension=DEFAULT_MAX_DIMENSION,
    quality=DEFAULT_WEBP_QUALITY,
    preserve_if_webp=True
):
    """
    Converts any standard image file (JPEG, PNG, etc.) to a highly compressed WebP format.

    Features:
    - Auto-corrects EXIF rotation (so mobile photos don't upload sideways).
    - Downscales oversized photos preserving aspect ratio (max dimension e.g. 1920px).
    - Preserves RGBA transparency for logos and transparent graphics.
    - Saves in WebP format with high quality (default 82%) and compression method 6.
    - Updates the file extension to .webp.

    Args:
        field_file: A Django FieldFile or UploadedFile.
        max_dimension: Integer max width/height in pixels.
        quality: WebP compression quality (1-100, recommended 80-85).
        preserve_if_webp: If True, skips conversion if already a WebP image.

    Returns:
        ContentFile: A Django ContentFile with the .webp image, or the original file if conversion is skipped/fails.
    """
    if not field_file:
        return field_file

    file_name = getattr(field_file, 'name', '') or 'image.webp'
    base_name, ext = os.path.splitext(file_name)
    ext_lower = ext.lower()

    # Skip files that shouldn't be processed as WebP
    if ext_lower in ('.ico', '.svg', '.gif'):
        return field_file

    try:
        # Open image using Pillow
        # Ensure file pointer is at the beginning
        if hasattr(field_file, 'seek'):
            field_file.seek(0)

        img = Image.open(field_file)

        # If already WebP and preserve_if_webp is enabled, check if resizing is needed
        if img.format == 'WEBP' and preserve_if_webp:
            # If dimensions are within limits, leave as-is
            if max(img.size) <= max_dimension:
                if hasattr(field_file, 'seek'):
                    field_file.seek(0)
                return field_file

        # Auto-orient based on EXIF metadata (crucial for photos taken on iPhones/Androids)
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass

        # Resize if oversized while preserving aspect ratio
        orig_width, orig_height = img.size
        if max(orig_width, orig_height) > max_dimension:
            img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
            logger.info(
                f"Downscaled image from {orig_width}x{orig_height} to {img.size[0]}x{img.size[1]}"
            )

        # Transparency handling
        # If image has an alpha channel or transparency palette, convert to RGBA to preserve transparency
        if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
            converted_img = img.convert('RGBA')
        else:
            converted_img = img.convert('RGB')

        # Encode to WebP in memory
        output_io = io.BytesIO()
        converted_img.save(
            output_io,
            format='WEBP',
            quality=quality,
            method=6,  # Best compression effort
        )
        output_io.seek(0)

        # In case base_name has subdirectories (e.g. landing/hero/xyz)
        base_pure = os.path.basename(base_name)
        new_clean_name = f"{base_pure}.webp"

        content_file = ContentFile(output_io.getvalue(), name=new_clean_name)
        return content_file

    except Exception as exc:
        logger.warning(f"Could not convert image '{file_name}' to WebP: {exc}. Retaining original file.")
        if hasattr(field_file, 'seek'):
            field_file.seek(0)
        return field_file
