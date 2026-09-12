import os
import io
from PIL import Image, ImageOps
from django.core.management.base import BaseCommand
from django.core.files.base import ContentFile
from django.conf import settings

from core.models import (
    RestaurantLanding,
    GalleryImage,
    TeamMember,
    Event,
    BlogPost,
    MenuItem,
    Category,
)
from core.signals import WEBP_CONVERSION_MAP
from core.image_utils import convert_image_to_webp


class Command(BaseCommand):
    help = "Scan existing database image records and convert legacy JPG/PNG files to highly compressed .webp files."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Simulate conversion without making changes to files or database.',
        )
        parser.add_argument(
            '--quality',
            type=int,
            default=82,
            help='WebP compression quality (1-100, default: 82).',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        quality = options['quality']

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Starting WebP Image Conversion {'(DRY RUN)' if dry_run else ''}..."
        ))

        total_scanned = 0
        total_converted = 0
        total_errors = 0
        total_orig_bytes = 0
        total_new_bytes = 0

        for model_cls, fields_config in WEBP_CONVERSION_MAP.items():
            model_name = model_cls.__name__
            records = model_cls.objects.all()

            for record in records:
                for field_name, max_dim in fields_config:
                    total_scanned += 1
                    field_file = getattr(record, field_name, None)
                    if not field_file or not field_file.name:
                        continue

                    # If already a .webp file, skip
                    if field_file.name.lower().endswith('.webp'):
                        continue

                    # Check if file physically exists
                    if not field_file.storage.exists(field_file.name):
                        continue

                    try:
                        orig_size = field_file.size
                        total_orig_bytes += orig_size
                    except Exception:
                        orig_size = 0

                    if dry_run:
                        self.stdout.write(
                            f"  [DRY-RUN] Would convert: {model_name} #{record.pk} [{field_name}] -> {field_file.name} ({orig_size / 1024:.1f} KB)"
                        )
                        total_converted += 1
                        continue

                    try:
                        # Open and convert
                        with field_file.open('rb') as f:
                            webp_content = convert_image_to_webp(
                                f,
                                max_dimension=max_dim,
                                quality=quality,
                                preserve_if_webp=False
                            )

                        if not webp_content or webp_content == field_file:
                            continue

                        old_path = field_file.name
                        new_bytes = len(webp_content)
                        total_new_bytes += new_bytes

                        # Save new file via storage
                        dir_name = os.path.dirname(old_path)
                        base_name = os.path.splitext(os.path.basename(old_path))[0]
                        new_rel_path = os.path.join(dir_name, f"{base_name}.webp").replace('\\', '/')

                        saved_path = field_file.storage.save(new_rel_path, webp_content)

                        # Update model record without triggering full signals
                        setattr(record, field_name, saved_path)
                        model_cls.objects.filter(pk=record.pk).update(**{field_name: saved_path})

                        # Delete old uncompressed file
                        try:
                            if old_path != saved_path and field_file.storage.exists(old_path):
                                field_file.storage.delete(old_path)
                        except Exception as e:
                            self.stdout.write(self.style.WARNING(f"    Warning: Could not delete old file {old_path}: {e}"))

                        saved_kb = (orig_size - new_bytes) / 1024
                        total_converted += 1

                        self.stdout.write(self.style.SUCCESS(
                            f"  [OK] {model_name} #{record.pk} [{field_name}]: "
                            f"{os.path.basename(old_path)} ({orig_size/1024:.1f}KB) -> "
                            f"{os.path.basename(saved_path)} ({new_bytes/1024:.1f}KB) [Saved {saved_kb:.1f}KB]"
                        ))

                    except Exception as exc:
                        total_errors += 1
                        self.stdout.write(self.style.ERROR(
                            f"  [ERROR] converting {model_name} #{record.pk} [{field_name}]: {exc}"
                        ))

        # Summary output
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(self.style.MIGRATE_LABEL("CONVERSION SUMMARY:"))
        self.stdout.write(f"  Fields Inspected : {total_scanned}")
        self.stdout.write(f"  Files Converted  : {total_converted}")
        self.stdout.write(f"  Errors           : {total_errors}")

        if not dry_run and total_orig_bytes > 0:
            saved_bytes = total_orig_bytes - total_new_bytes
            saved_mb = saved_bytes / (1024 * 1024)
            pct = (saved_bytes / total_orig_bytes) * 100 if total_orig_bytes > 0 else 0
            self.stdout.write(self.style.SUCCESS(
                f"  Disk Space Saved : {saved_mb:.2f} MB ({pct:.1f}% reduction)"
            ))
        self.stdout.write("=" * 60 + "\n")
