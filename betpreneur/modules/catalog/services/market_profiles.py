from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db.models import Q
from django.utils import timezone

from betpreneur.modules.catalog.domain.league_registry import (
    IntelligenceLeague,
    team_intelligence_leagues,
)
from betpreneur.modules.catalog.models import (
    DataCoverage,
    FixtureCache,
    LeagueMarketProfile,
    TeamMarketProfile,
    TeamProfile,
)
from betpreneur.platform.db.json import json_safe

TOTAL_GOAL_LINES = (1.5, 2.5, 3.5, 4.5)
TEAM_GOAL_LINES = (0.5, 1.5, 2.5)
TOTAL_CORNER_LINES = (7.5, 8.5, 9.5, 10.5, 11.5)
TEAM_CORNER_LINES = (2.5, 3.5, 4.5, 5.5)
TOTAL_CARD_LINES = (3.5, 4.5, 5.5)
TEAM_CARD_LINES = (1.5, 2.5, 3.5)
TOTAL_SOT_LINES = (6.5, 7.5, 8.5, 9.5, 10.5)
TEAM_SOT_LINES = (2.5, 3.5, 4.5, 5.5)


@dataclass(frozen=True)
class MarketProfileScope:
    league: IntelligenceLeague
    season: str


@dataclass(frozen=True)
class MarketOutcome:
    market: str
    market_family: str
    side: str
    line: float | None
    result: bool | None


