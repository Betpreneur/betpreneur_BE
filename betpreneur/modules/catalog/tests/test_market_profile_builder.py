from datetime import date

from django.test import TestCase

from betpreneur.modules.catalog.api import (
    DataCoverage,
    FixtureCache,
    LeagueMarketProfile,
    MarketProfileBuilder,
    TeamMarketProfile,
    TeamProfile,
    TeamSeasonProfile,
)
from betpreneur.modules.catalog.domain.league_registry import TOP_EUROPEAN_INTELLIGENCE_LEAGUES
from betpreneur.modules.catalog.domain.text import normalize_fixture_text


class MarketProfileBuilderTests(TestCase):
    def setUp(self):
        self.league = TOP_EUROPEAN_INTELLIGENCE_LEAGUES[0]
        self.team = TeamProfile.objects.create(
            canonical_name="Arsenal",
            canonical_normalized="arsenal",
            country="England",
            primary_league_key=self.league.key,
            primary_league_name=self.league.name,
            provider_ids={"statpal": {"team_id": "42", "league_id": "3037"}},
        )
        TeamSeasonProfile.objects.create(
            team=self.team,
            league_key=self.league.key,
            league_name=self.league.name,
            country="England",
            season="2026-2027",
            provider_ids={"statpal": {"team_id": "42", "league_id": "3037"}},
            matches_played=20,
            data_quality=TeamSeasonProfile.DataQuality.STRONG,
        )

    def _fixture(self, day, home, away, home_id, away_id, hg, ag, *, stats):
        FixtureCache.objects.create(
            match_date=date(2026, 8, day),
            fixture=f"{home} vs {away}",
            home_team=home,
            away_team=away,
            home_team_normalized=normalize_fixture_text(home),
            away_team_normalized=normalize_fixture_text(away),
            fixture_normalized=normalize_fixture_text(f"{home} vs {away}"),
            league=self.league.name,
            country="England",
            match_id=f"statpal:m{day}",
            source="statpal",
            api_payload={
                "provider_competition_id": "3037",
                "provider_match_id": f"m{day}",
                "provider_home_team_id": str(home_id),
                "provider_away_team_id": str(away_id),
                "status": "Finished",
                "home_goals": hg,
                "away_goals": ag,
                "team_stats": stats,
            },
        )

    def test_builds_league_profiles_for_score_goals_corners_cards_and_sot(self):
        self._fixture(
            1,
            "Arsenal",
            "Chelsea",
            "42",
            "50",
            2,
            1,
            stats={
                "home": {
                    "corners": {"total": 7},
                    "yellowcards": {"total": 1},
                    "shots_on_goal": {"total": 5},
                },
                "away": {
                    "corners": {"total": 4},
                    "yellowcards": {"total": 3},
                    "shots_on_goal": {"total": 3},
                },
            },
        )

        result = MarketProfileBuilder().build(
            league_keys=[self.league.key],
            seasons=["2026-2027"],
        )

        self.assertGreater(result["league_profiles_saved"], 0)
        self.assertEqual(
            LeagueMarketProfile.objects.get(league_key=self.league.key, season="2026-2027", market="Home Win").hit_rate,
            100.0,
        )
        self.assertEqual(
            LeagueMarketProfile.objects.get(league_key=self.league.key, season="2026-2027", market="Over 2.5").hit_rate,
            100.0,
        )
        self.assertEqual(
            LeagueMarketProfile.objects.get(league_key=self.league.key, season="2026-2027", market="GG / BTTS Yes").hit_rate,
            100.0,
        )
        self.assertEqual(
            LeagueMarketProfile.objects.get(league_key=self.league.key, season="2026-2027", market="Corners Over 10.5").hit_rate,
            100.0,
        )
        self.assertEqual(
            LeagueMarketProfile.objects.get(league_key=self.league.key, season="2026-2027", market="Cards Under 4.5").hit_rate,
            100.0,
        )
        self.assertEqual(
            LeagueMarketProfile.objects.get(
                league_key=self.league.key,
                season="2026-2027",
                market="Shots On Target Over 7.5",
            ).hit_rate,
            100.0,
        )

    def test_builds_team_profiles_for_result_team_goals_and_specialist_counts(self):
        self._fixture(
            1,
            "Arsenal",
            "Chelsea",
            "42",
            "50",
            2,
            1,
            stats={
                "home": {
                    "corners": {"total": 7},
                    "yellowcards": {"total": 1},
                    "shots_on_goal": {"total": 5},
                },
                "away": {
                    "corners": {"total": 4},
                    "yellowcards": {"total": 3},
                    "shots_on_goal": {"total": 3},
                },
            },
        )

        MarketProfileBuilder().build(
            league_keys=[self.league.key],
            seasons=["2026-2027"],
        )

        home_win = TeamMarketProfile.objects.get(team=self.team, market="Home Win", scope=TeamMarketProfile.Scope.HOME)
        team_goals = TeamMarketProfile.objects.get(
            team=self.team,
            market="Home Team Goals Over 1.5",
            scope=TeamMarketProfile.Scope.HOME,
        )
        team_corners = TeamMarketProfile.objects.get(
            team=self.team,
            market="Home Team Corners Over 5.5",
            scope=TeamMarketProfile.Scope.HOME,
        )
        team_cards = TeamMarketProfile.objects.get(
            team=self.team,
            market="Home Team Cards Under 1.5",
            scope=TeamMarketProfile.Scope.HOME,
        )
        team_sot = TeamMarketProfile.objects.get(
            team=self.team,
            market="Home Team Shots On Target Over 4.5",
            scope=TeamMarketProfile.Scope.HOME,
        )
        self.assertEqual(home_win.hit_rate, 100.0)
        self.assertEqual(team_goals.hit_rate, 100.0)
        self.assertEqual(team_corners.hit_rate, 100.0)
        self.assertEqual(team_cards.hit_rate, 100.0)
        self.assertEqual(team_sot.hit_rate, 100.0)
        self.assertTrue(
            DataCoverage.objects.filter(
                team=self.team,
                coverage_key=MarketProfileBuilder.COVERAGE_KEY,
                status=DataCoverage.Status.PARTIAL,
            ).exists()
        )
