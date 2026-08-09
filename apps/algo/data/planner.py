"""
Per-review hydration planning.

Two sources of waste in the old flow:

1. **Hydration ran per leg, not per fixture.** A slip with several markets on one match
   planned and refreshed the same snapshots once per leg. On a same-fixture slip that is
   almost entirely redundant work, and the import fix that stopped dropping those legs
   made it far more common.
2. **Every leg hydrated, even when it needed nothing.** Since ADR-001 the goals and
   result families are served by the nightly league fit, so they need no per-fixture
   StatPal call at all. Only corners, cards and player markets still do.

A per-review budget caps the damage a pathological slip can do to the rate limit;
exhausting it degrades legs to "insufficient data" rather than hammering the provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..evaluators.registry import SCORE_MATRIX_ENGINE, evaluator_for, required_capabilities
from .capability import snapshots_for_capabilities

DEFAULT_CALL_BUDGET = 120

# Ceilings for a model-backed assessment. These are deliberately below perfect
# certainty because fitted rates and fixture snapshots can still be thin or live-state
# dependent.
MODEL_CONFIDENCE_CAPS = {"strong": 85, "medium": 75, "limited": 62, "poor": 0}


def snapshots_for_family(family: str) -> list[str]:
    """Snapshot types this family still needs from StatPal, if any."""
    spec = evaluator_for(family)
    if spec is None or spec.engine == SCORE_MATRIX_ENGINE:
        return []
    return snapshots_for_capabilities(required_capabilities([family]))


def model_backed_capability(family: str, data_quality: str) -> dict:
    """
    Capability payload for a market served by the fitted score model.

    The capability layer was written assuming StatPal snapshots were the only data
    source; a matrix-derived market would otherwise be scored as having zero coverage
    and dropped. Its data quality comes from the league fit instead.
    """
    quality = str(data_quality or "poor").lower()
    cap = MODEL_CONFIDENCE_CAPS.get(quality, 0)
    warnings = []
    if quality in {"limited", "poor"}:
        warnings.append("thin_league_sample")
    warnings.append("no_expected_goals_available")
    return {
        "market": {"family": family},
        "support_level": "full" if quality in {"strong", "medium"} else "medium",
        "data_quality": quality,
        "confidence_cap": cap,
        "scoreable": quality != "poor",
        "required_snapshots": [],
        "available_snapshots": [],
        "missing_snapshots": [],
        "coverage_percent": 100.0 if quality != "poor" else 0.0,
        "warnings": warnings,
        "reason": f"Served by the fitted league score model at {quality} data quality.",
    }


def _detailed_stats_supports_count_market(family: str, statpal_context) -> bool:
    summary = ((((statpal_context or {}).get("snapshots") or {}).get("detailed_stats") or {}).get("summary") or {})
    if not summary:
        return False
    if family in {"corners_total", "team_corners", "corner_range", "team_corner_range", "corners_result", "corner_handicap"}:
        return summary.get("home_corners") is not None and summary.get("away_corners") is not None
    if family in {"cards_total", "team_cards", "booking_points", "cards", "cards_result"}:
        return any(
            summary.get(key) is not None
            for key in ("home_yellow_cards", "away_yellow_cards", "home_red_cards", "away_red_cards", "total_cards", "booking_points")
        )
    return False


def capability_for_descriptor(descriptor, *, fixture=None, statpal_context=None):
    """
    Capability for a market, routed by whichever engine actually serves it.

    Snapshot coverage is only the right yardstick for the StatPal advisory path. Judging
    a matrix- or count-model market that way caps it at the coverage of snapshots it
    never needed, which silently prevents it from ever being recommended.
    """
    from ..evaluators.registry import COUNT_MODEL_ENGINE, evaluator_for
    from ..market_capabilities import market_capability_service

    spec = evaluator_for(getattr(descriptor, "family", ""))
    if spec is None:
        return market_capability_service.assess(descriptor, statpal_context=statpal_context or {}).to_dict()

    if spec.engine == SCORE_MATRIX_ENGINE:
        from ..scoring.service import score_model_service

        game = fixture or {}
        rates = score_model_service.rates_for_fixture(
            league_id=game.get("statpal_provider_competition_id") or game.get("code") or game.get("league_id") or "",
            home_team_name=game.get("hname") or game.get("home_team") or "",
            away_team_name=game.get("aname") or game.get("away_team") or "",
        )
        return model_backed_capability(descriptor.family, rates.data_quality if rates.usable else "poor")

    if spec.engine == COUNT_MODEL_ENGINE:
        from ..scoring.rate_profiles import team_rate_profile_service

        game = fixture or {}
        context = statpal_context or game.get("statpal_context") or {}
        home = team_rate_profile_service.profile_for(
            team_id=str(game.get("statpal_home_team_id") or ""), team_name=game.get("hname") or game.get("home_team") or ""
        )
        away = team_rate_profile_service.profile_for(
            team_id=str(game.get("statpal_away_team_id") or ""), team_name=game.get("aname") or game.get("away_team") or ""
        )
        available = [profile for profile in (home, away) if profile is not None]
        if not available and _detailed_stats_supports_count_market(descriptor.family, context):
            quality = "limited"
        elif not available:
            quality = "poor"
        elif len(available) == 2 and min(profile.matches for profile in available) >= 8:
            quality = "medium"
        else:
            quality = "limited"
        return model_backed_capability(descriptor.family, quality)

    return market_capability_service.assess(descriptor, statpal_context=statpal_context or {}).to_dict()


@dataclass
class HydrationStats:
    calls_used: int = 0
    served_from_cache: int = 0
    served_by_model: int = 0
    budget_exhausted: bool = False
    fixtures_hydrated: set = field(default_factory=set)

    def to_dict(self):
        return {
            "calls_used": self.calls_used,
            "served_from_cache": self.served_from_cache,
            "served_by_model": self.served_by_model,
            "fixtures_hydrated": len(self.fixtures_hydrated),
            "budget_exhausted": self.budget_exhausted,
        }


_EMPTY_BUNDLE = {"context": {}, "refreshed": {}, "plan": {}, "plan_before_refresh": {}}


class FixtureHydrator:
    """
    Review-scoped snapshot fetcher: one hydration per (fixture, snapshot set).

    Not thread-safe by design — one instance belongs to one review being analysed.
    """

    def __init__(self, *, call_budget: int = DEFAULT_CALL_BUDGET, snapshot_service=None):
        self._cache: dict[tuple, dict] = {}
        self._budget = max(0, int(call_budget))
        self.stats = HydrationStats()
        self._snapshot_service = snapshot_service

    @property
    def service(self):
        if self._snapshot_service is None:
            from ..statpal_snapshots import statpal_snapshot_service

            self._snapshot_service = statpal_snapshot_service
        return self._snapshot_service

    def bundle_for(
        self,
        descriptor,
        *,
        match_id="",
        provider_match_id="",
        provider_competition_id="",
        home_team_id="",
        away_team_id="",
    ) -> dict:
        family = getattr(descriptor, "family", "") or ""
        needed = snapshots_for_family(family)
        if not needed:
            # Served by the nightly league fit; no per-fixture call required.
            self.stats.served_by_model += 1
            return dict(_EMPTY_BUNDLE)

        key = (
            str(match_id or provider_match_id or ""),
            tuple(sorted(needed)),
            str(home_team_id or ""),
            str(away_team_id or ""),
        )
        if key in self._cache:
            self.stats.served_from_cache += 1
            return self._cache[key]

        if self.stats.calls_used >= self._budget:
            self.stats.budget_exhausted = True
            return dict(_EMPTY_BUNDLE)

        bundle = self.service.prepare_fixture_context_for_market(
            descriptor,
            match_id=match_id,
            provider_match_id=provider_match_id,
            provider_competition_id=provider_competition_id,
        )
        self.stats.calls_used += 1
        if "team_stats" in needed and (home_team_id or away_team_id) and self.stats.calls_used < self._budget:
            team_refresh = self.service.refresh_fixture_team_stats(
                match_id=match_id,
                provider_match_id=provider_match_id,
                provider_competition_id=provider_competition_id,
                home_team_id=home_team_id,
                away_team_id=away_team_id,
            )
            bundle["team_stats_refresh"] = team_refresh
            bundle["context"] = self.service.fixture_context(match_id=match_id, provider_match_id=provider_match_id)
            self.stats.calls_used += int((team_refresh.get("api_usage") or {}).get("attempted_calls") or 0)
        self.stats.fixtures_hydrated.add(key[0])
        self._cache[key] = bundle
        return bundle


def plan_slip_hydration(selections) -> dict:
    """
    Summarise what a slip will actually need before any work starts.

    Grouping is by the bookmaker's event id, which is available before fixture
    resolution, so the plan can be reported up front.
    """
    by_fixture: dict[str, set] = {}
    for selection in selections or []:
        payload = selection.get("provider_payload") or selection
        event_id = str(payload.get("provider_event_id") or payload.get("match") or "")
        taxonomy = payload.get("market_taxonomy") or {}
        family = taxonomy.get("family") or ""
        by_fixture.setdefault(event_id, set()).update(snapshots_for_family(family))

    needing = {key: sorted(value) for key, value in by_fixture.items() if value}
    return {
        "legs": len(selections or []),
        "distinct_fixtures": len(by_fixture),
        "fixtures_needing_snapshots": len(needing),
        "fixtures_served_by_model": len(by_fixture) - len(needing),
        "estimated_snapshot_calls": sum(len(value) for value in needing.values()),
    }
