"""Fixture and cache build tasks.

Thin adapters — the work lives in services/.
"""
from datetime import date

from celery import shared_task


@shared_task(bind=True, ignore_result=False, soft_time_limit=1500, time_limit=1800)
def sync_fixture_horizon(self, days=3, league_ids=None):
    """
    Cache every fixture across the Match Checker's 3-day window.

    One request per league, roughly a thousand in total — about 2% of the daily quota.
    Doing it on a schedule keeps fixture resolution to a cache lookup, instead of the
    per-leg provider sync that used to dominate a slip review.
    """
    from betpreneur.modules.catalog.services.search import FixtureSearchService

    return FixtureSearchService().sync_statpal_horizon(days=days, league_ids=league_ids)


@shared_task(
    bind=True,
    ignore_result=False,
    max_retries=2,
    default_retry_delay=300,
    soft_time_limit=2400,
    time_limit=3000,
)
def build_statpal_daily_cache(self, start_date=None, days=3, include_optional=False, force=False, max_tasks=None):
    """
    Build StatPal's 3-day fixture/stat cache.

    This is the StatPal-native replacement/complement for the older provider fixture
    horizon: it fetches the daily match universe, then refreshes fixture, league,
    team, H2H, odds, lineup, injury, weather, and prediction snapshots for analysis.
    """
    from betpreneur.modules.catalog.services.daily_build import StatPalDailyBuildService

    parsed_start = date.fromisoformat(start_date) if start_date else None
    try:
        return StatPalDailyBuildService().build(
            start_date=parsed_start,
            days=days,
            include_optional=include_optional,
            force=force,
            max_tasks=max_tasks,
        )
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=300 * (self.request.retries + 1))
        raise




