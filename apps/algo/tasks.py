import logging
from datetime import timedelta
from datetime import date

from billiard.exceptions import SoftTimeLimitExceeded
from celery import chord, shared_task
from django.core.exceptions import ObjectDoesNotExist
from django.conf import settings
from django.utils import timezone

from .services import algo_runner_service

log = logging.getLogger(__name__)


@shared_task(bind=True, ignore_result=False)
def generate_daily_picks(self, target_date=None):
    if target_date is None:
        target_date = timezone.localdate()
    else:
        target_date = date.fromisoformat(target_date)

    algo_run = algo_runner_service.create_run(target_date=target_date)
    fixture_ids = algo_runner_service.prepare_fanout_run(algo_run)
    if fixture_ids:
        workflow = chord(
            [
                score_fixture_for_daily_run.s(fixture_id).set(queue=settings.ALGO_SCORING_QUEUE)
                for fixture_id in fixture_ids
            ]
        )(publish_daily_run.s(algo_run.id).set(queue=settings.ALGO_DAILY_QUEUE))
        status_value = "scoring_queued"
        child_task_id = workflow.id
    else:
        algo_run.refresh_from_db()
        status_value = algo_run.status
        child_task_id = ""
    return {
        "run_id": algo_run.id,
        "target_date": algo_run.target_date.isoformat(),
        "status": status_value,
        "publish_task_id": child_task_id,
        "picks_count": algo_run.picks_count,
        "bankers": algo_run.bankers,
        "value_gems": algo_run.value_gems,
        "wild_cards": algo_run.wild_cards,
        "error": algo_run.error,
    }


@shared_task(
    bind=True,
    ignore_result=False,
    max_retries=3,
    default_retry_delay=60,
    rate_limit="6/m",
    soft_time_limit=900,
    time_limit=1200,
)
def score_fixture_for_daily_run(self, fixture_id):
    try:
        result = algo_runner_service.score_fixture_for_run(fixture_id)
    except ObjectDoesNotExist as exc:
        return {
            "fixture_id": fixture_id,
            "status": "skipped",
            "error": str(exc) or "object_not_found",
        }
    if result.get("status") == "failed" and self.request.retries < self.max_retries:
        raise self.retry(countdown=60 * (self.request.retries + 1))
    return result


@shared_task(bind=True, ignore_result=False, max_retries=2, default_retry_delay=120)
def publish_daily_run(self, score_results, run_id):
    algo_run = algo_runner_service.publish_fanout_run(run_id)
    explain_picks_for_run.apply_async(args=[algo_run.id], queue=settings.ALGO_LLM_QUEUE)
    return {
        "run_id": algo_run.id,
        "target_date": algo_run.target_date.isoformat(),
        "status": algo_run.status,
        "picks_count": algo_run.picks_count,
        "bankers": algo_run.bankers,
        "value_gems": algo_run.value_gems,
        "wild_cards": algo_run.wild_cards,
        "scored": sum(1 for item in score_results or [] if (item or {}).get("status") == "scored"),
        "failed": sum(1 for item in score_results or [] if (item or {}).get("status") == "failed"),
        "error": algo_run.error,
    }


@shared_task(
    bind=True,
    ignore_result=False,
    max_retries=3,
    default_retry_delay=120,
    rate_limit="12/m",
    soft_time_limit=300,
    time_limit=420,
)
def explain_picks_for_run(self, run_id):
    try:
        return algo_runner_service.explain_picks_for_run(run_id)
    except Exception as exc:
        raise self.retry(exc=exc, countdown=120 * (self.request.retries + 1))


@shared_task(
    bind=True,
    ignore_result=False,
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=1500,
    time_limit=1800,
)
def import_slip_review(self, review_id):
    try:
        from .views import _json_safe, process_slip_review_import

        return _json_safe(process_slip_review_import(review_id))
    except SoftTimeLimitExceeded:
        from .views import fail_slip_review_import

        return fail_slip_review_import(
            review_id,
            "Slip review timed out while analysing selections. Please retry with fewer legs or try again later.",
            error_code="soft_time_limit_exceeded",
        )
    except ValueError as exc:
        return {
            "review_id": review_id,
            "status": "failed",
            "error": str(exc),
        }
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1))
        raise


