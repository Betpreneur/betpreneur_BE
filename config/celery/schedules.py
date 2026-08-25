"""Celery beat schedule.

Lifted verbatim from the old config/celery.py so the running cadence is
unchanged; only the task paths moved to their owning modules.
"""
import os
from datetime import timedelta

from celery.schedules import crontab


def _env_bool(name, default="0"):
    return os.environ.get(name, default).lower() in {"1", "true", "yes", "on"}


BEAT_SCHEDULE = {
    "generate-daily-picks": {
        "task": "betpreneur.modules.picks.tasks.generate_daily_picks",
        "schedule": crontab(
            hour=os.environ.get("ALGO_GENERATE_HOUR", "0"),
            minute=os.environ.get("ALGO_GENERATE_MINUTE", "5"),
        ),
        "options": {"expires": timedelta(hours=6).total_seconds()},
    },
    "settle-daily-results": {
        "task": "betpreneur.modules.settlement.tasks.settle_daily_results",
        "schedule": crontab(
            hour=os.environ.get("ALGO_SETTLE_HOUR", "6"),
            minute=os.environ.get("ALGO_SETTLE_MINUTE", "30"),
        ),
        "options": {"expires": timedelta(hours=6).total_seconds()},
    },
    # Four times a day: ~1,000 calls a sweep against a 50k daily quota, keeping the
    # Match Checker's 3-day fixture window warm so resolution never hits the provider.
    "sync-fixture-horizon": {
        "task": "betpreneur.modules.catalog.tasks.sync_fixture_horizon",
        "schedule": crontab(
            hour=os.environ.get("ALGO_HORIZON_HOURS", "1,7,13,19"),
            minute=os.environ.get("ALGO_HORIZON_MINUTE", "20"),
        ),
        "options": {"expires": timedelta(hours=5).total_seconds()},
    },
    # StatPal-native data build: fixtures plus analysis snapshots for today,
    # tomorrow, and next tomorrow. Run before top-pick generation by default.
    "build-statpal-daily-cache": {
        "task": "betpreneur.modules.catalog.tasks.build_statpal_daily_cache",
        "schedule": crontab(
            hour=os.environ.get("STATPAL_DAILY_BUILD_HOUR", "23"),
            minute=os.environ.get("STATPAL_DAILY_BUILD_MINUTE", "30"),
        ),
        "kwargs": {
            "days": int(os.environ.get("STATPAL_DAILY_BUILD_DAYS", "3")),
            "include_optional": os.environ.get("STATPAL_DAILY_BUILD_OPTIONAL", "0").lower() in {"1", "true", "yes"},
            "force": os.environ.get("STATPAL_DAILY_BUILD_FORCE", "0").lower() in {"1", "true", "yes"},
        },
        "options": {"expires": timedelta(hours=8).total_seconds()},
    },
    # Private Match Checker warm cache. This can cover the broad StatPal fixture
    # universe without changing the restricted public all-games/top-picks feed.
    "build-slip-review-market-cache": {
        "task": "betpreneur.modules.picks.tasks.build_slip_review_market_cache",
        "schedule": crontab(
            hour=os.environ.get("SLIP_REVIEW_MARKET_CACHE_BUILD_HOURS", "0,12"),
            minute=os.environ.get("SLIP_REVIEW_MARKET_CACHE_BUILD_MINUTE", "40"),
        ),
        "kwargs": {
            "days": int(os.environ.get("SLIP_REVIEW_MARKET_CACHE_BUILD_DAYS", "3")),
            "sync_fixtures": _env_bool("SLIP_REVIEW_MARKET_CACHE_BUILD_SYNC_FIXTURES", "1"),
            "force": _env_bool("SLIP_REVIEW_MARKET_CACHE_BUILD_FORCE", "0"),
            "max_fixtures": int(os.environ.get("SLIP_REVIEW_MARKET_CACHE_BUILD_MAX_FIXTURES", "0")),
        },
        "options": {"expires": timedelta(hours=11).total_seconds()},
    },
    # Every 15 minutes: team sheets only firm up in the hour before kickoff, and a
    # confirmed omission is what turns a priced player prop into a dead bet.
    "refresh-imminent-lineups": {
        "task": "betpreneur.modules.scoring.tasks.refresh_imminent_lineups",
        "schedule": crontab(minute=os.environ.get("ALGO_LINEUP_MINUTES", "*/15")),
        "options": {"expires": timedelta(minutes=14).total_seconds()},
    },
    "recover-stale-slip-reviews": {
        "task": "betpreneur.modules.slips.tasks.recover_stale_slip_reviews",
        "schedule": crontab(minute=os.environ.get("SLIP_REVIEW_RECOVERY_MINUTES", "*/5")),
        "kwargs": {
            "stale_after_seconds": int(os.environ.get("SLIP_REVIEW_STALE_AFTER_SECONDS", "1200")),
            "limit": int(os.environ.get("SLIP_REVIEW_RECOVERY_LIMIT", "25")),
        },
        "options": {"expires": timedelta(minutes=4).total_seconds()},
    },
    "refill-daily-free-tokens": {
        "task": "betpreneur.modules.billing.tasks.refill_daily_free_tokens",
        "schedule": crontab(
            hour=os.environ.get("TOKEN_FREE_REFILL_HOUR", "0"),
            minute=os.environ.get("TOKEN_FREE_REFILL_MINUTE", "15"),
        ),
        "options": {"expires": timedelta(hours=6).total_seconds()},
    },
    "expire-token-reservations": {
        "task": "betpreneur.modules.billing.tasks.expire_token_reservations",
        "schedule": crontab(minute=os.environ.get("TOKEN_RESERVATION_EXPIRY_MINUTES", "*/5")),
        "kwargs": {
            "limit": int(os.environ.get("TOKEN_RESERVATION_EXPIRY_LIMIT", "200")),
        },
        "options": {"expires": timedelta(minutes=4).total_seconds()},
    },
    "cleanup-slip-review-market-cache": {
        "task": "betpreneur.modules.picks.tasks.cleanup_slip_review_market_cache",
        "schedule": crontab(minute=os.environ.get("SLIP_REVIEW_MARKET_CACHE_CLEANUP_MINUTES", "55")),
        "kwargs": {
            "grace_seconds": int(os.environ.get("SLIP_REVIEW_MARKET_CACHE_CLEANUP_GRACE_SECONDS", "0")),
            "limit": int(os.environ.get("SLIP_REVIEW_MARKET_CACHE_CLEANUP_LIMIT", "0")),
        },
        "options": {"expires": timedelta(minutes=50).total_seconds()},
    },
    # Hourly: a late fitness call is what turns a priced player prop into a dead bet.
    "refresh-player-availability": {
        "task": "betpreneur.modules.scoring.tasks.refresh_player_availability",
        "schedule": crontab(minute=os.environ.get("ALGO_AVAILABILITY_MINUTE", "5")),
        "options": {"expires": timedelta(minutes=50).total_seconds()},
    },
    "fit-score-models": {
        "task": "betpreneur.modules.scoring.tasks.fit_score_models",
        "schedule": crontab(
            hour=os.environ.get("ALGO_FIT_MODELS_HOUR", "4"),
            minute=os.environ.get("ALGO_FIT_MODELS_MINUTE", "30"),
        ),
        "options": {"expires": timedelta(hours=8).total_seconds()},
    },
    "settle-slip-selections": {
        "task": "betpreneur.modules.settlement.tasks.settle_slip_selections",
        "schedule": crontab(
            hour=os.environ.get("ALGO_SLIP_SETTLE_HOUR", "7"),
            minute=os.environ.get("ALGO_SLIP_SETTLE_MINUTE", "0"),
        ),
        "options": {"expires": timedelta(hours=6).total_seconds()},
    },
    "run-monthly-auditor": {
        "task": "betpreneur.modules.analytics.tasks.run_monthly_auditor",
        "schedule": crontab(
            day_of_month=os.environ.get("ALGO_AUDITOR_DAY", "1"),
            hour=os.environ.get("ALGO_AUDITOR_HOUR", "8"),
            minute=os.environ.get("ALGO_AUDITOR_MINUTE", "0"),
        ),
        "options": {"expires": timedelta(hours=12).total_seconds()},
    },
}

if not _env_bool("SLIP_REVIEW_MARKET_CACHE_BUILD_ENABLED", "1"):
    BEAT_SCHEDULE.pop("build-slip-review-market-cache", None)
if not _env_bool("SLIP_REVIEW_MARKET_CACHE_CLEANUP_ENABLED", "1"):
    BEAT_SCHEDULE.pop("cleanup-slip-review-market-cache", None)
