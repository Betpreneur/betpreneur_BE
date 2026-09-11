import json

from django.core.management.base import BaseCommand, CommandError

from betpreneur.modules.catalog.services.coach_profile_import import coach_profile_csv_importer


class Command(BaseCommand):
    help = "Create or update coach tactical profiles from a local CSV file."

    def add_arguments(self, parser):
        parser.add_argument("csv_file", help="Path to the local coach tactical-profile CSV file.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and report all rows, then roll back database changes.",
        )

    def handle(self, *args, **options):
        try:
            result = coach_profile_csv_importer.import_file(
                options["csv_file"],
                dry_run=options["dry_run"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(json.dumps(result, indent=2, default=str))
        summary = (
            f"Coach profile import: {result['created']} created, {result['updated']} updated, "
            f"{result['skipped']} skipped."
        )
        if result["errors"]:
            self.stdout.write(self.style.WARNING(summary))
        elif result["dry_run"]:
            self.stdout.write(self.style.WARNING(f"{summary} Dry run only; no changes were saved."))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