@shared_task(
    bind=True,
    ignore_result=False,
    max_retries=1,
    default_retry_delay=60,
    soft_time_limit=900,
    time_limit=1200,
)
def analyse_slip_review_leg(self, review_id, index, selection, days=3):
    try:
        from .views import _json_safe, process_slip_review_leg_analysis

        return _json_safe(process_slip_review_leg_analysis(review_id, index, selection, days=days))
    except SoftTimeLimitExceeded as exc:
        from .views import _json_safe, process_slip_review_leg_failure

        return _json_safe(
            process_slip_review_leg_failure(
                review_id,
                index,
                selection,
                "Slip leg analysis timed out.",
                error_code="soft_time_limit_exceeded",
            )
        )
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1))
        from .views import _json_safe, process_slip_review_leg_failure

        return _json_safe(process_slip_review_leg_failure(review_id, index, selection, str(exc)))


@shared_task(bind=True, ignore_result=False, max_retries=1, default_retry_delay=60)
def finalize_slip_review_import(self, leg_results, review_id):
    try:
        from .views import _json_safe, finalize_slip_review_import_results

        return _json_safe(finalize_slip_review_import_results(review_id, leg_results))
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60)
        from .views import fail_slip_review_import

        return fail_slip_review_import(review_id, f"Slip review finalization failed: {exc}", error_code="finalize_failed")


@shared_task(bind=True, ignore_result=False)
def recover_stale_slip_reviews(self, stale_after_seconds=None, limit=25):
    from .views import _json_safe, recover_stale_slip_reviews as recover_stale

    return _json_safe(recover_stale(stale_after_seconds=stale_after_seconds, limit=limit))


@shared_task(bind=True, ignore_result=False)
def refill_daily_free_tokens(self, run_date=None, limit=None):
    from .tokens import token_wallet_service

    parsed_date = date.fromisoformat(run_date) if run_date else None
    return token_wallet_service.refill_daily_free_tokens(run_date=parsed_date, limit=limit)


@shared_task(bind=True, ignore_result=False)
def expire_token_reservations(self, limit=200):
    from .tokens import token_wallet_service

    return token_wallet_service.expire_stale_reservations(limit=limit)


@shared_task(bind=True, ignore_result=False)
def recover_daily_run(self, run_id, rescore_failed=False):
    return algo_runner_service.recover_fanout_run(run_id, rescore_failed=rescore_failed)


@shared_task(bind=True, ignore_result=False)
def settle_daily_results(self, target_date=None):
    if target_date is None:
        target_date = timezone.localdate() - timedelta(days=1)
    else:
        target_date = date.fromisoformat(target_date)

    return algo_runner_service.update_results(target_date=target_date)


@shared_task(bind=True, ignore_result=False, soft_time_limit=1500, time_limit=1800)
def sync_fixture_horizon(self, days=3, league_ids=None):
    """
    Cache every fixture across the Match Checker's 3-day window.

    One request per league, roughly a thousand in total — about 2% of the daily quota.
    Doing it on a schedule keeps fixture resolution to a cache lookup, instead of the
    per-leg provider sync that used to dominate a slip review.
    """
    from .services import FixtureSearchService

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
    from .statpal_daily_build import StatPalDailyBuildService

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
    max_retries=1,
    default_retry_delay=300,
    soft_time_limit=3600,
    time_limit=4200,
)
def build_slip_review_market_cache(self, start_date=None, days=None, sync_fixtures=True, force=False, max_fixtures=None):
    parsed_start = date.fromisoformat(start_date) if start_date else None
    try:
        return algo_runner_service.build_slip_review_market_cache(
            start_date=parsed_start,
            days=days,
            sync_fixtures=sync_fixtures,
            force=force,
            max_fixtures=max_fixtures,
        )
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=300)
        raise


@shared_task(bind=True, ignore_result=False)
def cleanup_slip_review_market_cache(self, grace_seconds=None, limit=None):
    return algo_runner_service.cleanup_slip_review_market_cache(
        grace_seconds=grace_seconds,
        limit=limit,
    )


@shared_task(bind=True, ignore_result=False)
def refresh_imminent_lineups(self, match_ids=None):
    """
    Pull team sheets for fixtures about to kick off.

    Lineups only firm up in the hour before kickoff, so this runs frequently and targets
    only fixtures users actually have money on — the ones referenced by slip selections
    still awaiting settlement today.
    """
    from .models import SlipSelection
    from .scoring.lineups import lineup_service

    if match_ids is None:
        today = timezone.localdate()
        pending = SlipSelection.objects.filter(
            match_date=today, outcome=SlipSelection.Outcome.PENDING
        ).values_list("analysis_payload", flat=True)
        match_ids = set()
        for payload in pending:
            matched = ((payload or {}).get("matched_fixture") or {})
            candidate = (
                matched.get("statpal_provider_match_id")
                or matched.get("provider_match_id")
                or matched.get("main_id")
                or ""
            )
            if candidate:
                match_ids.add(str(candidate))

    refreshed = failed = 0
    errors = []
    for match_id in sorted(match_ids or []):
        try:
            lineup_service.refresh(match_id=match_id)
            refreshed += 1
        except Exception as exc:
            failed += 1
            if len(errors) < 20:
                errors.append({"match_id": match_id, "error": str(exc)[:200]})

    return {"considered": len(match_ids or []), "refreshed": refreshed, "failed": failed, "errors": errors}


