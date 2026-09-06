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
                            "Recent scoreline sample: 2-2, 3-1, 1-2.",
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
        self.assertIn("Recent scoreline sample: 2-2, 3-1, 1-2.", game["analysis"]["key_points"])
        self.assertEqual(game["recent_form"]["home"]["scorelines"][0]["scoreline"], "2-2")
