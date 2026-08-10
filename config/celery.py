import os
from datetime import timedelta

from celery import Celery
from celery.schedules import crontab


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("betpreneur")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    "generate-daily-picks": {
        "task": "apps.algo.tasks.generate_daily_picks",
        "schedule": crontab(
            hour=os.environ.get("ALGO_GENERATE_HOUR", "0"),
            minute=os.environ.get("ALGO_GENERATE_MINUTE", "5"),
        ),
        "options": {"expires": timedelta(hours=6).total_seconds()},
    },
    "settle-daily-results": {
        "task": "apps.algo.tasks.settle_daily_results",
        "schedule": crontab(
            hour=os.environ.get("ALGO_SETTLE_HOUR", "6"),
            minute=os.environ.get("ALGO_SETTLE_MINUTE", "30"),
        ),
        "options": {"expires": timedelta(hours=6).total_seconds()},
    },
    # Four times a day: ~1,000 calls a sweep against a 50k daily quota, keeping the
    # Match Checker's 3-day fixture window warm so resolution never hits the provider.
    "sync-fixture-horizon": {
        "task": "apps.algo.tasks.sync_fixture_horizon",
        "schedule": crontab(
            hour=os.environ.get("ALGO_HORIZON_HOURS", "1,7,13,19"),
            minute=os.environ.get("ALGO_HORIZON_MINUTE", "20"),
        ),
        "options": {"expires": timedelta(hours=5).total_seconds()},
    },
    # StatPal-native data build: fixtures plus analysis snapshots for today,
    # tomorrow, and next tomorrow. Run before top-pick generation by default.
    "build-statpal-daily-cache": {
        "task": "apps.algo.tasks.build_statpal_daily_cache",
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
    # Every 15 minutes: team sheets only firm up in the hour before kickoff, and a
    # confirmed omission is what turns a priced player prop into a dead bet.
    "refresh-imminent-lineups": {
        "task": "apps.algo.tasks.refresh_imminent_lineups",
        "schedule": crontab(minute=os.environ.get("ALGO_LINEUP_MINUTES", "*/15")),
        "options": {"expires": timedelta(minutes=14).total_seconds()},
    },
    # Hourly: a late fitness call is what turns a priced player prop into a dead bet.
    "refresh-player-availability": {
        "task": "apps.algo.tasks.refresh_player_availability",
        "schedule": crontab(minute=os.environ.get("ALGO_AVAILABILITY_MINUTE", "5")),
        "options": {"expires": timedelta(minutes=50).total_seconds()},
    },
    "fit-score-models": {
        "task": "apps.algo.tasks.fit_score_models",
        "schedule": crontab(
            hour=os.environ.get("ALGO_FIT_MODELS_HOUR", "4"),
            minute=os.environ.get("ALGO_FIT_MODELS_MINUTE", "30"),
        ),
        "options": {"expires": timedelta(hours=8).total_seconds()},
    },
    "settle-slip-selections": {
        "task": "apps.algo.tasks.settle_slip_selections",
        "schedule": crontab(
            hour=os.environ.get("ALGO_SLIP_SETTLE_HOUR", "7"),
            minute=os.environ.get("ALGO_SLIP_SETTLE_MINUTE", "0"),
        ),
        "options": {"expires": timedelta(hours=6).total_seconds()},
    },
    "run-monthly-auditor": {
        "task": "apps.algo.tasks.run_monthly_auditor",
        "schedule": crontab(
            day_of_month=os.environ.get("ALGO_AUDITOR_DAY", "1"),
            hour=os.environ.get("ALGO_AUDITOR_HOUR", "8"),
            minute=os.environ.get("ALGO_AUDITOR_MINUTE", "0"),
        ),
        "options": {"expires": timedelta(hours=12).total_seconds()},
    },
}
