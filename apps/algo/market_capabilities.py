from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .market_taxonomy import MarketDescriptor, describe_market


FULL = "full"
MEDIUM = "medium"
WEAK = "weak"
UNSUPPORTED = "unsupported"


CAPABILITY_RULES = {
    "match_result": (FULL, 88),
    "double_chance": (FULL, 84),
    "draw_no_bet": (FULL, 84),
    "asian_handicap": (FULL, 82),
    "handicap": (MEDIUM, 76),
    "total_goals": (FULL, 88),
    "team_total_goals": (FULL, 86),
    "result_total_goals": (MEDIUM, 80),
    "result_btts": (MEDIUM, 80),
    "total_btts": (MEDIUM, 80),
    "double_chance_btts": (MEDIUM, 80),
    "double_chance_total_goals": (MEDIUM, 80),
    "result_or_total_goals": (MEDIUM, 80),
    "result_or_btts": (MEDIUM, 80),
    "result_or_clean_sheet": (MEDIUM, 80),
    "btts": (FULL, 84),
    "clean_sheet": (MEDIUM, 78),
    "first_to_score": (MEDIUM, 76),
    "last_to_score": (WEAK, 68),
    "corners_total": (MEDIUM, 78),
    "team_corners": (MEDIUM, 76),
    "corner_range": (MEDIUM, 74),
    "team_corner_range": (MEDIUM, 72),
    "corners_result": (MEDIUM, 72),
    "corner_handicap": (MEDIUM, 72),
    "cards_total": (MEDIUM, 74),
    "cards_result": (MEDIUM, 72),
    "team_cards": (MEDIUM, 72),
    "booking_points": (MEDIUM, 72),
    "player_goal": (WEAK, 70),
    "player_shots": (WEAK, 68),
    "player_shots_on_target": (WEAK, 68),
    "player_card": (WEAK, 66),
    "player_assist": (WEAK, 64),
    "player_saves": (WEAK, 64),
    "goal_range": (WEAK, 64),
    "exact_goals": (WEAK, 58),
    "multigoals": (WEAK, 62),
    "correct_score": (WEAK, 52),
    "odd_even": (WEAK, 55),
    "winning_margin": (WEAK, 58),
    "combo": (WEAK, 66),
}

QUALITY_ORDER = {
    FULL: 3,
    MEDIUM: 2,
    WEAK: 1,
    UNSUPPORTED: 0,
}


@dataclass(frozen=True)
class MarketCapability:
    market: dict[str, Any]
    support_level: str
    data_quality: str
    confidence_cap: int
    scoreable: bool
    required_snapshots: list[str]
    available_snapshots: list[str]
    missing_snapshots: list[str]
    coverage_percent: float
    warnings: list[str]
    reason: str

    def to_dict(self):
        return asdict(self)


class MarketCapabilityService:
    def assess(
        self,
        market: str | MarketDescriptor | dict[str, Any],
        *,
        statpal_context: dict[str, Any] | None = None,
        snapshot_plan: dict[str, Any] | None = None,
    ) -> MarketCapability:
        descriptor = self._descriptor(market)
        base_level, base_cap = CAPABILITY_RULES.get(descriptor.family, (UNSUPPORTED, 0))
        plan = snapshot_plan or (statpal_context or {}).get("market_snapshot_plan") or {}
        required = list(plan.get("snapshot_types") or descriptor.data_requirements or [])
        available = sorted(((statpal_context or {}).get("snapshots") or {}).keys())
        missing = list(plan.get("missing_snapshot_types") or [])
        if required and not missing:
            missing = [item for item in required if item not in available]
        coverage = self._coverage(required, available, plan)
        data_quality = self._data_quality(base_level, coverage, missing)
        confidence_cap = self._cap(base_cap, data_quality, descriptor)
        warnings = self._warnings(descriptor, base_level, data_quality, missing, coverage)
        scoreable = base_level != UNSUPPORTED and data_quality != UNSUPPORTED

        return MarketCapability(
            market=descriptor.to_dict(),
            support_level=base_level,
            data_quality=data_quality,
            confidence_cap=confidence_cap,
            scoreable=scoreable,
            required_snapshots=required,
            available_snapshots=available,
            missing_snapshots=missing,
            coverage_percent=coverage,
            warnings=warnings,
            reason=self._reason(descriptor, base_level, data_quality, coverage),
        )

    @staticmethod
    def _descriptor(market: str | MarketDescriptor | dict[str, Any]) -> MarketDescriptor:
        if isinstance(market, MarketDescriptor):
            return market
        if isinstance(market, dict):
            raw = market.get("raw") or market.get("canonical") or market.get("market") or ""
            return describe_market(
                raw,
                market_name=market.get("market_name") or "",
                outcome_name=market.get("outcome_name") or "",
                specifier=market.get("specifier") or "",
            )
        return describe_market(market)

    @staticmethod
    def _coverage(required, available, plan) -> float:
        if plan.get("coverage_percent") is not None:
            try:
                return round(float(plan.get("coverage_percent") or 0), 1)
            except (TypeError, ValueError):
                return 0.0
        required = list(required or [])
        if not required:
            return 0.0
        available_set = set(available or [])
        return round((sum(1 for item in required if item in available_set) / len(required)) * 100, 1)

    @staticmethod
    def _data_quality(base_level, coverage, missing):
        if base_level == UNSUPPORTED:
            return UNSUPPORTED
        if coverage >= 80:
            return "strong"
        if coverage >= 55:
            return "medium"
        if coverage >= 25:
            return "limited"
        return "poor" if missing else "limited"

    @staticmethod
    def _cap(base_cap, data_quality, descriptor):
        penalties = {
            "strong": 0,
            "medium": 8,
            "limited": 18,
            "poor": 30,
            UNSUPPORTED: base_cap,
        }
        cap = max(0, int(base_cap) - penalties.get(data_quality, 25))
        if descriptor.period not in {"match", ""}:
            cap = min(cap, 72)
        if descriptor.family.startswith("player_"):
            cap = min(cap, 70)
        return cap

    @staticmethod
    def _warnings(descriptor, base_level, data_quality, missing, coverage):
        warnings = []
        if base_level == UNSUPPORTED:
            warnings.append("unsupported_market_family")
        if data_quality in {"limited", "poor"}:
            warnings.append("low_statpal_coverage")
        if missing:
            warnings.append("missing_required_snapshots")
        if descriptor.period not in {"match", ""}:
            warnings.append("period_market_confidence_capped")
        if descriptor.family.startswith("player_"):
            warnings.append("player_market_needs_lineup_confirmation")
        if coverage == 0 and base_level != UNSUPPORTED:
            warnings.append("no_statpal_snapshots_available")
        return list(dict.fromkeys(warnings))

    @staticmethod
    def _reason(descriptor, base_level, data_quality, coverage):
        if base_level == UNSUPPORTED:
            return "This market family is recognized but not scoreable yet."
        return (
            f"{descriptor.family} is {base_level} supported with {data_quality} data quality "
            f"from {coverage}% StatPal snapshot coverage."
        )


market_capability_service = MarketCapabilityService()
