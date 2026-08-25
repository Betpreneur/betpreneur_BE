"""Celery base task.

Tasks in this codebase should be five-line adapters: deserialize, call a
service, return a summary. This base supplies the retry policy and the
structured logging so the task body does not have to.
"""
from __future__ import annotations

import logging

from celery import Task

logger = logging.getLogger(__name__)


class BaseTask(Task):
    """Retries transient failures with backoff; logs every terminal failure."""

    autoretry_for = (ConnectionError, TimeoutError)
    retry_backoff = True
    retry_backoff_max = 600
    retry_jitter = True
    max_retries = 3

    def on_failure(self, exc, task_id, args, kwargs, einfo) -> None:
        logger.error(
            "task failed name=%s id=%s args=%r error=%s",
            self.name, task_id, args, exc,
            exc_info=einfo,
        )
        super().on_failure(exc, task_id, args, kwargs, einfo)
