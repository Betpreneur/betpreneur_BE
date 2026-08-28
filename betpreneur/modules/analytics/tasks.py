"""Analytics and data-maintenance tasks.

Thin adapters — the work lives in services/.
"""

from datetime import date

from celery import chain, shared_task
from django.conf import settings

from .services.runner import run_monthly_auditor as run_monthly_auditor_service
from .services.strategy_memory import evaluate_strategy_memory as evaluate_strategy_memory_service


@shared_task(bind=True, ignore_result=False)
def refresh_team_intelligence_nightly(
    self,
    league_keys=None,
    seasons=None,
    days=3,
    recent_form_sync_matches=False,
    market_min_attempts=1,
    coverage_ttl_hours=24,
):
    """
    Queue the nightly Team Intelligence refresh in dependency order.

    The workflow warms fixtures first, then updates team baselines, recent form,
    market profiles and final coverage rows before daily picks/slip cache read them.
    """
    workflow = chain(
        _signature(
            "betpreneur.modules.catalog.tasks.sync_fixture_horizon",
            kwargs={"days": days},
            queue=settings.ALGO_STATPAL_QUEUE,
        ),
        _signature(
            "betpreneur.modules.catalog.tasks.hydrate_team_intelligence_history",
            kwargs={"league_keys": league_keys, "seasons": seasons},
            queue=settings.ALGO_STATPAL_QUEUE,
        ),
        _signature(
            "betpreneur.modules.catalog.tasks.build_team_recent_form",
            kwargs={
                "league_keys": league_keys,
                "seasons": seasons,
                "sync_matches": recent_form_sync_matches,
            },
            queue=settings.ALGO_STATPAL_QUEUE,
        ),
        _signature(
            "betpreneur.modules.catalog.tasks.build_team_market_profiles",
            kwargs={
                "league_keys": league_keys,
                "seasons": seasons,
                "min_attempts": market_min_attempts,
            },
            queue=settings.ALGO_STATPAL_QUEUE,
        ),
        _signature(
            "betpreneur.modules.catalog.tasks.refresh_team_data_coverage",
            kwargs={
                "league_keys": league_keys,
                "seasons": seasons,
                "ttl_hours": coverage_ttl_hours,
            },
            queue=settings.ALGO_MAINTENANCE_QUEUE,
        ),
    )
    result = workflow.apply_async()
    return {
        "status": "queued",
        "workflow_task_id": result.id,
        "steps": [
            "sync_fixture_horizon",
            "hydrate_team_intelligence_history",
            "build_team_recent_form",
            "build_team_market_profiles",
            "refresh_team_data_coverage",
        ],
    }


@shared_task(bind=True, ignore_result=False)
def run_monthly_auditor(self, from_date=None, to_date=None):
    if from_date is not None:
        from_date = date.fromisoformat(from_date)
    if to_date is not None:
        to_date = date.fromisoformat(to_date)

    return run_monthly_auditor_service(from_date=from_date, to_date=to_date)


@shared_task(bind=True, ignore_result=False)
def evaluate_strategy_memory(self, decision_date=None, evaluation_days=14):
    if decision_date is not None:
        decision_date = date.fromisoformat(decision_date)
    return evaluate_strategy_memory_service(
        decision_date=decision_date,
        evaluation_days=evaluation_days,
    )


def _signature(task_name: str, *, kwargs: dict, queue: str):
    from celery import current_app

    return current_app.signature(task_name, kwargs=kwargs, immutable=True).set(queue=queue)
