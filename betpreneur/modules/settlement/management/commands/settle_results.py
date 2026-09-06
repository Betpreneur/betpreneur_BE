from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from betpreneur.modules.picks.api import MarketPrediction
from betpreneur.modules.settlement.api import settlement_service


STAT_MARKET_FILTER = (
    Q(market__startswith="Corners ")
    | Q(market__startswith="Home Team Corners ")
    | Q(market__startswith="Away Team Corners ")
    | Q(market__startswith="Cards ")
    | Q(market__startswith="Home Team Cards ")
    | Q(market__startswith="Away Team Cards ")
    | Q(market__startswith="Shots On Target ")
    | Q(market__startswith="Home Team Shots On Target ")
    | Q(market__startswith="Away Team Shots On Target ")
)


class Command(BaseCommand):
    help = "Settle daily picks and internal market predictions for a date."

    def add_arguments(self, parser):
        parser.add_argument("--date", required=True, help="Date to settle, e.g. 2026-09-04.")
        parser.add_argument(
            "--reprocess-void-stat-markets",
            action="store_true",
            help="Move void card/corner/SOT market predictions back to pending before settling.",
        )

    def handle(self, *args, **options):
        try:
            target_date = date.fromisoformat(options["date"])
        except ValueError as exc:
            raise CommandError("--date must be in YYYY-MM-DD format") from exc

        reset_count = 0
        if options["reprocess_void_stat_markets"]:
            reset_count = (
                MarketPrediction.objects.filter(
                    match_date=target_date,
                    status=MarketPrediction.Status.VOID,
                )
                .filter(STAT_MARKET_FILTER)
                .update(
                    status=MarketPrediction.Status.PENDING,
                    pnl_simulated=None,
                    score="",
                    result="",
                    settled_at=None,
                )
            )

        result = settlement_service.update_results(target_date=target_date)
        if reset_count:
            result = {**result, "reprocessed_void_stat_markets": reset_count}
        self.stdout.write(str(result))
