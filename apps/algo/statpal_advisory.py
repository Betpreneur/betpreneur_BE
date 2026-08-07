from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

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
        if descriptor.requires_player_stats:
            return self._evaluate_player_market(descriptor, fixture=fixture, statpal_payload=statpal_payload).to_dict()
        if descriptor.requires_card_stats:
            return self._evaluate_cards_market(descriptor, fixture=fixture, provider_payload=provider_payload).to_dict()
        if descriptor.requires_corner_stats:
            return self._evaluate_corners_market(descriptor, fixture=fixture, provider_payload=provider_payload).to_dict()
        if descriptor.requires_team_goal_stats or descriptor.family == "team_total_goals":
            return self._evaluate_team_goal_market(descriptor, fixture=fixture).to_dict()
        if descriptor.family in {
            "total_goals",
            "btts",
            "match_result",
            "draw_no_bet",
            "double_chance",
            "clean_sheet",
            "first_to_score",
        }:
            return self._evaluate_fixture_context_market(descriptor, fixture=fixture).to_dict()
        return StatPalAdvisory(
            available=False,
            score=None,
            status="unsupported",
            basis="not_statpal_market",
            evidence={},
            warnings=["not_statpal_market"],
            message="This market does not need the StatPal advisory path.",
        ).to_dict()

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

        score = self._player_score(descriptor, evidence)
        warnings = []
        if appearances < 5:
            warnings.append("small_player_sample")
            score -= 8
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
            warnings=warnings,
            message=self._player_message(descriptor, evidence, score),
        )

    def _evaluate_cards_market(self, descriptor: MarketDescriptor, *, fixture=None, provider_payload=None) -> StatPalAdvisory:
        line = _num(descriptor.line, 3.5)
        score = 50.0
        warnings = ["cards_model_requires_referee_and_league_card_rates"]
        evidence = {
            "line": line,
            "market_family": descriptor.family,
            "recognized": True,
            "data_needed": ["league_card_rates", "team_card_rates", "referee_profile", "player_suspension_risk"],
        }
        if line <= 2.5:
            score += 6
        elif line >= 5.5:
            score -= 4
        score, snapshot_evidence, snapshot_warnings = self._apply_snapshot_context(score, descriptor=descriptor, fixture=fixture)
        evidence.update(snapshot_evidence)
        warnings.extend(snapshot_warnings)
        return StatPalAdvisory(
            available=True,
            score=round(max(0, min(100, score)), 1),
            status=_status(score),
            basis="statpal_cards_advisory_stub",
            evidence=evidence,
            warnings=warnings,
            message="Cards market recognized. Full confidence needs StatPal league/team/referee card profiles.",
        )

    def _evaluate_corners_market(self, descriptor: MarketDescriptor, *, fixture=None, provider_payload=None) -> StatPalAdvisory:
        line = _num(descriptor.line, 9.5)
        corner_profile = ((fixture or {}).get("corner_profile") or {}) if fixture else {}
        expected_total = _num(corner_profile.get("expected_total"), 0)
        evidence = {
            "line": line,
            "expected_total_corners": expected_total or None,
            "market_family": descriptor.family,
            "recognized": True,
        }
        if expected_total:
            edge = expected_total - line
            if descriptor.side == "under":
                edge = line - expected_total
            score = 58 + edge * 7
            warnings = [] if abs(edge) >= 1.0 else ["thin_corner_edge"]
            basis = "fixture_corner_profile"
        else:
            score = 52
            warnings = ["corner_profile_missing"]
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
            warnings=warnings,
            message="Corners market recognized. Confidence improves when fixture corner profile is available.",
        )

    def _evaluate_team_goal_market(self, descriptor: MarketDescriptor, *, fixture=None) -> StatPalAdvisory:
        evidence = {
            "line": _num(descriptor.line, 0.5),
            "side": descriptor.side,
            "market_family": descriptor.family,
            "recognized": True,
        }
        home_form = (fixture or {}).get("home_recent_form") or {}
        away_form = (fixture or {}).get("away_recent_form") or {}
        if home_form or away_form:
            if descriptor.side == "home":
                scored = _num(home_form.get("avg_scored") or home_form.get("goals_for_avg"))
            elif descriptor.side == "away":
                scored = _num(away_form.get("avg_scored") or away_form.get("goals_for_avg"))
            else:
                scored = max(
                    _num(home_form.get("avg_scored") or home_form.get("goals_for_avg")),
                    _num(away_form.get("avg_scored") or away_form.get("goals_for_avg")),
                )
            evidence["team_scored_average"] = scored or None
            score = 50 + (scored - evidence["line"]) * 18
            warnings = [] if scored else ["team_goal_profile_missing"]
        else:
            score = 52
            warnings = ["team_goal_profile_missing"]
        score, snapshot_evidence, snapshot_warnings = self._apply_snapshot_context(score, descriptor=descriptor, fixture=fixture)
        evidence.update(snapshot_evidence)
        warnings.extend(snapshot_warnings)
        score = round(max(0, min(100, score)), 1)
        return StatPalAdvisory(
            available=True,
            score=score,
            status=_status(score),
            basis="fixture_team_goal_profile",
            evidence=evidence,
            warnings=warnings,
            message="Team-goal market recognized. Score is based on available team scoring profile.",
        )

    def _evaluate_fixture_context_market(self, descriptor: MarketDescriptor, *, fixture=None) -> StatPalAdvisory:
        score = 58.0
        evidence = {
            "market_family": descriptor.family,
            "recognized": True,
        }
        warnings = []
        score, snapshot_evidence, snapshot_warnings = self._apply_snapshot_context(score, descriptor=descriptor, fixture=fixture)
        evidence.update(snapshot_evidence)
        warnings.extend(snapshot_warnings)
        score = round(max(0, min(100, score)), 1)
        return StatPalAdvisory(
            available=True,
            score=score,
            status=_status(score),
            basis="statpal_fixture_context",
            evidence=evidence,
            warnings=warnings,
            message="Fixture-level StatPal snapshots were used as supporting context for this market.",
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

    @staticmethod
    def _player_score(descriptor: MarketDescriptor, evidence: dict[str, Any]) -> float:
        if descriptor.family == "player_goal":
            return 48 + evidence["goals_per_appearance"] * 90 + evidence["shots_on_target_per_appearance"] * 12
        if descriptor.family == "player_assist":
            return 48 + evidence["assists_per_appearance"] * 110
        if descriptor.family == "player_shots_on_target":
            line = _num(descriptor.line, 0.5)
            return 50 + (evidence["shots_on_target_per_appearance"] - line) * 22
        if descriptor.family == "player_shots":
            line = _num(descriptor.line, 1.5)
            return 50 + (evidence["shots_per_appearance"] - line) * 18
        if descriptor.family == "player_card":
            return 48 + evidence["cards_per_appearance"] * 120
        if descriptor.family == "player_saves":
            line = _num(descriptor.line, 2.5)
            return 50 + (evidence["saves_per_appearance"] - line) * 12
        return 50

    @staticmethod
    def _player_message(descriptor: MarketDescriptor, evidence: dict[str, Any], score: float) -> str:
        player = evidence.get("player_name") or "This player"
        if descriptor.family == "player_goal":
            return f"{player} averages {evidence['goals_per_appearance']} goals and {evidence['shots_on_target_per_appearance']} shots on target per appearance in the sample."
        if descriptor.family == "player_assist":
            return f"{player} averages {evidence['assists_per_appearance']} assists per appearance in the sample."
        if descriptor.family in {"player_shots", "player_shots_on_target"}:
            return f"{player} averages {evidence['shots_per_appearance']} shots and {evidence['shots_on_target_per_appearance']} shots on target per appearance."
        if descriptor.family == "player_card":
            return f"{player} averages {evidence['cards_per_appearance']} cards per appearance in the sample."
        return f"{player} has a StatPal advisory score of {score} for this market."


statpal_market_advisory = StatPalMarketAdvisoryService()
