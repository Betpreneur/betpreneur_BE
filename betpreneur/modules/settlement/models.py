"""A record of every settlement attempt.

Settlement is deliberately re-runnable: fixtures finish late, so a date settled
this morning may still have pending legs this evening. What must not happen is
two attempts at the same date running at once — they would duplicate the
provider calls and interleave their writes.

So this is an attempt log, not a once-ever key. Concurrency is handled by the
run_once lock in platform/tasks; these rows are the audit trail of what each
attempt actually did.
"""
from django.db import models


class SettlementRun(models.Model):
    class Scope(models.TextChoices):
        PICKS = "picks", "Daily picks"
        SLIPS = "slips", "Slip selections"

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped (already running)"

    target_date = models.DateField()
    scope = models.CharField(max_length=10, choices=Scope.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    #: The service's own return payload, so a run can be inspected after the fact.
    summary = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "settlement_run"
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["target_date", "scope"]),
            models.Index(fields=["status", "started_at"]),
        ]

    def __str__(self):
        return f"{self.scope} settlement for {self.target_date} ({self.status})"
