from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.apps import apps


class Command(BaseCommand):
    help = "Merge one product into another by reassigning all related foreign keys to the target product."

    def add_arguments(self, parser):
        parser.add_argument("--source", type=int, required=True, help="ID of the product to merge FROM")
        parser.add_argument("--target", type=int, required=True, help="ID of the product to merge INTO")
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Perform the merge. Without this flag the command only does a dry-run.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Force merge even if products belong to different companies.",
        )

    def handle(self, *args, **options):
        source_id = options["source"]
        target_id = options["target"]
        do_commit = options["commit"]
        force = options["force"]

        Product = apps.get_model("catalog", "Product")

        try:
            source = Product.objects.get(pk=source_id)
        except Product.DoesNotExist:
            raise CommandError(f"Source product {source_id} does not exist")

        try:
            target = Product.objects.get(pk=target_id)
        except Product.DoesNotExist:
            raise CommandError(f"Target product {target_id} does not exist")

        if source.pk == target.pk:
            raise CommandError("Source and target are the same product")

        if source.company_id != target.company_id and not force:
            raise CommandError(
                "Source and target belong to different companies. Use --force to override."
            )

        self.stdout.write(self.style.NOTICE(f"Preparing to merge Product {source.pk} ('{source.name}') into {target.pk} ('{target.name}')"))

        # Find all model fields that FK to Product
        relations = []
        for model in apps.get_models():
            for field in model._meta.get_fields():
                if getattr(field, "remote_field", None) and getattr(field.remote_field, "model", None) == Product:
                    # skip the Product model itself
                    if model == Product:
                        continue
                    relations.append((model, field))

        # Summarize impact
        summary = []
        for model, field in relations:
            qs = model.objects.filter(**{f"{field.name}": source})
            count = qs.count()
            if count:
                summary.append((model._meta.label, field.name, count))

        if not summary:
            self.stdout.write(self.style.WARNING("No related objects found referencing the source product."))
        else:
            self.stdout.write("Affected related objects:")
            for label, fname, cnt in summary:
                self.stdout.write(f"- {label}.{fname}: {cnt}")

        if not do_commit:
            self.stdout.write(self.style.SUCCESS("Dry-run complete. No changes made. Re-run with --commit to apply."))
            return

        # Perform the merge inside a transaction
        with transaction.atomic():
            for model, field in relations:
                qs = model.objects.filter(**{f"{field.name}": source})
                if qs.exists():
                    update_kwargs = {f"{field.name}": target}
                    updated = qs.update(**update_kwargs)
                    self.stdout.write(self.style.SUCCESS(f"Updated {updated} rows on {model._meta.label}.{field.name}"))

            # Attempt to delete the source product
            try:
                source.delete()
                self.stdout.write(self.style.SUCCESS(f"Deleted source product {source_id}"))
            except Exception as e:
                # If deletion fails, raise a CommandError so the transaction is rolled back
                raise CommandError(f"Failed to delete source product: {e}")

        self.stdout.write(self.style.SUCCESS("Merge complete."))
