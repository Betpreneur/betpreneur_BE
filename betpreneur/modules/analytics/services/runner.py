"""Runs the monthly auditor.

The auditor reads its window from the environment, so this sets the variables
around the call. It lives here rather than on the daily-run service because
the auditor is analytics' own job — putting the wrapper in picks meant picks
importing analytics, which is upward.
"""
from __future__ import annotations

from betpreneur.modules.catalog.api import runner_env
from betpreneur.platform.config import temporary_env

from ..auditor import run_auditor


def run_monthly_auditor(*, from_date=None, to_date=None):
    env = runner_env()
    if from_date is not None:
        env["AUDITOR_FROM"] = from_date.isoformat()
    if to_date is not None:
        env["AUDITOR_TO"] = to_date.isoformat()
    with temporary_env(env):
        return run_auditor()
