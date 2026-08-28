from django.test import SimpleTestCase

from betpreneur.modules.prediction.api import (
    MarketProbability,
    PredictionDiagnostics,
    RecommendationScore,
    ValueAssessment,
)
from betpreneur.modules.pricing.api import (
    Tier,
    assess_all_games_policy,
    assess_slip_review_policy,
    assess_top_picks_policy,
)


def _market(
    market,
    *,
    fixture_id="fixture-1",
    raw=0.72,
    calibrated=0.68,
    confidence=68,
    family="total_goals",
    quality="strong",
):
    return MarketProbability(
        fixture_id=fixture_id,
        market=market,
        raw_probability=raw,
        calibrated_probability=calibrated,
        confidence_score=confidence,
        data_quality=quality,
        explanation_facts=("Projected total goals: 2.9.",),
        diagnostics=PredictionDiagnostics(
            metadata={"market_family": family, "market_support_level": "strong"}
        ),
    )


class ProductPolicyTests(SimpleTestCase):
    def test_all_games_policy_explains_model_without_profit_claims(self):
        assessment = assess_all_games_policy(_market("Over 2.5"))

        self.assertEqual(assessment.product, "all_games")
        self.assertEqual(assessment.raw_probability, 0.72)
        self.assertEqual(assessment.calibrated_probability, 0.68)
        self.assertEqual(assessment.data_confidence, 68)
        self.assertEqual(assessment.selection_bias, "coverage")
        self.assertFalse(assessment.aggressive_profit_claims)
        self.assertEqual(assessment.explanation_facts, ("Projected total goals: 2.9.",))

    def test_top_picks_policy_requires_balanced_score_real_odds_edge_and_ev(self):
        market = _market("Over 2.5", calibrated=0.74, confidence=74)
        value = ValueAssessment(
            fixture_id="fixture-1",
            market="Over 2.5",
            calibrated_probability=0.74,
            available_odds=1.7,
            edge=0.15,
            ev=0.258,
            diagnostics=PredictionDiagnostics(metadata={"estimated_odds": False}),
        )
        score = RecommendationScore(
            fixture_id="fixture-1",
            market="Over 2.5",
            recommendation_score=82,
        )

        assessment = assess_top_picks_policy(market, value, score)

        self.assertEqual(assessment.product, "top_picks")
        self.assertTrue(assessment.publishable)
        self.assertEqual(assessment.tier, Tier.BANKER)
        self.assertEqual(assessment.reasons, ())
        self.assertIn("stable_league_market_profile", assessment.tier_reasons)
        self.assertEqual(assessment.stake_warning, "")

    def test_top_picks_policy_blocks_short_bad_value_even_with_high_probability(self):
        market = _market("Over 1.5", calibrated=0.83, confidence=83, quality="limited")
        value = ValueAssessment(
            fixture_id="fixture-1",
            market="Over 1.5",
            calibrated_probability=0.83,
            available_odds=1.1,
            edge=-0.079,
            ev=-0.087,
            diagnostics=PredictionDiagnostics(metadata={"estimated_odds": False}),
        )
        score = RecommendationScore(
            fixture_id="fixture-1",
            market="Over 1.5",
            recommendation_score=73,
        )

        assessment = assess_top_picks_policy(market, value, score)

        self.assertFalse(assessment.publishable)
        self.assertIn("insufficient_edge", assessment.reasons)
        self.assertIn("insufficient_ev", assessment.reasons)
        self.assertNotEqual(assessment.tier, Tier.BANKER)

    def test_top_picks_policy_does_not_make_weak_market_a_banker(self):
        market = _market("Over 1.5", calibrated=0.84, confidence=84, quality="limited")
        market = MarketProbability(
            fixture_id=market.fixture_id,
            market=market.market,
            raw_probability=market.raw_probability,
            calibrated_probability=market.calibrated_probability,
            confidence_score=market.confidence_score,
            data_quality=market.data_quality,
            diagnostics=PredictionDiagnostics(
                metadata={"market_family": "total_goals", "market_support_level": "weak"}
            ),
        )
        value = ValueAssessment(
            fixture_id="fixture-1",
            market="Over 1.5",
            calibrated_probability=0.84,
            available_odds=1.35,
            edge=0.09,
            ev=0.134,
            diagnostics=PredictionDiagnostics(metadata={"estimated_odds": False}),
        )
        score = RecommendationScore(
            fixture_id="fixture-1",
            market="Over 1.5",
            recommendation_score=88,
            weak_market_penalty=6,
            warnings=("weak_market_penalty",),
        )

        assessment = assess_top_picks_policy(market, value, score)

        self.assertTrue(assessment.publishable)
        self.assertEqual(assessment.tier, Tier.WILD_CARD)
        self.assertIn("higher_variance", assessment.tier_reasons)
        self.assertEqual(
            assessment.stake_warning, "Higher-variance pick: use reduced stake sizing."
        )

    def test_top_picks_policy_value_gem_requires_real_edge_and_fit(self):
        market = _market("BTTS Yes", calibrated=0.63, confidence=63, family="both_teams_to_score")
        value = ValueAssessment(
            fixture_id="fixture-1",
            market="BTTS Yes",
            calibrated_probability=0.63,
            available_odds=1.85,
            edge=0.089,
            ev=0.1655,
            sample_size_penalty=4,
            diagnostics=PredictionDiagnostics(metadata={"estimated_odds": False}),
        )
        score = RecommendationScore(
            fixture_id="fixture-1",
            market="BTTS Yes",
            recommendation_score=76,
            market_fit_score=72,
        )

        assessment = assess_top_picks_policy(market, value, score)

        self.assertTrue(assessment.publishable)
        self.assertEqual(assessment.tier, Tier.VALUE_GEM)
        self.assertIn("positive_edge", assessment.tier_reasons)
        self.assertIn("good_market_fit", assessment.tier_reasons)

    def test_slip_review_policy_prefers_close_thesis_preserving_alternative(self):
        user_pick = _market("Over 2.5", calibrated=0.56, confidence=56, family="total_goals")
        same_family = _market("Over 1.5", calibrated=0.72, confidence=72, family="total_goals")
        far_top_pick_style = _market(
            "DC: 1X", calibrated=0.78, confidence=78, family="double_chance"
        )

        assessment = assess_slip_review_policy(
            user_pick,
            (
                {
                    "probability": far_top_pick_style,
                    "thesis_preserved": False,
                    "family_distance": 2,
                },
                {"probability": same_family, "thesis_preserved": True, "family_distance": 0},
            ),
        )

        self.assertEqual(assessment.product, "slip_review")
        self.assertFalse(assessment.supported)
        self.assertEqual(assessment.verdict, "replace")
        self.assertEqual(assessment.suggested_alternative.market, "Over 1.5")
        self.assertTrue(assessment.suggested_alternative.thesis_preserved)

    def test_slip_review_policy_keeps_supported_user_pick_without_top_pick_tier(self):
        user_pick = _market("Under 3.5", calibrated=0.72, confidence=72, family="total_goals")

        assessment = assess_slip_review_policy(user_pick)

        self.assertTrue(assessment.supported)
        self.assertEqual(assessment.verdict, "supported")
        self.assertIsNone(assessment.suggested_alternative)
        self.assertFalse(hasattr(assessment, "tier"))
