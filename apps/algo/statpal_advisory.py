from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from .evaluators.registry import HEURISTIC, NONE, SCORE_MATRIX_ENGINE, evaluator_for
from .market_taxonomy import MarketDescriptor, describe_market, normalize_market_text
from .models import ProviderPlayerMap


def _num(value, default=0.0) -> float:
    try:
        if value in (None, "", "-"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_rate(total, sample, *, scale=1.0) -> float:
    sample = _num(sample)
    if sample <= 0:
        return 0.0
    return round((_num(total) / sample) * scale, 3)


def _status(score: float) -> str:
    if score >= 78:
        return "strong"
    if score >= 66:
        return "playable"
    if score >= 55:
        return "caution"
    return "avoid"


@dataclass(frozen=True)
class StatPalAdvisory:
    available: bool
    score: float | None
    status: str
    basis: str
    evidence: dict[str, Any]
    warnings: list[str]
    message: str

    def to_dict(self):
        return asdict(self)


class StatPalMarketAdvisoryService:
    """
    Converts StatPal-shaped stats into Match Checker advisory signals.

    This does not replace the core top-pick engine. It gives Match Checker a
    separate stats-based path for wider bookmaker markets such as player goals,
    shots, cards, assists, saves, team totals, and corners.
    """

    def evaluate_market(
        self,
        market,
        *,
        fixture: dict[str, Any] | None = None,
        provider_payload: dict[str, Any] | None = None,
        statpal_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        descriptor = market if isinstance(market, MarketDescriptor) else describe_market(market)
        spec = evaluator_for(descriptor.family)
        if spec is None:
            return {
                **StatPalAdvisory(
                    available=False,
                    score=None,
                    status="unsupported",
                    basis="no_model_for_family",
                    evidence={"market_family": descriptor.family},
                    warnings=["no_model_for_family"],
                    message="This market family is recognised but not modelled yet.",
                ).to_dict(),
                "assessment_type": NONE,
                "market_family": descriptor.family,
            }

        if spec.engine == SCORE_MATRIX_ENGINE:
            from .evaluators import score_matrix_evaluator

            payload = score_matrix_evaluator.evaluate(descriptor, fixture=fixture)
            payload["assessment_type"] = spec.assessment_type
            payload["market_family"] = descriptor.family
            return payload

        # The family alone decides the handler. A data-requirement flag must never
        # hijack dispatch -- that is what routed team markets into the player model.
        handler = getattr(self, spec.handler)
        if spec.handler == "_evaluate_player_market":
            advisory = handler(descriptor, fixture=fixture, statpal_payload=statpal_payload)
        elif spec.handler in {"_evaluate_cards_market", "_evaluate_corners_market"}:
            advisory = handler(descriptor, fixture=fixture, provider_payload=provider_payload)
        else:
            advisory = handler(descriptor, fixture=fixture)

        payload = advisory.to_dict()
        payload["assessment_type"] = spec.assessment_type
        payload["market_family"] = descriptor.family
        if spec.assessment_type == HEURISTIC:
            # A constant-plus-nudges score is not a probability and must not be shown
            # as one. Consumers key off assessment_type to decide what they may claim.
            payload.setdefault("warnings", []).append("heuristic_assessment")
        return payload

    def _evaluate_player_market(self, descriptor: MarketDescriptor, *, fixture=None, statpal_payload=None) -> StatPalAdvisory:
        payload = statpal_payload or self._player_payload_from_mapping(descriptor.subject or descriptor.raw)
        if not payload:
            return StatPalAdvisory(
                available=False,
                score=None,
                status="needs_data",
                basis="player_stats_missing",
                evidence={"subject": descriptor.subject or descriptor.raw},
                warnings=["player_stats_missing"],
                message="We recognized this player market, but player stats are not available yet.",
            )

        player = payload.get("player") or {}
        seasons = self._club_rows(player)
        current = seasons[0] if seasons else {}
        totals = self._aggregate_rows(seasons[:3])
        appearances = _num(totals.get("appearances"))
        starts = _num(totals.get("starting_lineups"))
        minutes = _num(totals.get("minutes_played"))
        evidence = {
            "player_id": player.get("id") or "",
            "player_name": player.get("name") or descriptor.subject,
            "team": player.get("team") or current.get("team_name") or "",
            "position": player.get("position") or "",
            "sample_appearances": int(appearances),
            "sample_starts": int(starts),
            "sample_minutes": int(minutes),
            "starts_per_appearance": round(starts / appearances, 3) if appearances else 0,
            "minutes_per_appearance": round(minutes / appearances, 1) if appearances else 0,
            "goals_per_appearance": _safe_rate(totals.get("goals"), appearances),
            "assists_per_appearance": _safe_rate(totals.get("assists"), appearances),
            "shots_per_appearance": _safe_rate(totals.get("shots_total"), appearances),
            "shots_on_target_per_appearance": _safe_rate(totals.get("shots_on_target"), appearances),
            "cards_per_appearance": _safe_rate(_num(totals.get("yellowcards")) + _num(totals.get("redcards")), appearances),
            "saves_per_appearance": _safe_rate(totals.get("saves"), appearances),
            "rating": _num(current.get("rating"), None),
            "current_league": current.get("league") or "",
            "current_season": current.get("season") or "",
        }

        score, player_model_evidence, player_warnings = self._player_score(descriptor, evidence)
        evidence.update(player_model_evidence)
        warnings = []
        warnings.extend(player_warnings)
        if appearances < 5:
            warnings.append("small_player_sample")
            score -= 8
        if evidence["minutes_per_appearance"] and evidence["minutes_per_appearance"] < 55:
            warnings.append("low_minutes_sample")
            score -= 5
        if starts and appearances and starts / appearances < 0.45:
            warnings.append("rotation_risk")
            score -= 5
        score, snapshot_evidence, snapshot_warnings = self._apply_snapshot_context(
            score,
            descriptor=descriptor,
            fixture=fixture,
            player_team=evidence.get("team", ""),
        )
        evidence.update(snapshot_evidence)
        warnings.extend(snapshot_warnings)
        score = round(max(0, min(100, score)), 1)
        return StatPalAdvisory(
            available=True,
            score=score,
            status=_status(score),
            basis="statpal_player_stats",
            evidence=evidence,
            warnings=list(dict.fromkeys(warnings)),
            message=self._player_message(descriptor, evidence, score),
        )

    def _evaluate_cards_market(self, descriptor: MarketDescriptor, *, fixture=None, provider_payload=None) -> StatPalAdvisory:
        line = _num(descriptor.line, 3.5)
        expected_total, evidence, warnings = self._expected_card_profile(fixture, descriptor=descriptor)
        probability = self._goal_line_probability(expected_total, line, descriptor.selection or descriptor.side or "over")
        score = self._probability_score(probability, line=line, market_side=descriptor.selection or descriptor.side or "over")
        evidence = {
            **evidence,
            "line": line,
            "selection": descriptor.selection or descriptor.side or "over",
            "market_family": descriptor.family,
            "recognized": True,
            "estimated_probability": probability,
            "data_needed": ["league_card_rates", "team_card_rates", "referee_profile"],
        }
        if expected_total <= 0:
            warnings.append("cards_profile_missing")
            score = 50.0
        if abs(expected_total - line) < 0.45:
            warnings.append("thin_cards_edge")
            score -= 5
        score, snapshot_evidence, snapshot_warnings = self._apply_snapshot_context(score, descriptor=descriptor, fixture=fixture)
        evidence.update(snapshot_evidence)
        warnings.extend(snapshot_warnings)
        basis = "statpal_cards_market_model" if expected_total else "statpal_cards_advisory_stub"
        return StatPalAdvisory(
            available=True,
            score=round(max(0, min(100, score)), 1),
            status=_status(score),
            basis=basis,
            evidence=evidence,
            warnings=list(dict.fromkeys(warnings)),
            message=self._cards_message(descriptor, expected_total, line, probability),
        )

    def _evaluate_corners_market(self, descriptor: MarketDescriptor, *, fixture=None, provider_payload=None) -> StatPalAdvisory:
        line = _num(descriptor.line, 9.5)
        corner_profile = ((fixture or {}).get("corner_profile") or {}) if fixture else {}
        expected_total, statpal_evidence, statpal_warnings = self._expected_corner_profile(fixture, descriptor=descriptor)
        if not expected_total:
            expected_total = _num(corner_profile.get("expected_total"), 0)
        evidence = {
            **statpal_evidence,
            "line": line,
            "expected_total_corners": expected_total or None,
            "market_family": descriptor.family,
            "recognized": True,
        }
        if expected_total:
            probability = self._goal_line_probability(expected_total, line, descriptor.selection or descriptor.side or "over")
            score = self._probability_score(probability, line=line, market_side=descriptor.selection or descriptor.side or "over")
            evidence["estimated_probability"] = probability
            warnings = list(statpal_warnings)
            if abs(expected_total - line) < 0.8:
                warnings.append("thin_corner_edge")
                score -= 5
            basis = "statpal_corner_market_model" if statpal_evidence.get("corner_model_sources") else "fixture_corner_profile"
        else:
            score = 52
            warnings = ["corner_profile_missing", *statpal_warnings]
            basis = "statpal_corners_advisory_stub"
        score, snapshot_evidence, snapshot_warnings = self._apply_snapshot_context(score, descriptor=descriptor, fixture=fixture)
        evidence.update(snapshot_evidence)
        warnings.extend(snapshot_warnings)
        score = round(max(0, min(100, score)), 1)
        return StatPalAdvisory(
            available=True,
            score=score,
            status=_status(score),
            basis=basis,
            evidence=evidence,
            warnings=list(dict.fromkeys(warnings)),
            message=self._corners_message(descriptor, expected_total, line, evidence.get("estimated_probability")),
        )

    def _evaluate_total_goal_market(self, descriptor: MarketDescriptor, *, fixture=None) -> StatPalAdvisory:
        line = _num(descriptor.line, 2.5)
        expected_total, evidence, warnings = self._expected_total_goals(fixture)
        probability = self._goal_line_probability(expected_total, line, descriptor.selection or descriptor.side)
        score = self._probability_score(probability, line=line, market_side=descriptor.selection or descriptor.side)
        evidence.update({
            "line": line,
            "selection": descriptor.selection or descriptor.side,
            "market_family": descriptor.family,
            "recognized": True,
            "estimated_probability": probability,
        })
        if expected_total <= 0:
            warnings.append("goal_profile_missing")
        if abs(expected_total - line) < 0.35:
            warnings.append("thin_goal_edge")
            score -= 5
        score, snapshot_evidence, snapshot_warnings = self._apply_snapshot_context(score, descriptor=descriptor, fixture=fixture)
        evidence.update(snapshot_evidence)
        warnings.extend(snapshot_warnings)
        score = round(max(0, min(100, score)), 1)
        return StatPalAdvisory(
            available=True,
            score=score,
            status=_status(score),
            basis="statpal_goal_market_model",
            evidence=evidence,
            warnings=list(dict.fromkeys(warnings)),
            message=self._goal_message(descriptor, expected_total, line, probability),
        )

    def _evaluate_team_goal_market(self, descriptor: MarketDescriptor, *, fixture=None) -> StatPalAdvisory:
        team_side = descriptor.team or ("home" if "home" in normalize_market_text(descriptor.raw) else "away" if "away" in normalize_market_text(descriptor.raw) else "")
        line = _num(descriptor.line, 0.5)
        expected_team, evidence, warnings = self._expected_team_goals(fixture, team_side=team_side)
        probability = self._goal_line_probability(expected_team, line, descriptor.selection or descriptor.side or "over")
        score = self._probability_score(probability, line=line, market_side=descriptor.selection or descriptor.side or "over")
        evidence = {
            **evidence,
            "line": line,
            "selection": descriptor.selection or descriptor.side or "over",
            "team": team_side,
            "market_family": descriptor.family,
            "recognized": True,
            "estimated_probability": probability,
        }
        if expected_team <= 0:
            warnings = ["team_goal_profile_missing"]
        if abs(expected_team - line) < 0.25:
            warnings.append("thin_team_goal_edge")
            score -= 4
        score, snapshot_evidence, snapshot_warnings = self._apply_snapshot_context(score, descriptor=descriptor, fixture=fixture)
        evidence.update(snapshot_evidence)
        warnings.extend(snapshot_warnings)
        score = round(max(0, min(100, score)), 1)
        return StatPalAdvisory(
            available=True,
            score=score,
            status=_status(score),
            basis="statpal_team_goal_market_model",
            evidence=evidence,
            warnings=warnings,
            message=self._team_goal_message(descriptor, expected_team, line, probability, team_side),
        )

    # `_evaluate_fixture_context_market` was removed here. It returned a hardcoded 58
    # plus snapshot nudges for 1X2, Double Chance, DNB, BTTS and clean sheets -- the
    # highest-volume markets on the site -- and that score was rendered to users as
    # though it were modelled. Those families are now derived from the fitted score
    # distribution (apps.algo.evaluators.score_matrix_evaluator, ADR-001).

    def _expected_total_goals(self, fixture=None) -> tuple[float, dict[str, Any], list[str]]:
        fixture = fixture or {}
        context = fixture.get("fixture_context") or {}
        goal_model = context.get("goal_model") or {}
        statpal = self._statpal_summaries(fixture)
        predictions = statpal.get("predictions") or {}
        detailed = statpal.get("detailed_stats") or {}
        home_form = fixture.get("home_recent_form") or {}
        away_form = fixture.get("away_recent_form") or {}

        sources = []
        expected = _num(goal_model.get("expected_total"), 0)
        if expected:
            sources.append("fixture_goal_model")

        statpal_expected = (
            _num(detailed.get("expected_goals"), 0)
            or _num(predictions.get("expected_goals"), 0)
        )
        home_xg = _num(detailed.get("home_xg"), 0) or _num(predictions.get("home_xg"), 0)
        away_xg = _num(detailed.get("away_xg"), 0) or _num(predictions.get("away_xg"), 0)
        if not statpal_expected and home_xg and away_xg:
            statpal_expected = round(home_xg + away_xg, 2)
        if statpal_expected:
            sources.append("statpal_expected_goals")
            expected = round((expected * 0.55 + statpal_expected * 0.45), 2) if expected else statpal_expected

        form_expected = (
            _num(home_form.get("avg_scored") or home_form.get("goals_for_avg"), 0)
            + _num(away_form.get("avg_scored") or away_form.get("goals_for_avg"), 0)
        )
        if form_expected:
            sources.append("recent_scoring_form")
            expected = round((expected * 0.7 + form_expected * 0.3), 2) if expected else form_expected

        evidence = {
            "expected_total_goals": round(expected, 2) if expected else 0,
            "fixture_expected_goals": _num(goal_model.get("expected_total"), None),
            "statpal_expected_goals": statpal_expected or None,
            "statpal_home_xg": home_xg or None,
            "statpal_away_xg": away_xg or None,
            "form_expected_goals": round(form_expected, 2) if form_expected else None,
            "goal_model_sources": sources,
        }
        warnings = [] if expected else ["expected_goals_unavailable"]
        return round(expected, 2), evidence, warnings

    def _expected_team_goals(self, fixture=None, *, team_side="") -> tuple[float, dict[str, Any], list[str]]:
        fixture = fixture or {}
        statpal = self._statpal_summaries(fixture)
        predictions = statpal.get("predictions") or {}
        detailed = statpal.get("detailed_stats") or {}
        home_form = fixture.get("home_recent_form") or {}
        away_form = fixture.get("away_recent_form") or {}
        team_side = team_side if team_side in {"home", "away"} else ""

        home_expected = (
            _num(detailed.get("home_xg"), 0)
            or _num(predictions.get("home_xg"), 0)
            or self._team_form_expectation(home_form, away_form)
        )
        away_expected = (
            _num(detailed.get("away_xg"), 0)
            or _num(predictions.get("away_xg"), 0)
            or self._team_form_expectation(away_form, home_form)
        )
        if team_side == "home":
            expected = home_expected
        elif team_side == "away":
            expected = away_expected
        else:
            expected = max(home_expected, away_expected)

        evidence = {
            "expected_team_goals": round(expected, 2) if expected else 0,
            "home_expected_goals": round(home_expected, 2) if home_expected else None,
            "away_expected_goals": round(away_expected, 2) if away_expected else None,
            "team_goal_model_source": "statpal_xg" if (_num(detailed.get("home_xg"), 0) or _num(detailed.get("away_xg"), 0)) else "recent_form",
        }
        warnings = []
        if not expected:
            warnings.append("team_expected_goals_unavailable")
        if not team_side:
            warnings.append("team_side_inferred_from_best_profile")
        return round(expected, 2), evidence, warnings

    def _expected_corner_profile(self, fixture=None, *, descriptor: MarketDescriptor) -> tuple[float, dict[str, Any], list[str]]:
        detailed = self._statpal_summaries(fixture).get("detailed_stats") or {}
        home_corners = _num(detailed.get("home_corners"), 0)
        away_corners = _num(detailed.get("away_corners"), 0)
        team_side = descriptor.team if descriptor.team in {"home", "away"} else ""
        sources = []
        if home_corners or away_corners:
            sources.append("statpal_detailed_stats")

        if team_side == "home":
            expected = home_corners
        elif team_side == "away":
            expected = away_corners
        else:
            expected = home_corners + away_corners

        evidence = {
            "home_expected_corners": round(home_corners, 2) if home_corners else None,
            "away_expected_corners": round(away_corners, 2) if away_corners else None,
            "corner_model_sources": sources,
        }
        warnings = [] if expected else ["statpal_corner_profile_missing"]
        return round(expected, 2), evidence, warnings

    def _expected_card_profile(self, fixture=None, *, descriptor: MarketDescriptor) -> tuple[float, dict[str, Any], list[str]]:
        detailed = self._statpal_summaries(fixture).get("detailed_stats") or {}
        home_yellows = _num(detailed.get("home_yellow_cards"), 0)
        away_yellows = _num(detailed.get("away_yellow_cards"), 0)
        home_reds = _num(detailed.get("home_red_cards"), 0)
        away_reds = _num(detailed.get("away_red_cards"), 0)
        total_cards = _num(detailed.get("total_cards"), 0)
        booking_points = _num(detailed.get("booking_points"), 0)
        team_side = descriptor.team if descriptor.team in {"home", "away"} else ""

        if descriptor.family == "booking_points":
            if not booking_points and any([home_yellows, away_yellows, home_reds, away_reds]):
                booking_points = (home_yellows + away_yellows) * 10 + (home_reds + away_reds) * 25
            expected = booking_points
        elif team_side == "home":
            expected = home_yellows + home_reds
        elif team_side == "away":
            expected = away_yellows + away_reds
        else:
            expected = total_cards or home_yellows + away_yellows + home_reds + away_reds

        evidence = {
            "expected_cards": round(expected, 2) if expected else 0,
            "home_yellow_cards": round(home_yellows, 2) if home_yellows else None,
            "away_yellow_cards": round(away_yellows, 2) if away_yellows else None,
            "home_red_cards": round(home_reds, 2) if home_reds else None,
            "away_red_cards": round(away_reds, 2) if away_reds else None,
            "total_cards": round(total_cards, 2) if total_cards else None,
            "booking_points": round(booking_points, 2) if booking_points else None,
            "card_model_sources": ["statpal_detailed_stats"] if expected else [],
        }
        warnings = [] if expected else ["statpal_cards_profile_missing"]
        return round(expected, 2), evidence, warnings

    @staticmethod
    def _team_form_expectation(attacking_form, defending_form) -> float:
        scored = _num((attacking_form or {}).get("avg_scored") or (attacking_form or {}).get("goals_for_avg"), 0)
        conceded = _num((defending_form or {}).get("avg_conceded") or (defending_form or {}).get("goals_against_avg"), 0)
        if scored and conceded:
            return round(scored * 0.65 + conceded * 0.35, 2)
        return scored or 0

    @staticmethod
    def _goal_line_probability(expected_goals: float, line: float, market_side: str) -> float:
        expected_goals = max(0.05, _num(expected_goals, 0.05))
        threshold = math.floor(line)
        under_probability = sum(
            math.exp(-expected_goals) * expected_goals**goals / math.factorial(goals)
            for goals in range(0, threshold + 1)
        )
        under_probability = max(0.0, min(1.0, under_probability))
        if str(market_side or "").lower() == "under":
            return round(under_probability * 100, 1)
        return round((1 - under_probability) * 100, 1)

    @staticmethod
    def _probability_score(probability: float, *, line: float, market_side: str) -> float:
        score = 35 + _num(probability, 0) * 0.55
        if line >= 4.5 and market_side == "over":
            score -= 6
        if line <= 0.5 and market_side == "over":
            score += 4
        return score

    @staticmethod
    def _statpal_summaries(fixture=None) -> dict[str, Any]:
        snapshots = (((fixture or {}).get("statpal_context") or {}).get("snapshots") or {})
        return {
            key: (value.get("summary") or {})
            for key, value in snapshots.items()
            if isinstance(value, dict)
        }

    @staticmethod
    def _goal_message(descriptor: MarketDescriptor, expected_total: float, line: float, probability: float) -> str:
        side = (descriptor.selection or descriptor.side or "over").title()
        return (
            f"{side} {line} is rated from an expected-goals profile of {round(expected_total, 2)} "
            f"and an estimated hit probability of {probability}%."
        )

    @staticmethod
    def _team_goal_message(descriptor: MarketDescriptor, expected_team: float, line: float, probability: float, team_side: str) -> str:
        side = team_side.title() if team_side else "Selected team"
        selection = (descriptor.selection or descriptor.side or "over").title()
        return (
            f"{side} {selection} {line} is rated from an estimated team-goals profile of "
            f"{round(expected_team, 2)} and an estimated hit probability of {probability}%."
        )

    @staticmethod
    def _corners_message(descriptor: MarketDescriptor, expected_total: float, line: float, probability: float | None) -> str:
        side = (descriptor.selection or descriptor.side or "over").title()
        if probability is None:
            return "Corners market recognized, but corner profiles are not available yet."
        return (
            f"{side} {line} corners is rated from an expected-corners profile of "
            f"{round(expected_total, 2)} and an estimated hit probability of {probability}%."
        )

    @staticmethod
    def _cards_message(descriptor: MarketDescriptor, expected_total: float, line: float, probability: float) -> str:
        side = (descriptor.selection or descriptor.side or "over").title()
        unit = "booking points" if descriptor.family == "booking_points" else "cards"
        if expected_total <= 0:
            return f"{unit.title()} market recognized. Full confidence needs StatPal card/team/referee profiles."
        return (
            f"{side} {line} {unit} is rated from an expected {unit} profile of "
            f"{round(expected_total, 2)} and an estimated hit probability of {probability}%."
        )

    def _player_payload_from_mapping(self, subject: str) -> dict[str, Any] | None:
        normalized = normalize_market_text(subject)
        tokens = [token for token in normalized.split() if token not in {"player", "to", "score", "shots", "shot", "target", "assist", "carded", "card"}]
        if not tokens:
            return None
        query = " ".join(tokens)
        mapping = (
            ProviderPlayerMap.objects.filter(
                provider="statpal",
                active=True,
                provider_player_normalized__icontains=query,
            )
            .order_by("-confidence", "-verified_at", "-updated_at")
            .first()
        )
        if mapping and mapping.payload:
            return mapping.payload
        return None

    def _apply_snapshot_context(
        self,
        score: float,
        *,
        descriptor: MarketDescriptor,
        fixture: dict[str, Any] | None = None,
        player_team: str = "",
    ) -> tuple[float, dict[str, Any], list[str]]:
        context = (fixture or {}).get("statpal_context") or {}
        snapshots = context.get("snapshots") or {}
        if not snapshots:
            return score, {"statpal_snapshots_available": False}, ["statpal_fixture_snapshots_missing"]

        evidence = {
            "statpal_snapshots_available": True,
            "statpal_snapshot_types": sorted(snapshots.keys()),
        }
        warnings = []
        injuries = ((snapshots.get("injuries_suspensions") or {}).get("summary") or {})
        if injuries:
            injury_adjustment, injury_evidence, injury_warnings = self._injury_adjustment(
                injuries,
                descriptor=descriptor,
                fixture=fixture,
                player_team=player_team,
            )
            score += injury_adjustment
            evidence["injury_adjustment"] = round(injury_adjustment, 1)
            evidence["injuries"] = injury_evidence
            warnings.extend(injury_warnings)

        lineups = ((snapshots.get("lineups") or {}).get("summary") or {})
        if lineups:
            adjustment = self._lineup_adjustment(lineups)
            score += adjustment
            evidence["lineup_adjustment"] = round(adjustment, 1)
            evidence["lineups"] = lineups
            if adjustment < 0:
                warnings.append("lineup_uncertainty")

        odds = ((snapshots.get("prematch_odds") or {}).get("summary") or {})
        if odds:
            adjustment = self._odds_adjustment(odds)
            score += adjustment
            evidence["odds_adjustment"] = round(adjustment, 1)
            evidence["odds_snapshot"] = odds
            if adjustment < 0:
                warnings.append("odds_snapshot_caution")

        team_stats = ((snapshots.get("team_stats") or {}).get("summary") or {})
        if team_stats:
            evidence["team_stats_snapshot"] = team_stats
            if team_stats.get("sample_size") and _num(team_stats.get("sample_size")) < 5:
                score -= 4
                warnings.append("small_team_stat_sample")

        return max(0, min(100, score)), evidence, list(dict.fromkeys(warnings))

    @staticmethod
    def _injury_adjustment(injuries, *, descriptor: MarketDescriptor, fixture=None, player_team=""):
        home = injuries.get("home") or {}
        away = injuries.get("away") or {}
        home_missing = _num(home.get("to_miss_count"))
        away_missing = _num(away.get("to_miss_count"))
        home_questionable = _num(home.get("questionable_count"))
        away_questionable = _num(away.get("questionable_count"))
        evidence = {
            "home_to_miss": int(home_missing),
            "away_to_miss": int(away_missing),
            "home_questionable": int(home_questionable),
            "away_questionable": int(away_questionable),
            "home_availability_risk": home.get("availability_risk", ""),
            "away_availability_risk": away.get("availability_risk", ""),
        }
        warnings = []
        adjustment = 0.0
        target_side = descriptor.side
        if player_team:
            player_norm = normalize_market_text(player_team)
            if player_norm and player_norm in normalize_market_text(home.get("team_name")):
                target_side = "home"
            elif player_norm and player_norm in normalize_market_text(away.get("team_name")):
                target_side = "away"

        if descriptor.requires_player_stats:
            missing = home_missing if target_side == "home" else away_missing if target_side == "away" else max(home_missing, away_missing)
            questionable = home_questionable if target_side == "home" else away_questionable if target_side == "away" else max(home_questionable, away_questionable)
            adjustment -= min(10, missing * 2 + questionable)
            if missing or questionable:
                warnings.append("player_team_availability_risk")
        elif descriptor.family in {"team_total_goals", "total_goals", "btts"}:
            total_missing = home_missing + away_missing
            if descriptor.side == "over" or descriptor.family == "btts":
                adjustment -= min(9, total_missing * 1.3)
            elif descriptor.side == "under":
                adjustment += min(5, total_missing * 0.8)
            if total_missing >= 3:
                warnings.append("team_news_affects_goal_market")
        elif descriptor.family in {"cards_total", "cards", "player_card"}:
            total_missing = home_missing + away_missing + home_questionable + away_questionable
            adjustment += min(4, total_missing * 0.4)

        return adjustment, evidence, warnings

    @staticmethod
    def _lineup_adjustment(summary):
        confirmed = summary.get("confirmed")
        projected = summary.get("projected")
        if confirmed is True:
            return 3.0
        if projected is True:
            return 1.0
        unavailable = summary.get("available") is False or summary.get("lineups_available") is False
        return -3.0 if unavailable else 0.0

    @staticmethod
    def _odds_adjustment(summary):
        edge = _num(summary.get("edge") or summary.get("value_edge"), 0)
        spread = _num(summary.get("spread_pct"), 0)
        adjustment = max(-6, min(6, edge * 30))
        if spread and spread > 20:
            adjustment -= 3
        return adjustment

    @staticmethod
    def _club_rows(player: dict[str, Any]) -> list[dict[str, Any]]:
        stats = player.get("club_league_statistics") or {}
        rows = stats.get("club") or []
        if isinstance(rows, dict):
            rows = [rows]
        return [row for row in rows if isinstance(row, dict)]

    @staticmethod
    def _aggregate_rows(rows: list[dict[str, Any]]) -> dict[str, float]:
        keys = {
            "minutes_played",
            "appearances",
            "starting_lineups",
            "assists",
            "goals",
            "shots_total",
            "shots_on_target",
            "yellowcards",
            "redcards",
            "saves",
        }
        return {key: sum(_num(row.get(key)) for row in rows) for key in keys}

    def _player_score(self, descriptor: MarketDescriptor, evidence: dict[str, Any]) -> tuple[float, dict[str, Any], list[str]]:
        line = self._player_market_line(descriptor)
        expected = self._player_expected_metric(descriptor, evidence)
        probability = self._goal_line_probability(expected, line, descriptor.selection or descriptor.side or "over")
        score = 40 + probability * 0.55
        warnings = []
        if expected <= 0:
            warnings.append("player_market_stat_missing")
            score = 45
        if descriptor.family == "player_goal":
            score += min(8, evidence.get("shots_on_target_per_appearance", 0) * 10)
        if descriptor.family == "player_shots":
            score += min(6, evidence.get("shots_on_target_per_appearance", 0) * 4)
        if descriptor.family in {"player_assist", "player_card", "player_saves"}:
            score -= 2
        if abs(expected - line) < 0.25:
            warnings.append("thin_player_market_edge")
            score -= 4
        if evidence.get("sample_appearances", 0) < 10:
            warnings.append("limited_player_sample")
        return score, {
            "line": line,
            "selection": descriptor.selection or descriptor.side or "over",
            "expected_player_metric": round(expected, 3),
            "estimated_probability": probability,
            "player_market_family": descriptor.family,
        }, warnings

    @staticmethod
    def _player_market_line(descriptor: MarketDescriptor) -> float:
        if descriptor.family in {"player_goal", "player_card", "player_assist"}:
            return _num(descriptor.line, 0.5)
        if descriptor.family == "player_shots_on_target":
            return _num(descriptor.line, 0.5)
        if descriptor.family == "player_shots":
            return _num(descriptor.line, 1.5)
        if descriptor.family == "player_saves":
            return _num(descriptor.line, 2.5)
        return _num(descriptor.line, 0.5)

    @staticmethod
    def _player_expected_metric(descriptor: MarketDescriptor, evidence: dict[str, Any]) -> float:
        if descriptor.family == "player_goal":
            return _num(evidence.get("goals_per_appearance"), 0)
        if descriptor.family == "player_assist":
            return _num(evidence.get("assists_per_appearance"), 0)
        if descriptor.family == "player_shots_on_target":
            return _num(evidence.get("shots_on_target_per_appearance"), 0)
        if descriptor.family == "player_shots":
            return _num(evidence.get("shots_per_appearance"), 0)
        if descriptor.family == "player_card":
            return _num(evidence.get("cards_per_appearance"), 0)
        if descriptor.family == "player_saves":
            return _num(evidence.get("saves_per_appearance"), 0)
        return 0

    @staticmethod
    def _player_message(descriptor: MarketDescriptor, evidence: dict[str, Any], score: float) -> str:
        player = evidence.get("player_name") or "This player"
        probability = evidence.get("estimated_probability")
        line = evidence.get("line")
        if descriptor.family == "player_goal":
            return f"{player} averages {evidence['goals_per_appearance']} goals and {evidence['shots_on_target_per_appearance']} shots on target per appearance, with an estimated {probability}% chance against line {line}."
        if descriptor.family == "player_assist":
            return f"{player} averages {evidence['assists_per_appearance']} assists per appearance, with an estimated {probability}% chance against line {line}."
        if descriptor.family in {"player_shots", "player_shots_on_target"}:
            return f"{player} averages {evidence['shots_per_appearance']} shots and {evidence['shots_on_target_per_appearance']} shots on target per appearance, with an estimated {probability}% chance against line {line}."
        if descriptor.family == "player_card":
            return f"{player} averages {evidence['cards_per_appearance']} cards per appearance, with an estimated {probability}% chance against line {line}."
        if descriptor.family == "player_saves":
            return f"{player} averages {evidence['saves_per_appearance']} saves per appearance, with an estimated {probability}% chance against line {line}."
        return f"{player} has a StatPal advisory score of {score} for this market."


statpal_market_advisory = StatPalMarketAdvisoryService()
