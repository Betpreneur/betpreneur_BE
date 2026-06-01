from django.core.management.base import BaseCommand, CommandError

from apps.algo.services import algo_runner_service


class Command(BaseCommand):
    help = "Recover an interrupted algo run by optionally rescoring failed fixtures and republishing picks from DB state."

    def add_arguments(self, parser):
        parser.add_argument("--run-id", type=int, required=True)
        parser.add_argument(
            "--rescore-failed",
            action="store_true",
            help="Synchronously rescore fixtures currently marked pending or failed before publishing.",
        )

    def handle(self, *args, **options):
        run_id = options["run_id"]
        try:
            result = algo_runner_service.recover_fanout_run(
                run_id,
                rescore_failed=options["rescore_failed"],
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(
            "Recovered algo run "
            f"{result['run_id']} ({result['target_date']}): "
            f"status={result['status']} picks={result['picks_count']} "
            f"bankers={result['bankers']} value_gems={result['value_gems']} "
            f"wild_cards={result['wild_cards']}"
        ))
