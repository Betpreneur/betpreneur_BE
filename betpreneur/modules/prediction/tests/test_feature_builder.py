from datetime import date

from django.test import TestCase
from django.utils import timezone

from betpreneur.modules.catalog.api import (
    FixtureCache,
    LeagueMarketProfile,
    StatPalFixtureSnapshot,
    TeamMarketProfile,
    TeamProfile,
    TeamRecentFormProfile,
    TeamSeasonProfile,
    normalize_fixture_text,
)
from betpreneur.modules.prediction.api import FixtureFeatureSet, build_fixture_features
from betpreneur.modules.scoring.api import FixtureLineup, PlayerAvailability, TeamRateProfile


class FixtureFeatureBuilderTests(TestCase):
    def setUp(self):
        self.home = TeamProfile.objects.create(
            canonical_name="Arsenal",
            canonical_normalized=normalize_fixture_text("Arsenal"),
            country="England",
            primary_league_key="england-premier-league",
            provider_ids={"statpal": {"team_id": "home-1"}},
        )
        self.away = TeamProfile.objects.create(
            canonical_name="Chelsea",
            canonical_normalized=normalize_fixture_text("Chelsea"),
            country="England",
            primary_league_key="england-premier-league",
            provider_ids={"statpal": {"team_id": "away-1"}},
        )
        self.fixture = FixtureCache.objects.create(
            match_date=date(2026, 8, 29),
            fixture="Arsenal vs Chelsea",
            home_team="Arsenal",
            away_team="Chelsea",
            league="English Premier League",
            country="England",
            kickoff="15:00",
            match_id="fixture-123",
            source="statpal",
            api_payload={
                "season": "2026-2027",
                "league_id": "3037",
                "home_team_id": "home-1",
                "away_team_id": "away-1",
                "referee": {"name": "Michael Salisbury"},
            },
        )
        TeamSeasonProfile.objects.create(
            team=self.home,
            league_key="england-premier-league",
            league_name="English Premier League",
            country="England",
            season="2026-2027",
            matches_played=10,
            home_matches=5,
            away_matches=5,
            goals_for=22,
            goals_against=10,
            home_goals_for=14,
            home_goals_against=4,
            away_goals_for=8,
            away_goals_against=6,
            data_quality="strong",
            source="statpal",
        )
        TeamSeasonProfile.objects.create(
            team=self.away,
            league_key="england-premier-league",
            league_name="English Premier League",
            country="England",
            season="2026-2027",
            matches_played=10,
            home_matches=5,
            away_matches=5,
            goals_for=18,
            goals_against=12,
            home_goals_for=11,
            home_goals_against=5,
            away_goals_for=7,
            away_goals_against=7,
            data_quality="medium",
            source="statpal",
        )
        for team, scope in ((self.home, "all"), (self.home, "home"), (self.away, "all"), (self.away, "away")):
            TeamRecentFormProfile.objects.create(
                team=team,
                league_key="england-premier-league",
                league_name="English Premier League",
                season="2026-2027",
                window=5,
                scope=scope,
                matches=5,
                wins=3,
                draws=1,
                losses=1,
                goals_for=10,
                goals_against=5,
                corners_for=32,
                shots_on_target_for=21,
            )
        TeamMarketProfile.objects.create(
            team=self.home,
            league_key="england-premier-league",
            league_name="English Premier League",
            season="2026-2027",
            market_family="total_goals",
            market="Over 2.5",
            scope="home",
            attempts=5,
            wins=4,
            losses=1,
            hit_rate=80,
            confidence=72,
            data_quality="medium",
        )
        LeagueMarketProfile.objects.create(
            league_key="england-premier-league",
            league_name="English Premier League",
            country="England",
            season="2026-2027",
            market_family="corners_total",
            market="Corners Over 7.5",
            attempts=40,
            wins=30,
            losses=10,
            hit_rate=75,
            confidence=70,
            data_quality="strong",
        )
        TeamRateProfile.objects.create(
            provider="statpal",
            team_id="home-1",
            team_name="Arsenal",
            league_id="3037",
            corners_home=6.2,
            corners_away=4.8,
            cards_home=1.4,
            cards_away=2.1,
            shots_on_target_home=6.5,
            shots_on_target_away=4.2,
            matches=10,
        )
        FixtureLineup.objects.create(
            provider="statpal",
            match_id="fixture-123",
            side="home",
            team_id="home-1",
            team_name="Arsenal",
            formation="4-3-3",
            confidence=100,
            starting_xi=[{"name": "Player A"}],
            bench=[{"name": "Player B"}],
        )
        PlayerAvailability.objects.create(
            provider="statpal",
            player_id="p-1",
            player_name="Unavailable Player",
            player_name_normalized=normalize_fixture_text("Unavailable Player"),
            team_id="home-1",
            team_name="Arsenal",
            team_name_normalized=normalize_fixture_text("Arsenal"),
            match_id="fixture-123",
            status="out",
        )
        StatPalFixtureSnapshot.objects.create(
            fixture=self.fixture,
            match_id="fixture-123",
            provider_match_id="fixture-123",
            provider_competition_id="3037",
            snapshot_type=StatPalFixtureSnapshot.SnapshotType.PREMATCH_ODDS,
            status="available",
            source_endpoint="soccer/prematch-odds",
            summary={"markets": 14},
            fetched_at=timezone.now(),
        )
        for index, cards in enumerate((5, 4, 6), start=1):
            StatPalFixtureSnapshot.objects.create(
                match_id=f"historic-{index}",
                provider_match_id=f"historic-{index}",
                provider_competition_id="3037",
                snapshot_type=StatPalFixtureSnapshot.SnapshotType.DETAILED_STATS,
                status="available",
                source_endpoint="SOCCER_DETAILED_STATS",
                summary={
                    "referee_name": "Michael Salisbury, England",
                    "referee_normalized": "michael salisbury",
                    "total_cards": cards,
                    "booking_points": cards * 10,
                },
                fetched_at=timezone.now(),
            )

    def test_build_fixture_features_returns_shared_feature_set(self):
        feature_set = build_fixture_features(self.fixture)

        self.assertIsInstance(feature_set, FixtureFeatureSet)
        self.assertEqual(feature_set.fixture_id, "fixture-123")
        self.assertEqual(feature_set.league_key, "england-premier-league")
        self.assertEqual(feature_set.season, "2026-2027")
        self.assertEqual(feature_set.home_team.team_name, "Arsenal")
        self.assertEqual(feature_set.home_team.attack_rating, 2.8)
        self.assertEqual(feature_set.away_team.defence_rating, 1.4)
        self.assertEqual(feature_set.home_team.recent_form_score, 2.0)

        features = feature_set.features
        self.assertEqual(features["fixture"]["provider_league_id"], "3037")
        self.assertIn("5", features["home"]["recent_form"]["home"])
        self.assertEqual(features["home"]["rate_profile"]["corners_home"], 6.2)
        self.assertEqual(features["lineups"]["home"]["formation"], "4-3-3")
        self.assertEqual(features["player_availability"]["home"]["by_status"]["out"], 1)
        self.assertEqual(features["odds_snapshots"]["prematch"]["summary"]["markets"], 14)
        self.assertEqual(features["referee"]["name"], "Michael Salisbury")
        self.assertEqual(features["referee"]["normalized"], "michael salisbury")
        self.assertEqual(features["referee"]["sample_matches"], 3)
        self.assertEqual(features["referee"]["avg_cards_per_match"], 5.0)
        self.assertIn("total_goals", features["market_family_history"]["home"])
        self.assertIn("corners_total", features["market_family_history"]["league"])

    def test_build_fixture_features_supports_dict_input(self):
        feature_set = build_fixture_features(
            {
                "match_id": "manual-1",
                "fixture": "Manual Home vs Manual Away",
                "match_date": "2026-08-29",
            }
        )

        self.assertEqual(feature_set.fixture_id, "manual-1")
        self.assertEqual(feature_set.fixture_name, "Manual Home vs Manual Away")
        self.assertEqual(feature_set.season, "2026-2027")
        self.assertIn("goal_model_unavailable", feature_set.diagnostics.warnings)

    def test_recent_form_averages_are_not_double_divided(self):
        TeamRecentFormProfile.objects.create(
            team=self.home,
            league_key="england-premier-league",
            league_name="English Premier League",
            season="2026-2027",
            window=10,
            scope="all",
            matches=10,
            wins=4,
            draws=3,
            losses=3,
            goals_for=1.7,
            goals_against=1.6,
            corners_for=6.2,
            shots_on_target_for=5.1,
        )

        feature_set = build_fixture_features(self.fixture)
        recent = feature_set.features["home"]["recent_form"]["all"]["10"]

        self.assertEqual(recent["goals_for_per_match"], 1.7)
        self.assertEqual(recent["goals_against_per_match"], 1.6)
        self.assertEqual(recent["corners_for_per_match"], 6.2)
        self.assertEqual(recent["shots_on_target_for_per_match"], 5.1)
