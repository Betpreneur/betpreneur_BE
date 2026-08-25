"""The Celery application. Workers reach it via `celery -A config`."""
import os

from celery import Celery

from .schedules import BEAT_SCHEDULE

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("betpreneur")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
app.conf.beat_schedule = BEAT_SCHEDULE
