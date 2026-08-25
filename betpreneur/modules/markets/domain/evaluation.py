"""
Market evaluator registry.

**The family selects the evaluator. Nothing else.** Data requirements say what to
*fetch*, never what to *run*. The previous cascade checked `descriptor.requires_player_stats`
first, so `First to Score H` — a team market — was routed into the player-props model
with a nonsense subject, and the `first_to_score` branch below it was unreachable.

Each entry also declares its `assessment_type`, which carries the honesty invariant:

* ``quantitative_model`` — a probability was computed from data, and may be published.
* ``heuristic`` — a score derived from a constant plus context nudges. Useful as a
  signal, but it must never be rendered as a probability.
* absent from the registry — the family is recognised but unmodelled, and is reported
  as such rather than mapped to a nearby model.
"""

from __future__ import annotations

from dataclasses import dataclass

from .data_capability import DataCapability as Cap

QUANTITATIVE = "quantitative_model"
HEURISTIC = "heuristic"
NONE = "none"


STATPAL_ENGINE = "statpal_advisory"
SCORE_MATRIX_ENGINE = "score_matrix"
COUNT_MODEL_ENGINE = "count_model"


@dataclass(frozen=True)
class EvaluatorSpec:
    family: str
    handler: str          # method name on StatPalMarketAdvisoryService, or "" for other engines
    assessment_type: str
    required: tuple[Cap, ...] = ()
    optional: tuple[Cap, ...] = ()
    notes: str = ""
    engine: str = STATPAL_ENGINE

    @property
    def publishes_probability(self) -> bool:
        return self.assessment_type == QUANTITATIVE


_GOAL_CORE = (Cap.TEAM_GOALS_FOR, Cap.TEAM_GOALS_AGAINST)
_GOAL_RICH = (Cap.TEAM_SHOTS, Cap.TEAM_POSSESSION, Cap.MARKET_ODDS, Cap.INJURIES)
_CARD_CORE = (Cap.TEAM_CARDS, Cap.TEAM_FOULS)
_PLAYER_CORE = (Cap.PLAYER_SEASON_STATS,)
_SOT_CORE = (Cap.TEAM_SHOTS_ON_TARGET,)


