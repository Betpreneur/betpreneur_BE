from django.test import SimpleTestCase

from betpreneur.modules.picks.services.presentation import (
    _market_reasoning_for_game,
    public_game_detail_payload,
    _public_reasoning_text,
)


class PublicReasoningTests(SimpleTestCase):
    def test_public_reasoning_removes_provider_pricing_sentence(self):
        text = (
            "Under 3.5 rates at 68% final confidence after council review with 1.45 odds "
            "and +0.145 expected value. Expected goals sit around 1.75. "
            "Pricing is based on api_football odds."
        )

        cleaned = _public_reasoning_text(text)

        self.assertIn("Expected goals sit around 1.75.", cleaned)
        self.assertNotIn("Pricing is based on", cleaned)
        self.assertNotIn("api_football", cleaned)

    def test_market_reasoning_for_game_does_not_expose_odds_provider(self):
        reasoning = _market_reasoning_for_game(
            {
                "market": "Under 3.5",
                "confidence": 68,
                "final_confidence": 68,
                "odds": 1.45,
                "ev": 0.145,
                "odds_source": "api_football",
            },
            {
                "home_recent_form": {"games": 2, "wins": 0, "draws": 1, "losses": 1, "avg_scored": 0.0, "avg_conceded": 0.5},
                "away_recent_form": {"games": 32, "wins": 17, "draws": 7, "losses": 8, "avg_scored": 1.75, "avg_conceded": 1.31},
                "fixture_context": {"goal_model": {"expected_total": 1.75}},
            },
        )

        self.assertIn("Under 3.5 rates at 68% confidence", reasoning)
        self.assertIn("Expected goals sit around 1.75.", reasoning)
        self.assertNotIn("Pricing is based on", reasoning)
        self.assertNotIn("api_football", reasoning)

    def test_public_game_detail_exposes_recent_scoreline_context(self):
        payload = public_game_detail_payload(
            {
                "date": "2026-09-06",
                "published": False,
                "run_id": 371,
                "posted_at": "2026-09-06T00:48:50.927930Z",
                "game": {
                    "match_id": "statpal:2026090618418",
                    "fixture": "Everton vs Manchester Utd",
                    "home_team": "Everton",
                    "away_team": "Manchester Utd",
                    "top_market": {
                        "market": "Over 2.5",
                        "meaning": "3 or more total goals",
                        "confidence": 58,
                        "odds": 1.73,
                        "recommendation_status": "no_edge",
                        "analysis_summary": "Over 2.5 has 58% calibrated model confidence.",
                        "positive_evidence": [
                            "Projected total goals: 3.03.",
                            "Home average: 1.86 xG.",
                            "Away average: 1.17 xG.",
                            "Line 2.5 is below the model projection of 3.03 goals.",
                            "Tracked scorelines used: Everton 2-2 Leeds; Everton 3-1 Newcastle; H2H: Everton vs Manchester Utd 1-2.",
                            "Recent scorelines average 3.40 total goals across 10 tracked matches.",
                            "Recent scoreline Over 2.5 rate: 70.0%.",
                        ],
                    },
                    "home_recent_form": {
                        "form": ["D", "W"],
                        "wins": 1,
                        "draws": 1,
                        "losses": 0,
                        "games": 2,
                        "avg_scored": 2.0,
                        "avg_conceded": 1.5,
                        "fixtures": [
                            {
                                "match_date": "2026-09-01",
                                "fixture": "Everton vs Leeds",
                                "result": "D",
                                "goals_for": 2,
                                "goals_against": 2,
                            }
                        ],
                    },
                    "away_recent_form": {},
                },
            }
        )

        game = payload["game"]
        self.assertIn(
            "Tracked scorelines used: Everton 2-2 Leeds; Everton 3-1 Newcastle; H2H: Everton vs Manchester Utd 1-2.",
            game["analysis"]["key_points"],
        )
        self.assertEqual(game["recent_form"]["home"]["scorelines"][0]["scoreline"], "2-2")

    def test_public_game_detail_exposes_clean_phase5_analysis_shape(self):
        payload = public_game_detail_payload(
            {
                "date": "2026-09-08",
                "published": True,
                "run_id": 388,
                "posted_at": "2026-09-07T23:05:00Z",
                "game": {
                    "match_id": "statpal:2026090826111",
                    "fixture": "Lille vs Betis",
                    "home_team": "Lille",
                    "away_team": "Betis",
                    "top_market": {
                        "market": "Corners Over 7.5",
                        "meaning": "Match to finish with more than 7.5 total corners",
                        "confidence": 70,
                        "odds": 1.9,
                        "odds_source": "statpal",
                        "analysis_summary": "Corners Over 7.5 has 70% calibrated model confidence.",
                        "positive_evidence": [
                            "Projected corners: 10.24.",
                            "Home team averages 5.79 corners.",
                            "Away team averages 4.45 corners.",
                            "API-Football recent scorelines support Over 2.5 at 70% across 10 games.",
                            "Stored league profile: 58.0% hit rate for Corners Over 7.5 across 120 matches.",
                            "Model fair odds: 1.25.",
                        ],
                        "risk_evidence": ["Projected lineup data is not available yet."],
                        "insights": {"data_quality": "medium"},
                    },
                    "fixture_context": {
                        "statpal": {"available": True},
                        "api_football": {"available": True},
                        "prediction_features": {
                            "api_football": {
                                "available": True,
                                "available_snapshots": [
                                    "prediction",
                                    "team_statistics_home",
                                    "team_statistics_away",
                                    "recent_fixtures_home",
                                    "recent_fixtures_away",
                                    "fixture_statistics_home",
                                    "fixture_statistics_away",
                                ],
                                "corner_samples": {
                                    "combined": {
                                        "games": 10,
                                        "avg_for": 5.4,
                                        "avg_against": 4.7,
                                        "avg_total": 10.1,
                                    }
                                },
                            }
                        },
                    },
                    "corner_profile": {
                        "data_quality": "medium",
                        "expected_total": 10.24,
                        "sources": ["api_football_corner_samples", "team_rate_profile"],
                        "warnings": [],
                        "home": {"avg_for": 5.79, "expected_for": 5.79},
                        "away": {"avg_for": 4.45, "expected_for": 4.45},
                    },
                },
            }
        )

        game = payload["game"]
        self.assertNotIn("fixture_context", game)
        self.assertIn("projection", game["analysis"]["evidence"])
        self.assertIn("provider_context", game["analysis"]["evidence"])
        self.assertEqual(game["analysis"]["data_sources"][0]["name"], "StatPal")
        self.assertEqual(game["analysis"]["data_sources"][1]["name"], "API-Football")
        self.assertIn("historical corner statistics", game["analysis"]["data_sources"][1]["used_for"])
        self.assertEqual(game["recommended_market"]["key_points"][0], "Projected corners: 10.24.")
        self.assertTrue(game["corners"]["historical_samples"]["available"])
        self.assertEqual(game["corners"]["historical_samples"]["games"], 10)
