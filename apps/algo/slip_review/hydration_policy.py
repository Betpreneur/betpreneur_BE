"""Slip-review hydration and on-demand scoring policy."""

from apps.algo.scoring.api import COUNT_MODEL_ENGINE, SCORE_MATRIX_ENGINE, evaluator_for


def market_can_skip_core_on_demand(descriptor):
    if descriptor.family in {"team_shots_on_target"}:
        return True
    spec = evaluator_for(descriptor.family)
    if not spec:
        return False
    return spec.engine in {SCORE_MATRIX_ENGINE, COUNT_MODEL_ENGINE} or descriptor.family.startswith("player_")


def has_statpal_hydration_identity(candidate=None, statpal_candidate=None, provider_metadata=None):
    candidate = candidate or {}
    statpal_candidate = statpal_candidate or {}
    provider_metadata = provider_metadata or {}
    if str(provider_metadata.get("provider") or "").lower() == "statpal":
        if provider_metadata.get("provider_event_id"):
            return True
    if isinstance(statpal_candidate, dict) and (
        statpal_candidate.get("provider_match_id")
        or statpal_candidate.get("statpal_provider_match_id")
        or str(statpal_candidate.get("match_id") or "").startswith("statpal:")
        or statpal_candidate.get("home_team_id")
        or statpal_candidate.get("away_team_id")
        or statpal_candidate.get("statpal_home_team_id")
        or statpal_candidate.get("statpal_away_team_id")
    ):
        return True
    if isinstance(candidate, dict) and (
        candidate.get("provider_match_id")
        or candidate.get("statpal_provider_match_id")
        or str(candidate.get("match_id") or "").startswith("statpal:")
        or candidate.get("statpal_home_team_id")
        or candidate.get("statpal_away_team_id")
    ):
        return True
    return False


def should_skip_core_on_demand(descriptor, *, game=None, candidate=None, statpal_candidate=None, provider_metadata=None):
    if not market_can_skip_core_on_demand(descriptor):
        return False
    if game:
        return True
    return has_statpal_hydration_identity(candidate, statpal_candidate, provider_metadata)


def consume_review_force_fresh(review_scoring_context):
    if review_scoring_context is None:
        return True
    if review_scoring_context.get("fixture_universe_synced"):
        return False
    review_scoring_context["fixture_universe_synced"] = True
    return True


__all__ = [
    "consume_review_force_fresh",
    "has_statpal_hydration_identity",
    "market_can_skip_core_on_demand",
    "should_skip_core_on_demand",
]
