from django.test import TestCase
from django.utils import timezone

from betpreneur.modules.catalog.api import (
    DataCoverage,
    TeamMarketProfile,
    TeamProfile,
    TeamRecentFormProfile,
    TeamSeasonProfile,
    team_intelligence_service,
)
from betpreneur.modules.catalog.domain.league_registry import TOP_EUROPEAN_INTELLIGENCE_LEAGUES
from betpreneur.modules.catalog.services.coverage_tracker import DataCoverageTracker


class TeamIntelligenceServiceTests(TestCase):
    def setUp(self):
        self.league = TOP_EUROPEAN_INTELLIGENCE_LEAGUES[0]
        self.season = self.league.current_season
        self.home = self._team("Arsenal", "42")
        self.away = self._team("Chelsea", "50")
        self._profiles(self.home, market="Home Win")
        self._profiles(self.away, market="Away Win")

    def _team(self, name, team_id):
        return TeamProfile.objects.create(
            canonical_name=name,
            canonical_normalized=name.lower(),
            country=self.league.country,
            primary_league_key=self.league.key,
            primary_league_name=self.league.name,
            provider_ids={"statpal": {"team_id": team_id, "league_id": self.league.statpal_league_id}},
            aliases=[name],
        )

    def _profiles(self, team, *, market):
        TeamSeasonProfile.objects.create(
            team=team,
            league_key=self.league.key,
            league_name=self.league.name,
            country=self.league.country,
            season=self.season,
            matches_played=20,
            goals_for=45,
            goals_against=20,
            xg_for=40,
            xg_against=22,
            data_quality=TeamSeasonProfile.DataQuality.STRONG,
            computed_at=timezone.now(),
        )
        TeamRecentFormProfile.objects.create(
            team=team,
            league_key=self.league.key,
            league_name=self.league.name,
            season=self.season,
            window=5,
            scope=TeamRecentFormProfile.Scope.ALL,
            matches=5,
            wins=4,
            draws=1,
            losses=0,
            computed_at=timezone.now(),
        )
        TeamMarketProfile.objects.create(
            team=team,
            league_key=self.league.key,
            league_name=self.league.name,
            season=self.season,
            market_family="match_result",
            market=market,
            scope=TeamMarketProfile.Scope.ALL,
            attempts=18,
            wins=12,
            losses=6,
            hit_rate=66.7,
            confidence=72.0,
            data_quality="medium",
            computed_at=timezone.now(),
        )
        DataCoverage.objects.create(
            subject_type=DataCoverage.SubjectType.TEAM,
            subject_key=f"{team.canonical_normalized}:{self.league.key}:{self.season}",
            team=team,
            league_key=self.league.key,
            league_name=self.league.name,
            season=self.season,
            provider=DataCoverageTracker.PROVIDER,
            coverage_key=DataCoverageTracker.TEAM_COVERAGE_KEY,
            status=DataCoverage.Status.PARTIAL,
            available_requirements=["season_profile", "recent_form_all_5", "market_profile_match_result"],
            missing_requirements=["market_profile_total_goals"],
            metadata={"confidence": 66.2, "source_quality": "medium"},
            last_success_at=timezone.now(),
        )

    def test_loads_stored_intelligence_by_provider_team_ids(self):
        payload = team_intelligence_service.for_fixture(
            {
                "home_team": "Arsenal",
                "away_team": "Chelsea",
                "statpal_home_team_id": "42",
                "statpal_away_team_id": "50",
                "statpal_provider_competition_id": self.league.statpal_league_id,
                "season": self.season,
            }
        )

        self.assertTrue(payload["available"])
        self.assertEqual(payload["home"]["canonical_name"], "Arsenal")
        self.assertEqual(payload["away"]["canonical_name"], "Chelsea")
        self.assertEqual(payload["home"]["season_profile"]["data_quality"], "strong")
        self.assertEqual(payload["home"]["coverage"]["metadata"]["source_quality"], "medium")
        self.assertEqual(payload["home"]["market_profiles"][0]["market"], "Home Win")

    def test_falls_back_to_team_names_when_provider_ids_are_missing(self):
        payload = team_intelligence_service.for_fixture(
            {
                "home_team": "Arsenal",
                "away_team": "Chelsea",
                "league": self.league.name,
                "season": self.season,
            }
        )

        self.assertTrue(payload["available"])
        self.assertEqual(payload["league_key"], self.league.key)
        self.assertEqual(payload["away"]["canonical_name"], "Chelsea")

    def test_returns_missing_when_no_team_profile_matches(self):
        payload = team_intelligence_service.for_fixture(
            {
                "home_team": "Unknown FC",
                "away_team": "Missing United",
                "league": self.league.name,
                "season": self.season,
            }
        )

        self.assertFalse(payload["available"])
        self.assertEqual(payload["status"], "missing")
        self.assertIn("home_team_profile", payload["missing"])
