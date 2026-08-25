"""Model fitting and squad-data tasks.

Thin adapters — the work lives in services/.
"""
import logging

from celery import shared_task

log = logging.getLogger(__name__)


@shared_task(bind=True, ignore_result=False)
def refresh_imminent_lineups(self, match_ids=None):
    """
    Pull team sheets for fixtures about to kick off.

    Lineups only firm up in the hour before kickoff, so this runs frequently and targets
    only fixtures users actually have money on — the ones referenced by slip selections
    still awaiting settlement today.
    """
    from betpreneur.modules.scoring.services.lineups import lineup_service
    from betpreneur.modules.scoring.services.priority_fixtures import priority_match_ids

    if match_ids is None:
        match_ids = priority_match_ids()

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
    from betpreneur.modules.scoring.services.availability import player_availability_service

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
    from betpreneur.modules.catalog.api import statpal_client
    from betpreneur.modules.scoring.services.service import score_model_service

    client = statpal_client()
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
