from django.db import IntegrityError
from django.test import TestCase

from betpreneur.modules.catalog.api import (
    DataCoverage,
    LeagueMarketProfile,
    TeamMarketProfile,
    TeamProfile,
    TeamRecentFormProfile,
    TeamSeasonProfile,
)


class TeamIntelligenceModelTests(TestCase):
    def setUp(self):
        self.team = TeamProfile.objects.create(
            canonical_name="Arsenal",
            canonical_normalized="arsenal",
            country="England",
            primary_league_key="england-premier-league",
            primary_league_name="English Premier League",
            provider_ids={"api_football": "42", "statpal": "3001"},
            aliases=["Arsenal FC"],
        )

    def test_team_profile_stores_provider_identity(self):
        self.assertEqual(self.team.provider_ids["api_football"], "42")
        self.assertEqual(self.team.aliases, ["Arsenal FC"])

    def test_team_season_profile_is_unique_per_team_league_season(self):
        TeamSeasonProfile.objects.create(
            team=self.team,
            league_key="england-premier-league",
            league_name="English Premier League",
            country="England",
            season="2026-2027",
            provider_ids={"api_football": "39", "statpal": "3037"},
            matches_played=10,
            goals_for=21,
            goals_against=8,
            data_quality=TeamSeasonProfile.DataQuality.STRONG,
        )

        with self.assertRaises(IntegrityError):
            TeamSeasonProfile.objects.create(
                team=self.team,
                league_key="england-premier-league",
                league_name="English Premier League",
                season="2026-2027",
            )

    def test_recent_market_league_and_coverage_profiles_can_be_created(self):
        recent = TeamRecentFormProfile.objects.create(
            team=self.team,
            league_key="england-premier-league",
            league_name="English Premier League",
            season="2026-2027",
            window=5,
            scope=TeamRecentFormProfile.Scope.HOME,
            matches=5,
            form=["W", "W", "D", "W", "L"],
            stats={"points": 10},
        )
        team_market = TeamMarketProfile.objects.create(
            team=self.team,
            league_key="england-premier-league",
            league_name="English Premier League",
            season="2026-2027",
            market_family="total_goals",
            market="Over 2.5",
            scope=TeamMarketProfile.Scope.HOME,
            side="over",
            line=2.5,
            attempts=10,
            wins=7,
            losses=3,
            hit_rate=70.0,
            data_quality="strong",
        )
        league_market = LeagueMarketProfile.objects.create(
            league_key="england-premier-league",
            league_name="English Premier League",
            country="England",
            season="2026-2027",
            provider_ids={"api_football": "39", "statpal": "3037"},
            market_family="total_goals",
            market="Over 2.5",
            side="over",
            line=2.5,
            attempts=100,
            wins=58,
            losses=42,
            hit_rate=58.0,
            fairness_score=82.0,
            data_quality="strong",
        )
        coverage = DataCoverage.objects.create(
            subject_type=DataCoverage.SubjectType.TEAM,
            subject_key="arsenal",
            team=self.team,
            league_key="england-premier-league",
            league_name="English Premier League",
            season="2026-2027",
            provider="statpal",
            coverage_key="team_stats",
            status=DataCoverage.Status.FRESH,
            available_requirements=["team_stats"],
            missing_requirements=[],
        )

        self.assertEqual(str(recent), "Arsenal last 5 (home)")
        self.assertEqual(str(team_market), "Arsenal Over 2.5 (home)")
        self.assertEqual(str(league_market), "English Premier League Over 2.5 2026-2027")
        self.assertEqual(str(coverage), "team:arsenal team_stats (fresh)")