MARKET_EVALUATORS: dict[str, EvaluatorSpec] = {
    # --- quantitative: expected value -> line probability -> score ---
    "total_goals": EvaluatorSpec(
        "total_goals", "_evaluate_total_goal_market", QUANTITATIVE, _GOAL_CORE, _GOAL_RICH
    ),
    "team_total_goals": EvaluatorSpec(
        "team_total_goals", "_evaluate_team_goal_market", QUANTITATIVE, _GOAL_CORE, _GOAL_RICH
    ),
    "both_halves_total_goals": EvaluatorSpec(
        "both_halves_total_goals",
        "_evaluate_both_halves_total_goal_market",
        QUANTITATIVE,
        _GOAL_CORE,
        _GOAL_RICH,
        notes="approximated from first-half and second-half goal rates",
    ),
    # Corners and cards come from cached team rate profiles, not snapshots. The old
    # evaluators read fields the match-stats endpoint does not carry, so they returned a
    # constant that ignored the line while claiming to be quantitative.
    "corners_total": EvaluatorSpec(
        "corners_total", "", QUANTITATIVE, (Cap.TEAM_CORNERS,),
        (Cap.MARKET_ODDS, Cap.TEAM_POSSESSION), engine=COUNT_MODEL_ENGINE,
    ),
    "team_corners": EvaluatorSpec(
        "team_corners", "", QUANTITATIVE, (Cap.TEAM_CORNERS,),
        (Cap.MARKET_ODDS, Cap.TEAM_POSSESSION), engine=COUNT_MODEL_ENGINE,
    ),
    "corner_range": EvaluatorSpec(
        "corner_range", "", QUANTITATIVE, (Cap.TEAM_CORNERS,),
        (Cap.MARKET_ODDS, Cap.TEAM_POSSESSION), engine=COUNT_MODEL_ENGINE,
    ),
    "team_corner_range": EvaluatorSpec(
        "team_corner_range", "", QUANTITATIVE, (Cap.TEAM_CORNERS,),
        (Cap.MARKET_ODDS, Cap.TEAM_POSSESSION), engine=COUNT_MODEL_ENGINE,
    ),
    "corners_result": EvaluatorSpec(
        "corners_result", "", QUANTITATIVE, (Cap.TEAM_CORNERS,),
        (Cap.MARKET_ODDS, Cap.TEAM_POSSESSION), engine=COUNT_MODEL_ENGINE,
    ),
    "corner_handicap": EvaluatorSpec(
        "corner_handicap", "", QUANTITATIVE, (Cap.TEAM_CORNERS,),
        (Cap.MARKET_ODDS, Cap.TEAM_POSSESSION), engine=COUNT_MODEL_ENGINE,
    ),
    "cards_total": EvaluatorSpec(
        "cards_total", "", QUANTITATIVE, _CARD_CORE,
        (Cap.REFEREE, Cap.LINEUP_PROJECTED, Cap.MARKET_ODDS), engine=COUNT_MODEL_ENGINE,
    ),
    "cards_result": EvaluatorSpec(
        "cards_result", "", QUANTITATIVE, _CARD_CORE,
        (Cap.REFEREE, Cap.LINEUP_PROJECTED, Cap.MARKET_ODDS), engine=COUNT_MODEL_ENGINE,
    ),
    "team_cards": EvaluatorSpec(
        "team_cards", "", QUANTITATIVE, _CARD_CORE,
        (Cap.REFEREE, Cap.LINEUP_PROJECTED, Cap.MARKET_ODDS), engine=COUNT_MODEL_ENGINE,
    ),
    "cards": EvaluatorSpec(
        "cards", "", QUANTITATIVE, _CARD_CORE,
        (Cap.REFEREE, Cap.MARKET_ODDS), engine=COUNT_MODEL_ENGINE,
    ),
    "booking_points": EvaluatorSpec(
        "booking_points", "", QUANTITATIVE, _CARD_CORE,
        (Cap.REFEREE, Cap.LINEUP_PROJECTED, Cap.MARKET_ODDS), engine=COUNT_MODEL_ENGINE,
    ),
    "shots_on_target_total": EvaluatorSpec(
        "shots_on_target_total", "", QUANTITATIVE, _SOT_CORE,
        (Cap.MARKET_ODDS,), engine=COUNT_MODEL_ENGINE,
    ),
    "team_shots_on_target": EvaluatorSpec(
        "team_shots_on_target", "", QUANTITATIVE, _SOT_CORE,
        (Cap.MARKET_ODDS,), engine=COUNT_MODEL_ENGINE,
    ),
    "player_goal": EvaluatorSpec(
        "player_goal", "_evaluate_player_market", QUANTITATIVE, _PLAYER_CORE,
        (Cap.LINEUP_CONFIRMED, Cap.LINEUP_PROJECTED, Cap.INJURIES, Cap.MARKET_ODDS),
    ),
    "player_card": EvaluatorSpec(
        "player_card", "_evaluate_player_market", QUANTITATIVE, _PLAYER_CORE,
        (Cap.LINEUP_CONFIRMED, Cap.LINEUP_PROJECTED, Cap.REFEREE, Cap.INJURIES),
    ),
    "player_shots": EvaluatorSpec(
        "player_shots", "_evaluate_player_market", QUANTITATIVE, _PLAYER_CORE,
        (Cap.LINEUP_CONFIRMED, Cap.LINEUP_PROJECTED, Cap.INJURIES),
    ),
    "player_shots_on_target": EvaluatorSpec(
        "player_shots_on_target", "_evaluate_player_market", QUANTITATIVE, _PLAYER_CORE,
        (Cap.LINEUP_CONFIRMED, Cap.LINEUP_PROJECTED, Cap.INJURIES),
    ),
    "player_assist": EvaluatorSpec(
        "player_assist", "_evaluate_player_market", QUANTITATIVE, _PLAYER_CORE,
        (Cap.LINEUP_CONFIRMED, Cap.LINEUP_PROJECTED, Cap.INJURIES),
    ),
    "player_saves": EvaluatorSpec(
        "player_saves", "_evaluate_player_market", QUANTITATIVE, _PLAYER_CORE,
        (Cap.LINEUP_CONFIRMED, Cap.LINEUP_PROJECTED, Cap.INJURIES),
    ),

    # --- derived from the shared score distribution (ADR-001) ---
    # One fitted model per fixture, so these cannot contradict one another:
    # P(1X) is exactly P(home) + P(draw).
    **{
        family: EvaluatorSpec(
            family, "", QUANTITATIVE, _GOAL_CORE, _GOAL_RICH, engine=SCORE_MATRIX_ENGINE
        )
        for family in (
            "match_result",
            "double_chance",
            "draw_no_bet",
            "btts",
            "result_btts",
            "clean_sheet",
            "result_total_goals",
            "total_btts",
            "double_chance_btts",
            "double_chance_total_goals",
            "result_or_total_goals",
            "result_or_btts",
            "result_or_clean_sheet",
            "odd_even",
            "asian_handicap",
            "handicap",
        )
    },
    "first_to_score": EvaluatorSpec(
        "first_to_score", "", QUANTITATIVE, _GOAL_CORE, _GOAL_CORE + (Cap.GOAL_MINUTE_DIST,),
        engine=SCORE_MATRIX_ENGINE,
        notes="approximated from scoring rates; not a goal-timing model",
    ),
}

# Goal-volume families also come from the matrix, overriding the StatPal advisory path.
for _family in ("total_goals", "team_total_goals"):
    MARKET_EVALUATORS[_family] = EvaluatorSpec(
        _family, "", QUANTITATIVE, _GOAL_CORE, _GOAL_RICH, engine=SCORE_MATRIX_ENGINE
    )


def evaluator_for(family: str) -> EvaluatorSpec | None:
    return MARKET_EVALUATORS.get(str(family or ""))


def assessment_type_for(family: str) -> str:
    spec = evaluator_for(family)
    return spec.assessment_type if spec else NONE


def required_capabilities(families) -> set[Cap]:
    """
    Union of what every leg on a fixture needs.

    One planner, shared by the Match Checker, core scoring and alternative generation,
    so the same fixture cannot end up with different analytical quality depending on
    which route hydrated it.
    """
    needed: set[Cap] = set()
    for family in families or ():
        spec = evaluator_for(family)
        if spec:
            needed.update(spec.required)
            needed.update(spec.optional)
    return needed


def modelled_families() -> set[str]:
    return {family for family, spec in MARKET_EVALUATORS.items() if spec.publishes_probability}