@shared_task(bind=True, ignore_result=False, max_retries=2, default_retry_delay=300)
def refresh_player_availability(self):
    """
    Reload injuries and suspensions.

    Runs often, because a late fitness call is exactly the case that turns a priced
    player prop into a dead bet. One league-wide call covers every fixture.
    """
    from .scoring.availability import player_availability_service

    return player_availability_service.refresh()


def _season_names(node) -> list[str]:
    """StatPal returns `season` as a dict for one entry and a list for many."""
    if isinstance(node, dict):
        node = [node]
    names = []
    for item in node or []:
        name = str((item or {}).get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _normalize_season(value: str) -> str:
    """`2026/2027` from the standings body and `2026-2027` from the seasons list."""
    return str(value or "").strip().replace("/", "-")


def prior_season_candidates(seasons, current_season="", limit=3) -> tuple[str, ...]:
    """
    Finished seasons for a league, newest first, excluding the one in progress.

    The immediately preceding season is not automatically the right prior: England's
    2025/2026 standings sit at 20 games, frozen mid-January, while 2024/2025 has the
    full 38. The caller walks this list and takes the first with a real sample.
    """
    current = _normalize_season(current_season)
    names = [name for name in seasons if _normalize_season(name) != current]
    return tuple(reversed(names))[:limit]


def _standings_season_index(client) -> dict[str, list[str]]:
    """Map league id to the seasons that have standings, in one request."""
    try:
        payload = client.soccer_endpoint("SOCCER_LEAGUE_SEASONS")
    except Exception:
        log.warning("Season index fetch failed; fitting without prior seasons", exc_info=True)
        return {}
    leagues = ((payload or {}).get("seasons") or {}).get("league") or []
    if isinstance(leagues, dict):
        leagues = [leagues]
    index = {}
    for league in leagues:
        league_id = str((league or {}).get("id") or "")
        if not league_id:
            continue
        index[league_id] = _season_names(((league or {}).get("standings") or {}).get("season"))
    return index


@shared_task(bind=True, ignore_result=False)
def fit_score_models(self, league_ids=None):
    """
    Refit the per-league goal models.

    Nightly, so a slip review only ever reads a cached fit. A league that fails is
    logged and skipped rather than aborting the run — one bad league must not leave
    every other league stale.
    """
    from .scoring.service import score_model_service
    from .statpal import StatPalClient

    client = StatPalClient()
    if league_ids:
        targets = [{"id": str(item)} for item in league_ids]
    else:
        payload = client.soccer_endpoint("SOCCER_LEAGUES")
        leagues = ((payload or {}).get("leagues") or {}).get("league") or []
        targets = leagues if isinstance(leagues, list) else [leagues]

    # One call covers every league, so carrying priors costs a single extra request here
    # plus at most PRIOR_SEASON_ATTEMPTS standings calls for leagues that need one.
    season_index = _standings_season_index(client)

    fitted = failed = priors_used = 0
    errors = []
    for league in targets:
        league_id = str((league or {}).get("id") or "")
        if not league_id:
            continue
        try:
            current_season = (league or {}).get("season") or ""
            model = score_model_service.fit_league(
                league_id=league_id,
                league_name=(league or {}).get("name") or "",
                season=current_season,
                prior_seasons=prior_season_candidates(
                    season_index.get(league_id) or (), current_season
                ),
            )
            if getattr(model, "prior_season", ""):
                priors_used += 1
            fitted += 1
        except Exception as exc:
            failed += 1
            if len(errors) < 20:
                errors.append({"league_id": league_id, "error": str(exc)[:200]})

    return {
        "considered": len(targets),
        "fitted": fitted,
        "priors_used": priors_used,
        "failed": failed,
        "errors": errors,
    }


@shared_task(bind=True, ignore_result=False, max_retries=2, default_retry_delay=300)
def settle_slip_selections(self, target_date=None):
    if target_date is not None:
        target_date = date.fromisoformat(target_date)

    return algo_runner_service.settle_slip_selections(target_date=target_date)


@shared_task(bind=True, ignore_result=False)
def run_monthly_auditor(self, from_date=None, to_date=None):
    if from_date is not None:
        from_date = date.fromisoformat(from_date)
    if to_date is not None:
        to_date = date.fromisoformat(to_date)

    return algo_runner_service.run_auditor(from_date=from_date, to_date=to_date)
