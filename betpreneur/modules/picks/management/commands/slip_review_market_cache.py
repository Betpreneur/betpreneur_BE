import json

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Max, Min
from django.utils import timezone

from betpreneur.modules.catalog.api import SlipReviewMarketCache
from betpreneur.modules.picks.services.runner_service import algo_runner_service
from betpreneur.modules.picks.tasks import (
    build_slip_review_market_cache,
    cleanup_slip_review_market_cache,
)


class Command(BaseCommand):
    help = "Inspect, build, or clean the private slip-review market cache."

    def add_arguments(self, parser):
        parser.add_argument(
            "action",
            choices=("status", "build", "cleanup"),
            help="Operation to perform.",
        )
        parser.add_argument("--start-date", default="", help="Build start date in YYYY-MM-DD format.")
        parser.add_argument("--days", type=int, default=None, help="Build horizon in days.")
        parser.add_argument("--max-fixtures", type=int, default=None, help="Maximum fixtures to build.")
        parser.add_argument("--force", action="store_true", help="Force rebuilding fresh cached fixtures.")
        parser.add_argument(
            "--no-sync-fixtures",
            action="store_true",
            help="Do not sync the StatPal fixture horizon before building.",
        )
        parser.add_argument(
            "--inline",
            action="store_true",
            help="Run build/cleanup inline instead of queueing Celery.",
        )
        parser.add_argument("--grace-seconds", type=int, default=None, help="Cleanup grace period.")
        parser.add_argument("--limit", type=int, default=None, help="Cleanup deletion limit.")

    def handle(self, *args, **options):
        action = options["action"]
        if action == "status":
            payload = self._status()
        elif action == "build":
            payload = self._build(options)
        elif action == "cleanup":
            payload = self._cleanup(options)
        else:
            raise CommandError(f"Unsupported action: {action}")
        self.stdout.write(json.dumps(payload, indent=2, sort_keys=True, default=str))

    def _status(self):
        now = timezone.now()
        base = SlipReviewMarketCache.objects.filter(cache_scope=SlipReviewMarketCache.Scope.SLIP_REVIEW)
        aggregate = base.aggregate(
            rows=Count("id"),
            fixtures=Count("match_id", distinct=True),
            markets=Count("market", distinct=True),
            earliest_expiry=Min("expires_at"),
            latest_expiry=Max("expires_at"),
            latest_update=Max("updated_at"),
        )
        by_date = list(
            base.values("match_date")
            .annotate(rows=Count("id"), fixtures=Count("match_id", distinct=True))
            .order_by("-match_date")[:10]
        )
        by_family = list(
            base.values("market_family")
            .annotate(rows=Count("id"), fixtures=Count("match_id", distinct=True))
            .order_by("-rows")[:15]
        )
        return {
            "status": "ok",
            "now": now.isoformat(),
            "totals": {
                **aggregate,
                "expired_rows": base.filter(expires_at__lte=now).count(),
                "fresh_rows": base.filter(expires_at__gt=now).count(),
            },
            "by_date": by_date,
            "by_market_family": by_family,
        }

    def _build(self, options):
        kwargs = {
            "days": options["days"],
            "sync_fixtures": not options["no_sync_fixtures"],
            "force": bool(options["force"]),
            "max_fixtures": options["max_fixtures"],
        }
        if options["start_date"]:
            kwargs["start_date"] = options["start_date"]
        if options["inline"]:
            return algo_runner_service.build_slip_review_market_cache(**kwargs)
        task = build_slip_review_market_cache.delay(**kwargs)
        return {
            "status": "queued",
            "task_id": task.id,
            "task": build_slip_review_market_cache.name,
            "kwargs": kwargs,
        }

    def _cleanup(self, options):
        kwargs = {
            "grace_seconds": options["grace_seconds"],
            "limit": options["limit"],
        }
        if options["inline"]:
            return algo_runner_service.cleanup_slip_review_market_cache(**kwargs)
        task = cleanup_slip_review_market_cache.delay(**kwargs)
        return {
            "status": "queued",
            "task_id": task.id,
            "task": cleanup_slip_review_market_cache.name,
            "kwargs": kwargs,
        }
