import os
from django.core.management.base import BaseCommand
from django.conf import settings
from django.apps import apps


class Command(BaseCommand):
    help = "Find and delete orphaned media files on disk that are no longer referenced in the database."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Simulate cleanup without deleting any files.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Scanning for orphaned media files {'(DRY RUN)' if dry_run else ''}..."
        ))

        # 1. Collect all active file paths referenced in the database
        referenced_files = set()
        for model in apps.get_models():
            file_fields = [
                f.name for f in model._meta.get_fields()
                if f.get_internal_type() in ('FileField', 'ImageField')
            ]
            if not file_fields:
                continue

            for obj in model.objects.all():
                for field_name in file_fields:
                    field_val = getattr(obj, field_name, None)
                    if field_val and field_val.name:
                        # Normalize path with forward slashes
                        clean_path = os.path.normpath(field_val.name).replace('\\', '/')
                        referenced_files.add(clean_path)

        self.stdout.write(f"  Total media files referenced in database: {len(referenced_files)}")

        # 2. Walk MEDIA_ROOT and find all files on disk
        media_root = settings.MEDIA_ROOT
        disk_files = []

        for root, dirs, files in os.walk(media_root):
            for file in files:
                # Ignore hidden files like .gitignore, .DS_Store
                if file.startswith('.'):
                    continue
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, media_root).replace('\\', '/')
                disk_files.append((rel_path, full_path))

        self.stdout.write(f"  Total media files found on disk: {len(disk_files)}")

        # 3. Identify orphans
        orphans = [
            (rel, full) for rel, full in disk_files
            if rel not in referenced_files
        ]

        if not orphans:
            self.stdout.write(self.style.SUCCESS("  [OK] No orphaned files found! All files on disk are referenced in the database."))
            return

        total_orphan_bytes = sum(os.path.getsize(full) for _, full in orphans if os.path.exists(full))
        total_orphan_mb = total_orphan_bytes / (1024 * 1024)

        self.stdout.write(self.style.WARNING(
            f"\nFound {len(orphans)} orphaned file(s) occupying {total_orphan_mb:.2f} MB of disk space:"
        ))

        deleted_count = 0
        deleted_bytes = 0

        for rel, full in orphans:
            try:
                f_size = os.path.getsize(full) if os.path.exists(full) else 0
                if dry_run:
                    self.stdout.write(f"  [DRY-RUN] Would delete: {rel} ({f_size / 1024:.1f} KB)")
                else:
                    os.remove(full)
                    deleted_count += 1
                    deleted_bytes += f_size
                    self.stdout.write(self.style.SUCCESS(f"  [DELETED] {rel} ({f_size / 1024:.1f} KB)"))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  [ERROR] Could not delete {rel}: {e}"))

        # Ensure standard media subdirectories exist
        standard_dirs = ['sounds', 'landing', 'menu_images', 'category_images', 'order_images', 'table_qrs', 'blogs']
        for sdir in standard_dirs:
            os.makedirs(os.path.join(media_root, sdir), exist_ok=True)

        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(self.style.MIGRATE_LABEL("CLEANUP SUMMARY:"))
        if dry_run:
            self.stdout.write(f"  Orphan Files Detected : {len(orphans)}")
            self.stdout.write(f"  Disk Space to Reclaim : {total_orphan_mb:.2f} MB")
        else:
            freed_mb = deleted_bytes / (1024 * 1024)
            self.stdout.write(self.style.SUCCESS(f"  Orphan Files Deleted : {deleted_count}"))
            self.stdout.write(self.style.SUCCESS(f"  Disk Space Reclaimed : {freed_mb:.2f} MB"))
        self.stdout.write("=" * 60 + "\n")
