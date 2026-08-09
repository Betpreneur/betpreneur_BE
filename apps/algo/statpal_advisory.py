from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from typing import Any

from .evaluators.registry import (
    COUNT_MODEL_ENGINE,
    HEURISTIC,
    NONE,
    SCORE_MATRIX_ENGINE,
    evaluator_for,
)
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

        if spec.engine in {SCORE_MATRIX_ENGINE, COUNT_MODEL_ENGINE}:
            if spec.engine == SCORE_MATRIX_ENGINE:
                from .evaluators import score_matrix_evaluator as engine
            else:
                from .evaluators import count_market_evaluator as engine

            payload = engine.evaluate(descriptor, fixture=fixture)
            payload["assessment_type"] = spec.assessment_type
            payload["market_family"] = descriptor.family
            payload = self._apply_odds_overlay(
                payload,
                descriptor=descriptor,
                fixture=fixture,
                provider_payload=provider_payload,
            )
            if spec.engine == SCORE_MATRIX_ENGINE and not payload.get("available"):
                fallback = self._score_matrix_fallback(
                    descriptor,
                    fixture=fixture,
                    provider_payload=provider_payload,
                )
                if fallback is not None and fallback.get("available"):
                    fallback.setdefault("warnings", []).append("score_matrix_fit_missing")
                    fallback["assessment_type"] = spec.assessment_type
                    fallback["market_family"] = descriptor.family
                    return fallback
            return payload

        if spec.handler == "_evaluate_player_market":
            unavailable = self._player_availability_block(descriptor, fixture)
            if unavailable is not None:
                return unavailable

        # The family alone decides the handler. A data-requirement flag must never
        # hijack dispatch -- that is what routed team markets into the player model.
        handler = getattr(self, spec.handler)
        if spec.handler == "_evaluate_player_market":
            advisory = handler(descriptor, fixture=fixture, statpal_payload=statpal_payload)
        elif spec.handler in {"_evaluate_cards_market", "_evaluate_corners_market", "_evaluate_total_goal_market", "_evaluate_team_goal_market"}:
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

    @staticmethod
    def _player_team_hint(descriptor, fixture) -> str:
        """SportyBet writes the club in brackets: `Haller, Sebastian (Sanfrecce Hiroshima)`."""
        subject = str(descriptor.subject or descriptor.raw or "")
        if "(" in subject and ")" in subject:
            return subject.split("(", 1)[1].rsplit(")", 1)[0].strip()
        return ""

    def _player_availability_block(self, descriptor: MarketDescriptor, fixture):
        """
        Refuse to price a player who will not be on the pitch.

        An injured or suspended player makes the bet dead, not risky, so this returns an
        unavailable assessment rather than a low score. A doubtful player is priced but
        flagged, because the bet is still live.
        """
        from .scoring.availability import player_availability_service

        subject = str(descriptor.subject or descriptor.raw or "")
        if not subject:
            return None

        from .scoring.lineups import lineup_service

        team_hint = self._player_team_hint(descriptor, fixture)
        match_id = str((fixture or {}).get("statpal_provider_match_id") or "")

        verdict = player_availability_service.verdict_for(
            player_name=subject, team_name=team_hint, match_id=match_id
        )

        if not verdict.is_out:
            # A confirmed team sheet that omits the player is equally decisive; a
            # projected one is only a signal and must not block pricing.
            sheet = lineup_service.verdict_for(
                match_id=match_id, player_name=subject, team_name=team_hint
            )
            if sheet.blocks_pricing:
                return {
                    **StatPalAdvisory(
                        available=False,
                        score=None,
                        status="unavailable",
                        basis="player_not_in_confirmed_lineup",
                        evidence={"player": subject, "lineup": sheet.to_dict()},
                        warnings=["player_omitted_from_confirmed_lineup"],
                        message=f"{subject} is not in the confirmed team sheet for this fixture.",
                    ).to_dict(),
                    "assessment_type": NONE,
                    "market_family": descriptor.family,
                }
            return None

        return {
            **StatPalAdvisory(
                available=False,
                score=None,
                status="unavailable",
                basis="player_unavailable",
                evidence={
                    "player": verdict.player_name or subject,
                    "availability": verdict.status,
                    "reason": verdict.reason,
                },
                warnings=["player_out_injured_or_suspended"],
                message=(
                    f"{verdict.player_name or subject} is currently listed as unavailable"
                    + (f" ({verdict.reason})." if verdict.reason else ".")
                ),
            ).to_dict(),
            "assessment_type": NONE,
            "market_family": descriptor.family,
        }

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
        score, snapshot_evidence, snapshot_warnings = self._apply_snapshot_context(
            score,
            descriptor=descriptor,
            fixture=fixture,
            provider_payload=provider_payload,
        )
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
        score, snapshot_evidence, snapshot_warnings = self._apply_snapshot_context(
            score,
            descriptor=descriptor,
            fixture=fixture,
            provider_payload=provider_payload,
        )
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

    def _evaluate_total_goal_market(self, descriptor: MarketDescriptor, *, fixture=None, provider_payload=None) -> StatPalAdvisory:
        line = _num(descriptor.line, 2.5)
        expected_total, evidence, warnings = self._expected_total_goals(fixture)
        probability = self._goal_line_probability(expected_total, line, descriptor.selection or descriptor.side)
        score = self._probability_score(probability, line=line, market_side=descriptor.selection or descriptor.side)
        evidence.update({
            "line": line,
            "selection": descriptor.selection or descriptor.side,
            "market_family": descriptor.family,
            "recognized": True,
        })
        if expected_total <= 0:
            warnings.append("goal_profile_missing")
            return StatPalAdvisory(
                available=False,
                score=None,
                status="needs_data",
                basis="goal_profile_missing",
                evidence={**evidence, "estimated_probability": None},
                warnings=list(dict.fromkeys(warnings)),
                message="Expected-goals inputs are unavailable for this fixture.",
            )
        evidence["estimated_probability"] = probability
        if abs(expected_total - line) < 0.35:
            warnings.append("thin_goal_edge")
            score -= 5
        score, snapshot_evidence, snapshot_warnings = self._apply_snapshot_context(
            score,
            descriptor=descriptor,
            fixture=fixture,
            provider_payload=provider_payload,
        )
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

    def _evaluate_team_goal_market(self, descriptor: MarketDescriptor, *, fixture=None, provider_payload=None) -> StatPalAdvisory:
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
        }
        if expected_team <= 0:
            warnings = ["team_goal_profile_missing"]
            return StatPalAdvisory(
                available=False,
                score=None,
                status="needs_data",
                basis="team_goal_profile_missing",
                evidence={**evidence, "estimated_probability": None},
                warnings=list(dict.fromkeys(warnings)),
                message="Team expected-goals inputs are unavailable for this fixture.",
            )
        evidence["estimated_probability"] = probability
        if abs(expected_team - line) < 0.25:
            warnings.append("thin_team_goal_edge")
            score -= 4
        score, snapshot_evidence, snapshot_warnings = self._apply_snapshot_context(
            score,
            descriptor=descriptor,
            fixture=fixture,
            provider_payload=provider_payload,
        )
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

    def _score_matrix_fallback(self, descriptor: MarketDescriptor, *, fixture=None, provider_payload=None) -> dict[str, Any] | None:
        if descriptor.family == "total_goals":
            advisory = self._evaluate_total_goal_market(descriptor, fixture=fixture, provider_payload=provider_payload)
        elif descriptor.family == "team_total_goals":
            advisory = self._evaluate_team_goal_market(descriptor, fixture=fixture, provider_payload=provider_payload)
        else:
            return None
        payload = advisory.to_dict()
        if payload.get("score") is None or payload.get("basis") in {"score_matrix_no_fit", "no_model_for_family"}:
            return None
        return payload

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
        team_stats = statpal.get("team_stats") or {}
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

        history = self._team_history_summary_for_descriptor(team_stats, MarketDescriptor(
            raw="total_goals",
            canonical="total_goals",
            code="total_goals",
            family="total_goals",
            category="Goals",
            selection="over",
            side="over",
            period="full_match",
        )) if team_stats else {}
        history_expected = _num(history.get("avg_total_goals"), 0)
        if history_expected:
            sources.append("statpal_team_history")
            expected = round((expected * 0.6 + history_expected * 0.4), 2) if expected else history_expected

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
            "statpal_team_history_goals": history_expected or None,
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
        team_stats = statpal.get("team_stats") or {}
        home_form = fixture.get("home_recent_form") or {}
        away_form = fixture.get("away_recent_form") or {}
        team_side = team_side if team_side in {"home", "away"} else ""
        history_descriptor = MarketDescriptor(
            raw="team_total_goals",
            canonical="team_total_goals",
            code="team_total_goals",
            family="team_total_goals",
            category="Goals",
            selection="over",
            side="over",
            team=team_side,
            period="full_match",
        )
        history = self._team_history_summary_for_descriptor(team_stats, history_descriptor) if team_stats else {}
        history_expected = _num(history.get("avg_goals_for"), 0)

        home_expected = (
            _num(detailed.get("home_xg"), 0)
            or _num(predictions.get("home_xg"), 0)
            or (history_expected if team_side == "home" else 0)
            or self._team_form_expectation(home_form, away_form)
        )
        away_expected = (
            _num(detailed.get("away_xg"), 0)
            or _num(predictions.get("away_xg"), 0)
            or (history_expected if team_side == "away" else 0)
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
            "statpal_team_history_goals_for": history_expected or None,
            "team_goal_model_source": "statpal_xg" if (_num(detailed.get("home_xg"), 0) or _num(detailed.get("away_xg"), 0)) else "statpal_team_history" if history_expected else "recent_form",
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

    def _apply_odds_overlay(
        self,
        payload: dict[str, Any],
        *,
        descriptor: MarketDescriptor,
        fixture: dict[str, Any] | None = None,
        provider_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshots = (((fixture or {}).get("statpal_context") or {}).get("snapshots") or {})
        odds_snapshot = snapshots.get("prematch_odds") or {}
        odds_summary = odds_snapshot.get("summary") or {}
        if not odds_summary or not payload.get("available"):
            return payload
        score = _num(payload.get("score"), None)
        if score is None:
            return payload

        adjustment, odds_evidence, odds_warnings = self._odds_adjustment(
            odds_summary,
            descriptor=descriptor,
            payload=odds_snapshot.get("payload") or {},
            provider_payload=provider_payload or {},
        )
        adjusted = round(max(0, min(100, score + adjustment)), 1)
        result = dict(payload)
        result["score"] = adjusted
        result["status"] = _status(adjusted)
        evidence = dict(result.get("evidence") or {})
        evidence["odds_adjustment"] = round(adjustment, 1)
        evidence["odds_snapshot"] = odds_summary
        if odds_evidence:
            evidence["odds_value"] = odds_evidence
        result["evidence"] = evidence
        warnings = list(result.get("warnings") or [])
        if adjustment < 0:
            warnings.append("odds_snapshot_caution")
        warnings.extend(odds_warnings)
        result["warnings"] = list(dict.fromkeys(warnings))
        return result

    def _apply_snapshot_context(
        self,
        score: float,
        *,
        descriptor: MarketDescriptor,
        fixture: dict[str, Any] | None = None,
        player_team: str = "",
        provider_payload: dict[str, Any] | None = None,
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

        odds_snapshot = snapshots.get("prematch_odds") or {}
        odds = odds_snapshot.get("summary") or {}
        if odds:
            adjustment, odds_evidence, odds_warnings = self._odds_adjustment(
                odds,
                descriptor=descriptor,
                payload=odds_snapshot.get("payload") or {},
                provider_payload=provider_payload or {},
            )
            score += adjustment
            evidence["odds_adjustment"] = round(adjustment, 1)
            evidence["odds_snapshot"] = odds
            if odds_evidence:
                evidence["odds_value"] = odds_evidence
            if adjustment < 0:
                warnings.append("odds_snapshot_caution")
            warnings.extend(odds_warnings)

        team_stats = ((snapshots.get("team_stats") or {}).get("summary") or {})
        if team_stats:
            adjustment, team_evidence, team_warnings = self._team_history_adjustment(team_stats, descriptor=descriptor)
            score += adjustment
            evidence["team_stats_snapshot"] = team_stats
            evidence["team_history_adjustment"] = round(adjustment, 1)
            if team_evidence:
                evidence["team_history"] = team_evidence
            if team_stats.get("sample_size") and _num(team_stats.get("sample_size")) < 5:
                score -= 4
                warnings.append("small_team_stat_sample")
            warnings.extend(team_warnings)

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
    def _team_history_adjustment(team_stats: dict[str, Any], *, descriptor: MarketDescriptor):
        if not isinstance(team_stats, dict):
            return 0.0, {}, []
        team_stats = StatPalMarketAdvisoryService._team_history_summary_for_descriptor(team_stats, descriptor)

        warnings = []
        sample_size = _num(team_stats.get("sample_size"))
        selection = (descriptor.selection or descriptor.side or "").lower()
        line = _num(descriptor.line, 0.0)
        period_prefix = "firsthalf_" if descriptor.period == "first_half" else "secondhalf_" if descriptor.period == "second_half" else ""
        evidence = {
            "team_id": team_stats.get("team_id") or "",
            "team_name": team_stats.get("team_name") or "",
            "sample_size": int(sample_size) if sample_size else 0,
            "current_league": team_stats.get("current_league") or "",
            "current_season": team_stats.get("current_season") or "",
        }
        if 0 < sample_size < 5:
            warnings.append("small_team_stat_sample")

        def directional(metric, threshold_line, *, cushion=0.25, scale=1.2, cap=3.0):
            metric = _num(metric, None)
            if metric is None or not selection or threshold_line <= 0:
                return 0.0
            edge = metric - threshold_line
            if abs(edge) < cushion:
                return 0.0
            magnitude = min(cap, (abs(edge) - cushion) * scale)
            if selection in {"over", "yes"}:
                return magnitude if edge > 0 else -magnitude
            if selection in {"under", "no"}:
                return magnitude if edge < 0 else -magnitude
            return 0.0

        family = descriptor.family
        adjustment = 0.0
        if family == "total_goals":
            metric = team_stats.get(f"{period_prefix}avg_total_goals") if period_prefix else team_stats.get("avg_total_goals")
            if metric is None and period_prefix:
                scored = _num(team_stats.get(f"{period_prefix}avg_goals_for"), None)
                conceded = _num(team_stats.get(f"{period_prefix}avg_goals_against"), None)
                if scored is not None or conceded is not None:
                    metric = (scored or 0) + (conceded or 0)
            evidence["metric"] = "avg_total_goals"
            evidence["metric_value"] = round(_num(metric), 2) if metric is not None else None
            evidence["line"] = line
            adjustment = directional(metric, line, cushion=0.35, scale=1.15, cap=3.0)
        elif family == "team_total_goals":
            metric = team_stats.get(f"{period_prefix}avg_goals_for") if period_prefix else team_stats.get("avg_goals_for")
            evidence["metric"] = "avg_goals_for"
            evidence["metric_value"] = round(_num(metric), 2) if metric is not None else None
            evidence["line"] = line
            adjustment = directional(metric, line, cushion=0.2, scale=1.5, cap=3.5)
        elif family == "team_corners":
            metric = team_stats.get("avg_corners")
            evidence["metric"] = "avg_corners"
            evidence["metric_value"] = round(_num(metric), 2) if metric is not None else None
            evidence["line"] = line
            adjustment = directional(metric, line, cushion=0.4, scale=0.9, cap=2.0)
        elif family == "corners_total":
            metric = _num(team_stats.get("avg_corners"), None)
            evidence["metric"] = "avg_corners"
            evidence["metric_value"] = round(metric, 2) if metric is not None else None
            if metric is not None:
                if selection == "over" and metric >= 6:
                    adjustment = 1.2
                elif selection == "under" and metric <= 3.5:
                    adjustment = 1.2
        elif family == "team_cards":
            metric = team_stats.get("avg_yellowcards")
            evidence["metric"] = "avg_yellowcards"
            evidence["metric_value"] = round(_num(metric), 2) if metric is not None else None
            evidence["line"] = line
            adjustment = directional(metric, line, cushion=0.25, scale=0.9, cap=2.0)
        elif family in {"cards_total", "booking_points"}:
            metric = _num(team_stats.get("avg_yellowcards"), None)
            evidence["metric"] = "avg_yellowcards"
            evidence["metric_value"] = round(metric, 2) if metric is not None else None
            if metric is not None:
                if selection == "over" and metric >= 2.7:
                    adjustment = 1.0
                elif selection == "under" and metric <= 1.4:
                    adjustment = 1.0
        elif family in {"btts", "clean_sheet", "team_clean_sheet"}:
            avg_for = _num(team_stats.get("avg_goals_for"), None)
            avg_against = _num(team_stats.get("avg_goals_against"), None)
            evidence["metric"] = "avg_goals_for_against"
            evidence["avg_goals_for"] = avg_for
            evidence["avg_goals_against"] = avg_against
            if avg_for is not None and avg_against is not None:
                if family == "btts":
                    if selection == "yes" and avg_for >= 1 and avg_against >= 1:
                        adjustment = 1.5
                    elif selection == "no" and (avg_for < 0.9 or avg_against < 0.9):
                        adjustment = 1.5
                else:
                    if selection == "yes" and avg_against <= 0.9:
                        adjustment = 1.5
                    elif selection == "no" and avg_against >= 1.2:
                        adjustment = 1.5

        if adjustment:
            warnings.append("team_history_context_applied")
        return adjustment, evidence, warnings

    @staticmethod
    def _team_history_summary_for_descriptor(team_stats: dict[str, Any], descriptor: MarketDescriptor) -> dict[str, Any]:
        teams = team_stats.get("teams") if isinstance(team_stats.get("teams"), list) else []
        if not teams:
            return team_stats

        target_side = descriptor.team if descriptor.team in {"home", "away"} else ""
        if target_side:
            side_summary = team_stats.get(target_side) or next(
                (team for team in teams if isinstance(team, dict) and team.get("fixture_side") == target_side),
                {},
            )
            if side_summary:
                return side_summary

        usable = [team for team in teams if isinstance(team, dict)]
        if not usable:
            return team_stats

        def values(key):
            return [_num(team.get(key), None) for team in usable if _num(team.get(key), None) is not None]

        def avg(key):
            nums = values(key)
            return round(sum(nums) / len(nums), 2) if nums else None

        def total(key):
            nums = values(key)
            return round(sum(nums), 2) if nums else None

        sample_sizes = values("sample_size")
        return {
            "team_id": "fixture",
            "team_name": " + ".join(team.get("team_name") or "" for team in usable if team.get("team_name")),
            "fixture_side": "fixture",
            "sample_size": int(min(sample_sizes)) if sample_sizes else None,
            "current_league": next((team.get("current_league") for team in usable if team.get("current_league")), ""),
            "current_season": next((team.get("current_season") for team in usable if team.get("current_season")), ""),
            "avg_goals_for": avg("avg_goals_for"),
            "avg_goals_against": avg("avg_goals_against"),
            "avg_total_goals": avg("avg_total_goals"),
            "avg_corners": total("avg_corners") if descriptor.family == "corners_total" else avg("avg_corners"),
            "avg_yellowcards": total("avg_yellowcards") if descriptor.family in {"cards_total", "booking_points"} else avg("avg_yellowcards"),
            "firsthalf_avg_goals_for": avg("firsthalf_avg_goals_for"),
            "firsthalf_avg_goals_against": avg("firsthalf_avg_goals_against"),
            "secondhalf_avg_goals_for": avg("secondhalf_avg_goals_for"),
            "secondhalf_avg_goals_against": avg("secondhalf_avg_goals_against"),
        }

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

    @classmethod
    def _odds_adjustment(cls, summary, *, descriptor: MarketDescriptor | None = None, payload=None, provider_payload=None):
        offered = cls._offered_odds(provider_payload or {})
        reference = cls._statpal_reference_odds(descriptor, payload or {}) if descriptor and isinstance(payload, dict) else {}
        reference_odds = _num(reference.get("odds"), None)
        evidence = {}
        warnings = []
        if offered and reference_odds:
            edge = (offered / reference_odds) - 1
            reliability = reference.get("reliability") or "unknown"
            adjustment_multiplier = cls._odds_reference_adjustment_multiplier(reliability)
            adjustment = max(-6, min(6, edge * 35 * adjustment_multiplier))
            evidence = {
                "offered_odds": round(offered, 3),
                "statpal_reference_odds": round(reference_odds, 3),
                "statpal_reference_min_odds": reference.get("min_odds"),
                "statpal_reference_max_odds": reference.get("max_odds"),
                "statpal_reference_bookmaker_count": reference.get("bookmaker_count"),
                "statpal_reference_spread_pct": reference.get("spread_pct"),
                "value_edge_pct": round(edge * 100, 1),
                "matched_market": reference.get("market") or "",
                "matched_outcome": reference.get("outcome") or "",
                "bookmaker": reference.get("bookmaker") or "",
                "reference_method": reference.get("reference_method") or "",
                "reference_reliability": reliability,
            }
            if edge <= -0.05:
                warnings.append("odds_below_statpal_reference")
            elif edge >= 0.05:
                warnings.append("positive_price_edge")
            if reliability == "thin":
                warnings.append("single_bookmaker_reference")
            elif reliability == "wide":
                warnings.append("wide_odds_reference_spread")
            elif reliability == "volatile":
                warnings.append("volatile_odds_reference_spread")
            return adjustment, evidence, warnings

        edge = _num(summary.get("edge") or summary.get("value_edge"), 0)
        spread = _num(summary.get("spread_pct"), 0)
        adjustment = max(-6, min(6, edge * 30))
        if spread and spread > 20:
            adjustment -= 3
            warnings.append("wide_odds_spread")
        return adjustment, evidence, warnings

    @classmethod
    def _offered_odds(cls, value):
        if isinstance(value, dict):
            for key in ("odds", "odd", "price", "decimal_odds", "decimalOdds"):
                parsed = _num(value.get(key), None)
                if parsed and parsed > 1:
                    return parsed
            for child in value.values():
                parsed = cls._offered_odds(child)
                if parsed:
                    return parsed
        elif isinstance(value, list):
            for item in value:
                parsed = cls._offered_odds(item)
                if parsed:
                    return parsed
        return None

    @classmethod
    def _statpal_reference_odds(cls, descriptor: MarketDescriptor | None, payload: dict[str, Any]) -> dict[str, Any]:
        if not descriptor:
            return {}
        for market in payload.get("markets") or []:
            if not cls._market_matches_descriptor(market, descriptor):
                continue
            result = cls._reference_from_market(market, descriptor)
            if result:
                return result
        return {}

    @classmethod
    def _reference_from_market(cls, market: dict[str, Any], descriptor: MarketDescriptor) -> dict[str, Any]:
        line = _num(descriptor.line, None)
        outcome = cls._descriptor_outcome_name(descriptor)
        candidates = []
        for bookmaker in market.get("bookmakers") or []:
            result = cls._odd_from_items(bookmaker.get("odds") or [], outcome)
            if result:
                candidates.append(cls._reference_candidate(market, bookmaker, result, outcome))
            for bucket_name in ("totals", "handicaps"):
                for bucket in bookmaker.get(bucket_name) or []:
                    bucket_line = _num(bucket.get("line"), None)
                    if line is not None and bucket_line is not None and abs(bucket_line - line) > 0.01:
                        continue
                    result = cls._odd_from_items(bucket.get("odds") or [], outcome)
                    if result:
                        candidates.append(cls._reference_candidate(market, bookmaker, result, outcome))
        return cls._aggregate_reference_candidates(candidates)

    @staticmethod
    def _reference_candidate(market: dict[str, Any], bookmaker: dict[str, Any], odd: dict[str, Any], outcome: str) -> dict[str, Any]:
        odds = _num((odd or {}).get("value"), None)
        if not odds or odds <= 1:
            return {}
        return {
            "odds": odds,
            "market": (market or {}).get("name") or "",
            "outcome": (odd or {}).get("name") or outcome,
            "bookmaker": (bookmaker or {}).get("name") or "",
        }

    @staticmethod
    def _aggregate_reference_candidates(candidates: list[dict[str, Any]]) -> dict[str, Any]:
        rows = [row for row in candidates if row and _num(row.get("odds"), None)]
        if not rows:
            return {}
        odds = sorted(_num(row.get("odds")) for row in rows)
        reference = float(statistics.median(odds))
        min_odds = float(odds[0])
        max_odds = float(odds[-1])
        spread_pct = round(((max_odds - min_odds) / reference) * 100, 1) if reference else 0
        reliability = StatPalMarketAdvisoryService._odds_reference_reliability(len(rows), spread_pct)
        first = rows[0]
        return {
            "odds": round(reference, 3),
            "min_odds": round(min_odds, 3),
            "max_odds": round(max_odds, 3),
            "spread_pct": spread_pct,
            "bookmaker_count": len(rows),
            "market": first.get("market") or "",
            "outcome": first.get("outcome") or "",
            "bookmaker": first.get("bookmaker") if len(rows) == 1 else f"median_of_{len(rows)}",
            "reference_method": "single_bookmaker" if len(rows) == 1 else "median_bookmaker_odds",
            "reliability": reliability,
        }

    @staticmethod
    def _odds_reference_reliability(bookmaker_count: int, spread_pct: float) -> str:
        if bookmaker_count <= 1:
            return "thin"
        if spread_pct >= 35:
            return "volatile"
        if spread_pct >= 20:
            return "wide"
        return "solid"

    @staticmethod
    def _odds_reference_adjustment_multiplier(reliability: str) -> float:
        return {
            "solid": 1.0,
            "wide": 0.55,
            "volatile": 0.25,
            "thin": 0.65,
        }.get(str(reliability or ""), 0.5)

    @staticmethod
    def _odd_from_items(items, outcome: str):
        wanted = normalize_market_text(outcome)
        for item in items or []:
            name = normalize_market_text((item or {}).get("name") or "")
            if name == wanted:
                return item
            if wanted in {"over", "under"} and name.startswith(wanted):
                return item
            if wanted in {"yes", "no"} and name == wanted:
                return item
            if wanted in {"home", "draw", "away"} and name == wanted:
                return item
        return None

    @staticmethod
    def _descriptor_outcome_name(descriptor: MarketDescriptor) -> str:
        if descriptor.family in {"match_result", "double_chance", "draw_no_bet", "asian_handicap", "handicap"}:
            return {"home": "Home", "draw": "Draw", "away": "Away"}.get(descriptor.side, descriptor.selection or descriptor.side or "")
        if descriptor.family in {"total_goals", "team_total_goals", "corners_total", "team_corners", "cards_total", "team_cards", "booking_points"}:
            return (descriptor.selection or descriptor.side or "over").title()
        if descriptor.family in {"btts", "clean_sheet", "team_clean_sheet"}:
            return (descriptor.selection or descriptor.side or "yes").title()
        return descriptor.selection or descriptor.side or ""

    @classmethod
    def _market_matches_descriptor(cls, market: dict[str, Any], descriptor: MarketDescriptor) -> bool:
        name = normalize_market_text((market or {}).get("name") or "")
        if not cls._period_matches(name, descriptor.period):
            return False
        if descriptor.family == "match_result":
            return name in {"1x2", "1 x 2", "match result"} or "1x2" in name
        if descriptor.family in {"total_goals", "team_total_goals"}:
            if "corner" in name or "card" in name:
                return False
            if descriptor.family == "team_total_goals":
                if not cls._team_matches(name, descriptor.team):
                    return False
            return "over under" in name or "total" in name or "goals" in name
        if descriptor.family == "btts":
            return "both teams" in name or "btts" in name or "gg" in name or "ng" in name
        if descriptor.family in {"clean_sheet", "team_clean_sheet"}:
            return "clean sheet" in name and cls._team_matches(name, descriptor.team or descriptor.side)
        if descriptor.family in {"corners_total", "team_corners"}:
            if "corner" not in name:
                return False
            if descriptor.family == "team_corners" and not cls._team_matches(name, descriptor.team):
                return False
            return True
        if descriptor.family in {"cards_total", "team_cards", "booking_points"}:
            if not ("card" in name or "booking" in name):
                return False
            if descriptor.family == "team_cards" and not cls._team_matches(name, descriptor.team):
                return False
            return True
        return False

    @staticmethod
    def _period_matches(market_name: str, period: str) -> bool:
        period = str(period or "match")
        first_half = any(token in market_name for token in ("1st half", "first half", "1h", "half time", "halftime"))
        second_half = any(token in market_name for token in ("2nd half", "second half", "2h"))
        if period in {"first_half", "1st_half"}:
            return first_half
        if period in {"second_half", "2nd_half"}:
            return second_half
        if period in {"match", "full_match"}:
            return not first_half and not second_half
        return True

    @staticmethod
    def _team_matches(market_name: str, team: str) -> bool:
        team = normalize_market_text(team)
        if not team:
            return True
        if team == "home":
            return "home" in market_name or "home team" in market_name
        if team == "away":
            return "away" in market_name or "away team" in market_name
        return team in market_name

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
