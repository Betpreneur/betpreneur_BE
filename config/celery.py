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
