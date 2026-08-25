from django.test import SimpleTestCase

from betpreneur.modules.picks.services.presentation import (
    _market_reasoning_for_game,
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
