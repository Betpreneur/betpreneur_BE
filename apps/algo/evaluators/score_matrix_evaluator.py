"""
Evaluate goals-and-result markets from the shared score distribution.

Replaces `_evaluate_fixture_context_market`, which returned a hardcoded 58 plus
snapshot nudges for the highest-volume markets on the site. Every family here now
derives from one fitted distribution, so the answers are real probabilities and cannot
contradict each other.

When no fit is available for the fixture the evaluator returns *unavailable* rather
than falling back to a default matrix. A number produced from league-average
placeholders would look identical to a modelled one, which is the failure this whole
redesign exists to remove.
"""

from __future__ import annotations

from ..scoring import derive
from ..scoring.service import score_model_service


def _line(descriptor) -> float | None:
    try:
        return float(descriptor.line)
    except (TypeError, ValueError):
        return None


def _fixture_ids(fixture):
    fixture = fixture or {}
    return {
        "league_id": str(fixture.get("code") or fixture.get("league_id") or ""),
        "home_team_name": fixture.get("hname") or fixture.get("home_team") or "",
        "away_team_name": fixture.get("aname") or fixture.get("away_team") or "",
        "home_team_id": str(fixture.get("statpal_home_team_id") or fixture.get("hid") or ""),
        "away_team_id": str(fixture.get("statpal_away_team_id") or fixture.get("aid") or ""),
    }


def _outcome_probability(descriptor, matrix):
    """Map a canonical family + side + line onto the matrix. Returns (probability, push)."""
    family, side, line = descriptor.family, (descriptor.side or "").lower(), _line(descriptor)
    team = descriptor.team or ("home" if side == "home" else "away" if side == "away" else "")

    if family == "match_result":
        return {"home": derive.home_win, "draw": derive.draw, "away": derive.away_win}.get(
            side, lambda _m: None
        )(matrix), 0.0

    if family == "double_chance":
        return derive.double_chance(matrix, side), 0.0

    if family == "draw_no_bet":
        outcome = derive.draw_no_bet(matrix, side or "home")
        return outcome.probability, outcome.push

    if family == "btts":
        return derive.btts(matrix, side != "no"), 0.0

    if family == "result_btts":
        return derive.result_btts(matrix, side), 0.0

    if family == "clean_sheet":
        return derive.clean_sheet(matrix, team or "home"), 0.0

    if family == "total_goals" and line is not None:
        outcome = derive.total_goals(matrix, line, side or "over")
        return outcome.probability, outcome.push

    if family == "result_total_goals" and line is not None:
        outcome = derive.result_total_goals(matrix, line, side)
        return outcome.probability, outcome.push

    if family == "total_btts" and line is not None:
        outcome = derive.total_btts(matrix, line, side)
        return outcome.probability, outcome.push

    if family == "double_chance_btts":
        return derive.double_chance_btts(matrix, side), 0.0

    if family == "double_chance_total_goals" and line is not None:
        outcome = derive.double_chance_total_goals(matrix, line, side)
        return outcome.probability, outcome.push

    if family == "result_or_total_goals" and line is not None:
        outcome = derive.result_or_total_goals(matrix, line, side)
        return outcome.probability, outcome.push

    if family == "result_or_btts":
        return derive.result_or_btts(matrix, side), 0.0

    if family == "result_or_clean_sheet":
        return derive.result_or_clean_sheet(matrix, side), 0.0

    if family == "team_total_goals" and line is not None:
        outcome = derive.team_total_goals(matrix, line, team=team or "home", side=side or "over")
        return outcome.probability, outcome.push

    if family == "odd_even":
        return derive.odd_even(matrix, side or "odd"), 0.0

    if family == "asian_handicap" and line is not None:
        outcome = derive.asian_handicap(matrix, line, team=team or "home")
        return outcome.probability, outcome.push

    if family == "handicap" and line is not None:
        return derive.european_handicap(matrix, line, side or "home"), 0.0

    if family == "first_to_score":
        # Which side scores first, approximated by which side scores at all more often.
        # Not a timing model; flagged as such by its wider confidence cap.
        home = derive.team_total_goals(matrix, 0.5, team="home", side="over").win
        away = derive.team_total_goals(matrix, 0.5, team="away", side="over").win
        total = home + away
        if total <= 0:
            return None, 0.0
        return (home if (team or side) == "home" else away) / total, 0.0

    return None, 0.0


def evaluate(descriptor, *, fixture=None, **_ignored) -> dict:
    ids = _fixture_ids(fixture)
    rates = score_model_service.rates_for_fixture(**ids)

    if not rates.usable:
        return {
            "available": False,
            "score": None,
            "status": "needs_data",
            "basis": "score_matrix_no_fit",
            "evidence": {
                "market_family": descriptor.family,
                "league_id": rates.league_id,
                "matched_home_team": rates.matched_home,
                "matched_away_team": rates.matched_away,
            },
            "warnings": ["no_fitted_score_model"],
            "message": "No fitted goal model is available for this fixture yet.",
        }

    matrix = rates.matrix()
    probability, push = _outcome_probability(descriptor, matrix)
    if probability is None:
        return {
            "available": False,
            "score": None,
            "status": "unsupported",
            "basis": "score_matrix_unmapped_outcome",
            "evidence": {"market_family": descriptor.family, "side": descriptor.side},
            "warnings": ["outcome_not_derivable"],
            "message": "This outcome could not be derived from the score model.",
        }

    expected_home, expected_away = matrix.expected_goals()
    return {
        "available": True,
        "score": round(probability * 100, 1),
        "probability": round(probability, 6),
        "push_probability": round(push, 6),
        "status": "modelled",
        "basis": "score_matrix",
        "evidence": {
            "market_family": descriptor.family,
            "expected_goals_home": expected_home,
            "expected_goals_away": expected_away,
            "league_id": rates.league_id,
            "model_version": rates.model_version,
            "data_quality": rates.data_quality,
            "push_probability": round(push, 6),
        },
        "warnings": [] if rates.data_quality in {"strong", "medium"} else ["thin_league_sample"],
        "message": (
            f"Derived from a fitted goal model: {expected_home} expected home goals, "
            f"{expected_away} away."
        ),
    }
