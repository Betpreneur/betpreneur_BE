from datetime import date

from django.test import TestCase
from django.utils import timezone

from betpreneur.modules.catalog.api import (
    CoachProfile,
    CoachTacticalProfile,
    FixtureCache,
    LeagueMarketProfile,
    StatPalFixtureSnapshot,
    TeamCoachAssignment,
    TeamMarketProfile,
    TeamProfile,
    TeamRecentFormProfile,
    TeamSeasonProfile,
    normalize_fixture_text,
)
from betpreneur.modules.prediction.api import (
    FixtureFeatureSet,
    PredictionTeamMatchFeedback,
    build_fixture_features,
)
from betpreneur.modules.prediction.feature_builder import _coach_payload
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
        recent_fixture_rows = [
            {"match_id": "r1", "match_date": "2026-08-20", "fixture": "Arsenal vs Team A", "opponent": "Team A", "result": "W", "goals_for": 4, "goals_against": 2},
            {"match_id": "r2", "match_date": "2026-08-17", "fixture": "Team B vs Arsenal", "opponent": "Team B", "result": "D", "goals_for": 2, "goals_against": 2},
            {"match_id": "r3", "match_date": "2026-08-13", "fixture": "Arsenal vs Team C", "opponent": "Team C", "result": "W", "goals_for": 3, "goals_against": 1},
            {"match_id": "r4", "match_date": "2026-08-09", "fixture": "Team D vs Arsenal", "opponent": "Team D", "result": "L", "goals_for": 1, "goals_against": 2},
            {"match_id": "r5", "match_date": "2026-08-04", "fixture": "Arsenal vs Team E", "opponent": "Team E", "result": "W", "goals_for": 2, "goals_against": 1},
        ]
        home_fixture_rows = [
            {"match_id": "h1", "match_date": "2026-08-18", "fixture": "Arsenal vs Home Team", "opponent": "Home Team", "result": "W", "goals_for": 1, "goals_against": 0},
        ]
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
                stats={"fixtures": home_fixture_rows if scope == "home" else recent_fixture_rows},
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
        StatPalFixtureSnapshot.objects.create(
            fixture=self.fixture,
            match_id="fixture-123",
            provider_match_id="fixture-123",
            provider_competition_id="3037",
            snapshot_type=StatPalFixtureSnapshot.SnapshotType.HEAD_TO_HEAD,
            status="available",
            source_endpoint="SOCCER_HEAD_TO_HEAD",
            payload={
                "recent_meetings": [
                    {
                        "match_id": "h2h-1",
                        "date": "2026-04-01",
                        "team1_name": "Arsenal",
                        "team2_name": "Chelsea",
                        "team1_score": 3,
                        "team2_score": 2,
                    }
                ],
            },
            summary={"recent_meetings_count": 1},
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
        home_coach = CoachProfile.objects.create(
            canonical_name="Mikel Arteta",
            canonical_normalized=normalize_fixture_text("Mikel Arteta"),
            research_status=CoachProfile.ResearchStatus.APPROVED,
            research_confidence=CoachProfile.Confidence.HIGH,
        )
        TeamCoachAssignment.objects.create(
            team=self.home,
            coach=home_coach,
            currently_active=True,
            first_detected_at=timezone.now(),
            last_confirmed_at=timezone.now(),
        )
        CoachTacticalProfile.objects.create(
            coach=home_coach,
            team=self.home,
            status=CoachTacticalProfile.Status.APPROVED,
            effective_from=date(2026, 7, 1),
            preferred_formation="4-3-3",
            philosophy_summary="High-possession positional play with aggressive counterpressing.",
            attacking_style="Positional attacking",
            build_up_style="Patient build-up",
            defensive_style="High press",
            source_urls=["https://example.com/arteta"],
            attacking_tempo=72,
            pressing_intensity=84,
            defensive_line_height=78,
            tactical_flexibility=70,
        )

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
        self.assertEqual(features["home"]["coach"]["coach_name"], "Mikel Arteta")
        self.assertEqual(features["home"]["coach"]["tactical_profile"]["preferred_formation"], "4-3-3")
        self.assertEqual(features["home"]["coach"]["tactical_profile"]["ratings"]["pressing_intensity"], 84)
        self.assertTrue(features["coach_tactical_matchup"]["available"])
        self.assertIn("corners_total", features["market_family_history"]["league"])
        self.assertEqual(features["scoreline_profile"]["home_recent"]["scorelines"][0]["scoreline"], "4-2")
        self.assertEqual(features["scoreline_profile"]["head_to_head"]["scorelines"][0]["scoreline"], "3-2")
        self.assertGreaterEqual(features["scoreline_profile"]["combined"]["over_2_5_rate"], 80)

    def test_coach_payload_falls_back_to_team_alias_when_team_intelligence_is_missing(self):
        team = TeamProfile.objects.create(
            canonical_name="Ipswich Town",
            canonical_normalized=normalize_fixture_text("Ipswich Town"),
            aliases=["Ipswich"],
        )
        coach = CoachProfile.objects.create(
            canonical_name="Kieran McKenna",
            canonical_normalized=normalize_fixture_text("Kieran McKenna"),
        )
        TeamCoachAssignment.objects.create(
            team=team,
            coach=coach,
            currently_active=True,
            first_detected_at=timezone.now(),
            last_confirmed_at=timezone.now(),
        )
        CoachTacticalProfile.objects.create(
            coach=coach,
            team=team,
            status=CoachTacticalProfile.Status.APPROVED,
            preferred_formation="4-2-3-1",
            philosophy_summary="Aggressive build-up with compact defensive recovery.",
            attacking_style="Direct wide attacks",
            build_up_style="Vertical build-up",
            defensive_style="Mid-block press",
            source_urls=["https://example.com/ipswich"],
        )

        payload = _coach_payload(
            None,
            fallback_name="Ipswich",
            provider_team_id="",
            fixture_date=date(2026, 9, 12),
        )

        self.assertTrue(payload["available"])
        self.assertEqual(payload["coach_name"], "Kieran McKenna")
        self.assertEqual(payload["tactical_profile"]["preferred_formation"], "4-2-3-1")

    def test_build_fixture_features_normalizes_api_football_snapshots(self):
        feature_set = build_fixture_features(
            {
                "match_id": "api-1",
                "fixture": "Arsenal vs Chelsea",
                "home_team": "Arsenal",
                "away_team": "Chelsea",
                "match_date": "2026-08-29",
                "api_football_league_id": "39",
                "api_football_home_team_id": "42",
                "api_football_away_team_id": "49",
                "season": "2026",
                "api_football_context": {
                    "available": True,
                    "snapshots": {
                        "prediction": {
                            "available": True,
                            "payload": {
                                "predictions": {
                                    "winner": {"id": 42, "name": "Arsenal", "comment": "Win or draw"},
                                    "win_or_draw": True,
                                    "under_over": "+2.5",
                                    "goals": {"home": "+1.5", "away": "-1.5"},
                                    "advice": "Home or draw and over 2.5 goals",
                                    "percent": {"home": "45%", "draw": "30%", "away": "25%"},
                                },
                                "comparison": {"att": {"home": "55%", "away": "45%"}},
                                "h2h": [
                                    {
                                        "fixture": {"id": 10, "date": "2026-02-01T15:00:00+00:00"},
                                        "teams": {
                                            "home": {"id": 42, "name": "Arsenal"},
                                            "away": {"id": 49, "name": "Chelsea"},
                                        },
                                        "goals": {"home": 3, "away": 2},
                                    }
                                ],
                            },
                        },
                        "team_statistics_home": {
                            "available": True,
                            "payload": {
                                "team": {"id": 42, "name": "Arsenal"},
                                "fixtures": {
                                    "played": {"home": 5, "away": 5, "total": 10},
                                    "wins": {"home": 4, "away": 2, "total": 6},
                                    "draws": {"home": 1, "away": 1, "total": 2},
                                    "loses": {"home": 0, "away": 2, "total": 2},
                                },
                                "goals": {
                                    "for": {
                                        "average": {"home": "2.0", "away": "1.4", "total": "1.7"},
                                        "under_over": {"2.5": {"over": 4, "under": 6}},
                                    },
                                    "against": {
                                        "average": {"home": "0.8", "away": "1.2", "total": "1.0"},
                                        "under_over": {"1.5": {"over": 3, "under": 7}},
                                    },
                                },
                                "clean_sheet": {"total": 4},
                                "failed_to_score": {"total": 1},
                                "lineups": [{"formation": "4-3-3", "played": 8}],
                            },
                        },
                        "recent_fixtures_home": {
                            "available": True,
                            "payload": [
                                {
                                    "fixture": {"id": 99, "date": "2026-08-20T12:00:00+00:00"},
                                    "teams": {
                                        "home": {"id": 42, "name": "Arsenal"},
                                        "away": {"id": 8, "name": "Team A"},
                                    },
                                    "goals": {"home": 4, "away": 1},
                                }
                            ],
                        },
                        "fixture_statistics_home": {
                            "available": True,
                            "payload": [
                                {
                                    "fixture_id": "99",
                                    "fixture": "Arsenal vs Team A",
                                    "date": "2026-08-20T12:00:00+00:00",
                                    "corner_kicks_for": 7,
                                    "payload": [
                                        {
                                            "team": {"id": 42},
                                            "statistics": [{"type": "Corner Kicks", "value": 7}],
                                        },
                                        {
                                            "team": {"id": 8},
                                            "statistics": [{"type": "Corner Kicks", "value": 4}],
                                        },
                                    ],
                                }
                            ],
                        },
                    },
                },
            }
        )

        api_features = feature_set.features["api_football"]
        self.assertTrue(api_features["available"])
        self.assertEqual(api_features["prediction_opinion"]["percent"]["home"], 45.0)
        self.assertEqual(api_features["prediction_opinion"]["comparison"]["att"]["home"], 55.0)
        self.assertEqual(api_features["team_statistics"]["home"]["goals_for"]["average"]["home"], 2.0)
        self.assertEqual(api_features["team_statistics"]["home"]["clean_sheet_rate"], 40.0)
        self.assertEqual(api_features["recent_scorelines"]["home"]["scorelines"][0]["scoreline"], "4-1")
        self.assertEqual(api_features["head_to_head"]["scorelines"][0]["scoreline"], "3-2")
        self.assertEqual(api_features["corner_samples"]["home"]["avg_for"], 7.0)
        self.assertEqual(api_features["corner_samples"]["home"]["avg_total"], 11.0)

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

    def test_build_fixture_features_includes_previous_prediction_feedback(self):
        PredictionTeamMatchFeedback.objects.create(
            fixture_id="previous-1",
            fixture_name="Arsenal vs Aston Villa",
            match_date=date(2026, 8, 20),
            team_name="Arsenal",
            opponent_name="Aston Villa",
            side="home",
            actual_result="loss",
            goals_for=1,
            goals_against=2,
            corners_for=7,
            corners_against=4,
            cards_for=2,
            cards_against=3,
            prediction_snapshot={
                "markets": [{"market": "Over 2.5", "confidence": 72, "data_status": "modelled"}]
            },
        )

        feature_set = build_fixture_features(self.fixture)
        feedback = feature_set.features["prediction_feedback"]["home"]

        self.assertEqual(feedback["matches"], 1)
        self.assertEqual(feedback["summary"]["avg_goals_for"], 1.0)
        self.assertEqual(feedback["recent"][0]["opponent"], "Aston Villa")
        self.assertEqual(feedback["recent"][0]["prediction_snapshot"]["markets"][0]["market"], "Over 2.5")
