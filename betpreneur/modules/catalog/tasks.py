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


@shared_task(
    bind=True,
    ignore_result=False,
    max_retries=2,
    default_retry_delay=600,
    soft_time_limit=3600,
    time_limit=4200,
)
def hydrate_team_intelligence_history(self, league_keys=None, seasons=None, max_teams=None):
    """
    Hydrate current/previous season team profiles for the Team Intelligence Store.

    This is the historical baseline for slip-review analysis, so runtime analysis can
    use stored team facts and reserve provider calls for live/recent context.
    """
    from betpreneur.modules.catalog.services.historical_hydrator import HistoricalTeamHydrator

    try:
        return HistoricalTeamHydrator().hydrate(
            league_keys=league_keys,
            seasons=seasons,
            max_teams=max_teams,
        )
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=600 * (self.request.retries + 1))
        raise


@shared_task(
    bind=True,
    ignore_result=False,
    max_retries=2,
    default_retry_delay=600,
    soft_time_limit=3600,
    time_limit=4200,
)
def build_team_recent_form(self, league_keys=None, seasons=None, windows=None, sync_matches=True, max_matches=None):
    """
    Build last-5/10/15 all/home/away team-form profiles for top intelligence leagues.
    """
    from betpreneur.modules.catalog.services.recent_form import RecentFormBuilder

    try:
        return RecentFormBuilder().build(
            league_keys=league_keys,
            seasons=seasons,
            windows=windows or (5, 10, 15),
            sync_matches=sync_matches,
            max_matches=max_matches,
        )
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=600 * (self.request.retries + 1))
        raise


@shared_task(
    bind=True,
    ignore_result=False,
    max_retries=2,
    default_retry_delay=600,
    soft_time_limit=3600,
    time_limit=4200,
)
def build_team_market_profiles(self, league_keys=None, seasons=None, min_attempts=1):
    """
    Build historical team/league market behaviour profiles for top intelligence leagues.
    """
    from betpreneur.modules.catalog.services.market_profiles import MarketProfileBuilder

    try:
        return MarketProfileBuilder().build(
            league_keys=league_keys,
            seasons=seasons,
            min_attempts=min_attempts,
        )
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=600 * (self.request.retries + 1))
        raise


@shared_task(
    bind=True,
    ignore_result=False,
    max_retries=2,
    default_retry_delay=300,
    soft_time_limit=900,
    time_limit=1200,
)
def refresh_team_data_coverage(self, league_keys=None, seasons=None, ttl_hours=24):
    """
    Refresh derived readiness rows for Team Intelligence coverage.
    """
    from betpreneur.modules.catalog.services.coverage_tracker import DataCoverageTracker

    try:
        return DataCoverageTracker().refresh(
            league_keys=league_keys,
            seasons=seasons,
            ttl_hours=ttl_hours,
        )
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=300 * (self.request.retries + 1))
        raise


@shared_task(
    bind=True,
    ignore_result=False,
    max_retries=1,
    default_retry_delay=900,
    soft_time_limit=7200,
    time_limit=7800,
)
def backfill_team_intelligence(
    self,
    league_keys=None,
    max_teams=None,
    max_matches=None,
    min_attempts=1,
    ttl_hours=24,
    sync_recent_matches=True,
):
    """
    One-time top-league Team Intelligence backfill with monitoring output.
    """
    from betpreneur.modules.catalog.services.team_intelligence_backfill import (
        team_intelligence_backfill_service,
    )

    try:
        return team_intelligence_backfill_service.backfill(
            league_keys=league_keys,
            max_teams=max_teams,
            max_matches=max_matches,
            min_attempts=min_attempts,
            ttl_hours=ttl_hours,
            sync_recent_matches=sync_recent_matches,
        )
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=900)
        raise
