"""Task -> queue routing.

Each task is named for the module that owns it. The queue names are unchanged
from before the refactor, so the docker-compose workers need no edits — but the
task *names* did change, so drain the queues at cutover.
"""
from . import queues

TASK_ROUTES = {
    "betpreneur.modules.picks.tasks.generate_daily_picks": {"queue": queues.ALGO_DAILY},
    "betpreneur.modules.picks.tasks.publish_daily_run": {"queue": queues.ALGO_DAILY},
    "betpreneur.modules.picks.tasks.recover_daily_run": {"queue": queues.ALGO_DAILY},
    "betpreneur.modules.picks.tasks.score_fixture_for_daily_run": {"queue": queues.ALGO_SCORING},
    "betpreneur.modules.picks.tasks.explain_picks_for_run": {"queue": queues.ALGO_LLM},
    "betpreneur.modules.catalog.tasks.build_statpal_daily_cache": {"queue": queues.ALGO_STATPAL},
    "betpreneur.modules.picks.tasks.build_slip_review_market_cache": {"queue": queues.ALGO_STATPAL},
    "betpreneur.modules.catalog.tasks.sync_fixture_horizon": {"queue": queues.ALGO_STATPAL},
    "betpreneur.modules.scoring.tasks.fit_score_models": {"queue": queues.ALGO_STATPAL},
    "betpreneur.modules.settlement.tasks.settle_daily_results": {"queue": queues.ALGO_SETTLEMENT},
    "betpreneur.modules.settlement.tasks.settle_slip_selections": {"queue": queues.ALGO_SETTLEMENT},
    "betpreneur.modules.scoring.tasks.refresh_imminent_lineups": {"queue": queues.ALGO_MAINTENANCE},
    "betpreneur.modules.scoring.tasks.refresh_player_availability": {"queue": queues.ALGO_MAINTENANCE},
    "betpreneur.modules.slips.tasks.recover_stale_slip_reviews": {"queue": queues.ALGO_MAINTENANCE},
    "betpreneur.modules.picks.tasks.cleanup_slip_review_market_cache": {"queue": queues.ALGO_MAINTENANCE},
    "betpreneur.modules.billing.tasks.refill_daily_free_tokens": {"queue": queues.ALGO_MAINTENANCE},
    "betpreneur.modules.billing.tasks.expire_token_reservations": {"queue": queues.ALGO_MAINTENANCE},
    "betpreneur.modules.analytics.tasks.run_monthly_auditor": {"queue": queues.ALGO_MAINTENANCE},
    "betpreneur.modules.slips.tasks.import_slip_review": {"queue": queues.SLIP_REVIEW_IMPORT},
    "betpreneur.modules.slips.tasks.analyse_slip_review_leg": {"queue": queues.SLIP_REVIEW_LEG},
    "betpreneur.modules.slips.tasks.finalize_slip_review_import": {"queue": queues.SLIP_REVIEW_FINALIZE},
}
