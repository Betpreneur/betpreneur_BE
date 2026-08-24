"""Maintenance job registry for algo admin endpoints."""


def maintenance_jobs():
    """
    Data jobs the Match Checker depends on, queued rather than run inline.

    Each is expensive, so callers return task ids to poll rather than holding the
    request open.
    """
    from apps.algo.tasks import (
        build_slip_review_market_cache,
        build_statpal_daily_cache,
        cleanup_slip_review_market_cache,
        fit_score_models,
        refresh_imminent_lineups,
        refresh_player_availability,
        recover_stale_slip_reviews,
        settle_slip_selections,
        sync_fixture_horizon,
    )

    return {
        # Ordered so a full run populates fixtures before anything that reads them.
        "statpal_daily_cache": (build_statpal_daily_cache, "Build StatPal 3-day fixtures and analysis snapshots"),
        "fixture_horizon": (sync_fixture_horizon, "Cache every fixture in the 3-day window"),
        "slip_review_market_cache": (build_slip_review_market_cache, "Pre-score private markets for slip review"),
        "slip_review_market_cache_cleanup": (
            cleanup_slip_review_market_cache,
            "Delete expired private slip-review market rows",
        ),
        "score_models": (fit_score_models, "Refit per-league goal models"),
        "player_availability": (refresh_player_availability, "Reload injuries and suspensions"),
        "lineups": (refresh_imminent_lineups, "Pull team sheets for imminent fixtures"),
        "settle_slips": (settle_slip_selections, "Settle yesterday's slip selections"),
        "recover_slip_reviews": (recover_stale_slip_reviews, "Finalize or fail stale slip reviews"),
    }


__all__ = ["maintenance_jobs"]