class MarketProfileBuilder:
    """Build team and league market behaviour profiles from completed fixtures."""

    COVERAGE_KEY = "market_profiles"
    FINAL_STATUSES = {"finished", "ft", "after extra time", "after penalties", "ended", "complete", "completed"}

    def build(
        self,
        *,
        league_keys: list[str] | tuple[str, ...] | None = None,
        seasons: list[str] | tuple[str, ...] | None = None,
        min_attempts: int = 1,
    ) -> dict[str, Any]:
        scopes = self._scopes(league_keys=league_keys, seasons=seasons)
        results = [self.build_scope(scope, min_attempts=min_attempts) for scope in scopes]
        return {
            "provider": "statpal",
            "scopes": len(results),
            "leagues": len({item["league_key"] for item in results}),
            "seasons": sorted({item["season"] for item in results}),
            "fixtures_considered": sum(item.get("fixtures_considered", 0) for item in results),
            "team_profiles_saved": sum(item.get("team_profiles_saved", 0) for item in results),
            "league_profiles_saved": sum(item.get("league_profiles_saved", 0) for item in results),
            "skipped": sum(1 for item in results if item.get("status") == "skipped"),
            "results": results,
        }

    def build_scope(self, scope: MarketProfileScope, *, min_attempts: int = 1) -> dict[str, Any]:
        provider_league_id = self._provider_league_id(scope.league)
        if not provider_league_id:
            return self._skipped(scope, "missing_provider_league_id")

        fixtures = list(self._completed_fixtures(scope))
        league_profiles = self._save_league_profiles(scope, fixtures, min_attempts=min_attempts)
        team_profiles = 0
        for team in self._teams_for_scope(scope):
            team_profiles += self._save_team_profiles(scope, team, fixtures, min_attempts=min_attempts)

        return {
            "status": "complete",
            "league_key": scope.league.key,
            "league_name": scope.league.name,
            "season": scope.season,
            "provider_league_id": provider_league_id,
            "fixtures_considered": len(fixtures),
            "team_profiles_saved": team_profiles,
            "league_profiles_saved": league_profiles,
        }

    def _save_league_profiles(
        self,
        scope: MarketProfileScope,
        fixtures: list[FixtureCache],
        *,
        min_attempts: int,
    ) -> int:
        buckets: dict[tuple[str, str], list[MarketOutcome]] = {}
        for fixture in fixtures:
            for outcome in self._league_outcomes(fixture):
                buckets.setdefault((outcome.market, outcome.market_family), []).append(outcome)

        saved = 0
        for (_market, _family), outcomes in buckets.items():
            if len(outcomes) < min_attempts:
                continue
            first = outcomes[0]
            wins = sum(1 for item in outcomes if item.result is True)
            losses = sum(1 for item in outcomes if item.result is False)
            voids = sum(1 for item in outcomes if item.result is None)
            attempts = wins + losses
            hit_rate = round((wins / attempts) * 100, 2) if attempts else None
            profile, _ = LeagueMarketProfile.objects.update_or_create(
                league_key=scope.league.key,
                season=scope.season,
                market=first.market,
                defaults={
                    "league_name": scope.league.name,
                    "country": scope.league.country,
                    "provider_ids": json_safe(
                        {
                            "statpal": scope.league.statpal_league_id,
                            "api_football": scope.league.api_football_league_id,
                        }
                    ),
                    "market_family": first.market_family,
                    "side": first.side,
                    "line": first.line,
                    "attempts": attempts,
                    "wins": wins,
                    "losses": losses,
                    "voids": voids,
                    "hit_rate": hit_rate,
                    "confidence": self._confidence(attempts, hit_rate),
                    "fairness_score": self._fairness_score(hit_rate),
                    "volatility": self._volatility(wins, losses),
                    "data_quality": self._data_quality(attempts),
                    "stats": json_safe({"sample_matches": len(outcomes)}),
                    "source": "statpal",
                    "computed_at": timezone.now(),
                },
            )
            self._save_league_coverage(profile)
            saved += 1
        return saved

    def _save_team_profiles(
        self,
        scope: MarketProfileScope,
        team: TeamProfile,
        fixtures: list[FixtureCache],
        *,
        min_attempts: int,
    ) -> int:
        buckets: dict[tuple[str, str, str], list[MarketOutcome]] = {}
        for fixture in fixtures:
            occurrence = self._team_occurrence(fixture, team)
            if not occurrence:
                continue
            for outcome in self._team_outcomes(occurrence):
                buckets.setdefault((outcome.market, outcome.market_family, occurrence["scope"]), []).append(outcome)

        saved = 0
        for (_market, _family, profile_scope), outcomes in buckets.items():
            if len(outcomes) < min_attempts:
                continue
            first = outcomes[0]
            wins = sum(1 for item in outcomes if item.result is True)
            losses = sum(1 for item in outcomes if item.result is False)
            voids = sum(1 for item in outcomes if item.result is None)
            attempts = wins + losses
            hit_rate = round((wins / attempts) * 100, 2) if attempts else None
            profile, _ = TeamMarketProfile.objects.update_or_create(
                team=team,
                league_key=scope.league.key,
                season=scope.season,
                market=first.market,
                scope=profile_scope,
                defaults={
                    "league_name": scope.league.name,
                    "market_family": first.market_family,
                    "side": first.side,
                    "line": first.line,
                    "attempts": attempts,
                    "wins": wins,
                    "losses": losses,
                    "voids": voids,
                    "hit_rate": hit_rate,
                    "confidence": self._confidence(attempts, hit_rate),
                    "data_quality": self._data_quality(attempts),
                    "stats": json_safe({"sample_matches": len(outcomes)}),
                    "source": "statpal",
                    "computed_at": timezone.now(),
                },
            )
            self._save_team_coverage(profile)
            saved += 1
        return saved

    def _league_outcomes(self, fixture: FixtureCache) -> list[MarketOutcome]:
        payload = fixture.api_payload or {}
        home_goals = self._score(payload.get("home_goals"), payload.get("ft_home_goals"))
        away_goals = self._score(payload.get("away_goals"), payload.get("ft_away_goals"))
        if home_goals is None or away_goals is None:
            return []
        total_goals = home_goals + away_goals
        total_corners = self._total_metric(payload, "corners")
        total_cards = self._total_metric(payload, "yellowcards")
        total_sot = self._total_metric(payload, "shots_on_goal")
        outcomes = [
            MarketOutcome("Home Win", "match_result", "home", None, home_goals > away_goals),
            MarketOutcome("Away Win", "match_result", "away", None, away_goals > home_goals),
            MarketOutcome("Draw", "match_result", "draw", None, home_goals == away_goals),
            MarketOutcome("DC: 1X", "double_chance", "home_or_draw", None, home_goals >= away_goals),
            MarketOutcome("DC: X2", "double_chance", "away_or_draw", None, away_goals >= home_goals),
            MarketOutcome("DC: 12", "double_chance", "home_or_away", None, home_goals != away_goals),
            MarketOutcome("DNB Home", "draw_no_bet", "home", None, None if home_goals == away_goals else home_goals > away_goals),
            MarketOutcome("DNB Away", "draw_no_bet", "away", None, None if home_goals == away_goals else away_goals > home_goals),
            MarketOutcome("AH Home +0.5", "asian_handicap", "home", 0.5, home_goals >= away_goals),
            MarketOutcome("AH Away +0.5", "asian_handicap", "away", 0.5, away_goals >= home_goals),
            MarketOutcome("GG / BTTS Yes", "btts", "yes", None, home_goals > 0 and away_goals > 0),
            MarketOutcome("GG + Over 2.5", "total_btts", "yes", 2.5, home_goals > 0 and away_goals > 0 and total_goals > 2.5),
        ]
        outcomes.extend(self._line_outcomes("Over", "Under", "total_goals", total_goals, TOTAL_GOAL_LINES))
        outcomes.extend(self._line_outcomes("Corners Over", "Corners Under", "corners_total", total_corners, TOTAL_CORNER_LINES))
        outcomes.extend(self._line_outcomes("Cards Over", "Cards Under", "cards_total", total_cards, TOTAL_CARD_LINES))
        outcomes.extend(self._line_outcomes("Shots On Target Over", "Shots On Target Under", "shots_on_target_total", total_sot, TOTAL_SOT_LINES))
        return outcomes

    def _team_outcomes(self, occurrence: dict[str, Any]) -> list[MarketOutcome]:
        side_label = "Home" if occurrence["scope"] == TeamMarketProfile.Scope.HOME else "Away"
        goals_for = occurrence["goals_for"]
        goals_against = occurrence["goals_against"]
        corners_for = occurrence.get("corners_for")
        cards_for = occurrence.get("cards_for")
        sot_for = occurrence.get("shots_on_target_for")
        outcomes = [
            MarketOutcome(f"{side_label} Win", "match_result", occurrence["scope"], None, goals_for > goals_against),
            MarketOutcome(f"DNB {side_label}", "draw_no_bet", occurrence["scope"], None, None if goals_for == goals_against else goals_for > goals_against),
            MarketOutcome(f"AH {side_label} +0.5", "asian_handicap", occurrence["scope"], 0.5, goals_for >= goals_against),
            MarketOutcome(f"{side_label} CS", "clean_sheet", occurrence["scope"], None, goals_against == 0),
        ]
        outcomes.extend(self._line_outcomes(f"{side_label} Team Goals Over", f"{side_label} Team Goals Under", "team_total_goals", goals_for, TEAM_GOAL_LINES))
        outcomes.extend(self._line_outcomes(f"{side_label} Team Corners Over", f"{side_label} Team Corners Under", "team_corners", corners_for, TEAM_CORNER_LINES))
        outcomes.extend(self._line_outcomes(f"{side_label} Team Cards Over", f"{side_label} Team Cards Under", "team_cards", cards_for, TEAM_CARD_LINES))
        outcomes.extend(self._line_outcomes(f"{side_label} Team Shots On Target Over", f"{side_label} Team Shots On Target Under", "team_shots_on_target", sot_for, TEAM_SOT_LINES))
        return outcomes

    @staticmethod
    def _line_outcomes(
        over_prefix: str,
        under_prefix: str,
        family: str,
        value: float | int | None,
        lines: tuple[float, ...],
    ) -> list[MarketOutcome]:
        if value is None:
            return []
        outcomes = []
        for line in lines:
            outcomes.append(MarketOutcome(f"{over_prefix} {line:g}", family, "over", line, value > line))
            outcomes.append(MarketOutcome(f"{under_prefix} {line:g}", family, "under", line, value < line))
        return outcomes

    def _team_occurrence(self, fixture: FixtureCache, team: TeamProfile) -> dict[str, Any] | None:
        payload = fixture.api_payload or {}
        team_ids = self._team_provider_ids(team)
        home_id = str(payload.get("provider_home_team_id") or payload.get("hid") or "")
        away_id = str(payload.get("provider_away_team_id") or payload.get("aid") or "")
        home_match = bool(home_id and home_id in team_ids) or fixture.home_team_normalized == team.canonical_normalized
        away_match = bool(away_id and away_id in team_ids) or fixture.away_team_normalized == team.canonical_normalized
        if not home_match and not away_match:
            return None
        home_goals = self._score(payload.get("home_goals"), payload.get("ft_home_goals"))
        away_goals = self._score(payload.get("away_goals"), payload.get("ft_away_goals"))
        if home_goals is None or away_goals is None:
            return None
        scope = TeamMarketProfile.Scope.HOME if home_match else TeamMarketProfile.Scope.AWAY
        opposite = TeamMarketProfile.Scope.AWAY if home_match else TeamMarketProfile.Scope.HOME
        return {
            "scope": scope,
            "goals_for": home_goals if home_match else away_goals,
            "goals_against": away_goals if home_match else home_goals,
            "corners_for": self._side_metric(payload, scope, "corners"),
            "corners_against": self._side_metric(payload, opposite, "corners"),
            "cards_for": self._side_metric(payload, scope, "yellowcards"),
            "cards_against": self._side_metric(payload, opposite, "yellowcards"),
            "shots_on_target_for": self._side_metric(payload, scope, "shots_on_goal"),
            "shots_on_target_against": self._side_metric(payload, opposite, "shots_on_goal"),
        }

    def _completed_fixtures(self, scope: MarketProfileScope):
        provider_ids = self._provider_league_ids(scope.league)
        payload_filter = Q()
        for provider_id in provider_ids:
            payload_filter |= Q(api_payload__provider_competition_id=str(provider_id))
            payload_filter |= Q(api_payload__code=str(provider_id))
            payload_filter |= Q(api_payload__league_id=str(provider_id))
        return (
            FixtureCache.objects.filter(source__in=["statpal", "api_football", "aps_provider_lookup"])
            .filter(payload_filter)
            .order_by("-match_date", "-kickoff_utc", "-updated_at")
        )

    @staticmethod
    def _provider_league_id(league: IntelligenceLeague) -> str:
        return str(league.statpal_league_id or league.api_football_league_id or "").strip()

    @staticmethod
    def _provider_league_ids(league: IntelligenceLeague) -> set[str]:
        return {str(value).strip() for value in (league.statpal_league_id, league.api_football_league_id) if str(value or "").strip()}

    @staticmethod
    def _team_provider_ids(team: TeamProfile) -> set[str]:
        values = set()
        for identity in (team.provider_ids or {}).values():
            if isinstance(identity, dict):
                values.update(str(value).strip() for key, value in identity.items() if key.endswith("team_id") or key == "team_id")
            else:
                values.add(str(identity).strip())
        return values - {""}

    @staticmethod
    def _teams_for_scope(scope: MarketProfileScope):
        return TeamProfile.objects.filter(
            active=True,
            primary_league_key=scope.league.key,
            season_profiles__league_key=scope.league.key,
            season_profiles__season=scope.season,
        ).distinct()

    @staticmethod
    def _total_metric(payload: dict[str, Any], metric: str) -> float | None:
        home = MarketProfileBuilder._side_metric(payload, "home", metric)
        away = MarketProfileBuilder._side_metric(payload, "away", metric)
        if home is None and away is None:
            return None
        return round((home or 0) + (away or 0), 3)

    @staticmethod
    def _side_metric(payload: dict[str, Any], side: str, metric: str) -> float | None:
        team_stats = payload.get("team_stats") if isinstance(payload.get("team_stats"), dict) else {}
        value = None
        side_stats = team_stats.get(side) if isinstance(team_stats.get(side), dict) else {}
        metric_stats = side_stats.get(metric) if isinstance(side_stats.get(metric), dict) else {}
        if metric_stats:
            value = metric_stats.get("total")
        if value in (None, ""):
            value = payload.get(f"{side}_{metric}") or payload.get(f"{metric}_{side}")
        try:
            if value in (None, ""):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _score(*values) -> int | None:
        for value in values:
            try:
                if value in (None, ""):
                    continue
                return int(float(value))
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _confidence(attempts: int, hit_rate: float | None) -> float | None:
        if hit_rate is None:
            return None
        sample_factor = min(1.0, attempts / 20)
        return round(50 + ((hit_rate - 50) * sample_factor), 2)

    @staticmethod
    def _fairness_score(hit_rate: float | None) -> float | None:
        if hit_rate is None:
            return None
        return round(max(0, 100 - abs(hit_rate - 50)), 2)

    @staticmethod
    def _volatility(wins: int, losses: int) -> float | None:
        attempts = wins + losses
        if not attempts:
            return None
        p = wins / attempts
        return round((p * (1 - p)) ** 0.5, 4)

    @staticmethod
    def _data_quality(attempts: int) -> str:
        if attempts >= 20:
            return "strong"
        if attempts >= 10:
            return "medium"
        if attempts >= 3:
            return "limited"
        if attempts > 0:
            return "poor"
        return "missing"

    @staticmethod
    def _save_team_coverage(profile: TeamMarketProfile) -> None:
        now = timezone.now()
        DataCoverage.objects.update_or_create(
            subject_type=DataCoverage.SubjectType.MARKET,
            subject_key=f"{profile.team.canonical_normalized}:{profile.league_key}:{profile.season}:{profile.market}:{profile.scope}",
            provider="statpal",
            coverage_key=MarketProfileBuilder.COVERAGE_KEY,
            defaults={
                "team": profile.team,
                "league_key": profile.league_key,
                "league_name": profile.league_name,
                "season": profile.season,
                "status": DataCoverage.Status.FRESH if profile.attempts >= 3 else DataCoverage.Status.PARTIAL,
                "available_requirements": ["completed_market_outcomes"] if profile.attempts else [],
                "missing_requirements": [] if profile.attempts else ["completed_market_outcomes"],
                "last_attempted_at": now,
                "last_success_at": now if profile.attempts else None,
                "metadata": {"team_market_profile_id": profile.pk},
            },
        )

    @staticmethod
    def _save_league_coverage(profile: LeagueMarketProfile) -> None:
        now = timezone.now()
        DataCoverage.objects.update_or_create(
            subject_type=DataCoverage.SubjectType.MARKET,
            subject_key=f"{profile.league_key}:{profile.season}:{profile.market}",
            provider="statpal",
            coverage_key=MarketProfileBuilder.COVERAGE_KEY,
            defaults={
                "league_key": profile.league_key,
                "league_name": profile.league_name,
                "season": profile.season,
                "status": DataCoverage.Status.FRESH if profile.attempts >= 3 else DataCoverage.Status.PARTIAL,
                "available_requirements": ["completed_market_outcomes"] if profile.attempts else [],
                "missing_requirements": [] if profile.attempts else ["completed_market_outcomes"],
                "last_attempted_at": now,
                "last_success_at": now if profile.attempts else None,
                "metadata": {"league_market_profile_id": profile.pk},
            },
        )

    def _scopes(
        self,
        *,
        league_keys: list[str] | tuple[str, ...] | None,
        seasons: list[str] | tuple[str, ...] | None,
    ) -> list[MarketProfileScope]:
        selected = set(league_keys or [])
        scopes = []
        for league in team_intelligence_leagues(active_only=True):
            if selected and league.key not in selected:
                continue
            league_seasons = seasons or (league.current_season, league.previous_season)
            for season in league_seasons:
                scopes.append(MarketProfileScope(league=league, season=str(season)))
        return scopes

    @staticmethod
    def _skipped(scope: MarketProfileScope, reason: str) -> dict[str, Any]:
        return {
            "status": "skipped",
            "reason": reason,
            "league_key": scope.league.key,
            "league_name": scope.league.name,
            "season": scope.season,
            "fixtures_considered": 0,
            "team_profiles_saved": 0,
            "league_profiles_saved": 0,
        }
