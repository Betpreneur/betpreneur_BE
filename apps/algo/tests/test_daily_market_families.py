from unittest.mock import patch

from django.test import SimpleTestCase

from apps.algo.evaluators.registry import COUNT_MODEL_ENGINE, QUANTITATIVE, SCORE_MATRIX_ENGINE
from apps.algo.grindalgo import algo_runner
from apps.algo.models import Pick
from apps.algo.serializers import PickSerializer


class DailyMarketFamilyTests(SimpleTestCase):
    def _market(self, name):
        markets = algo_runner.serialize_fixture_markets(
            {name: 72},
            real_odds={},
            league="Test League",
            fixture_context={
                "country": "Testland",
                "league": "Test League",
                "home_team": "Alpha FC",
                "away_team": "Beta FC",
                "goal_model": {"expected_total": 2.6, "draw_confidence": 24},
            },
            home_recent_form={
                "games": 8,
                "wins": 5,
                "draws": 1,
                "losses": 2,
                "avg_scored": 1.8,
                "avg_conceded": 0.9,
                "over25_rate": 62.5,
                "btts_rate": 50.0,
                "clean_sheets": 3,
            },
            away_recent_form={
                "games": 8,
                "wins": 3,
                "draws": 2,
                "losses": 3,
                "avg_scored": 1.2,
                "avg_conceded": 1.4,
                "over25_rate": 50.0,
                "btts_rate": 62.5,
                "clean_sheets": 1,
            },
        )
        self.assertEqual(len(markets), 1)
        return markets[0]

    def test_legacy_result_market_is_tagged_with_family_metadata(self):
        market = self._market("Home Win")

        self.assertEqual(market["market_family"], "match_result")
        self.assertEqual(market["assessment_type"], QUANTITATIVE)
        self.assertEqual(market["evaluation_engine"], SCORE_MATRIX_ENGINE)
        self.assertEqual(market["daily_evaluation_route"]["engine"], SCORE_MATRIX_ENGINE)
        self.assertTrue(market["market_core_supported"])
        self.assertEqual(market["insights"]["market_family"], "match_result")
        self.assertEqual(market["insights"]["daily_evaluation_route"]["family"], "match_result")
        self.assertEqual(market["insights"]["market_identity"]["canonical"], "Home Win")

    def test_legacy_combo_and_clean_sheet_labels_are_overridden(self):
        combo = self._market("GG + Over 2.5")
        clean_sheet = self._market("Home CS")

        self.assertEqual(combo["market_family"], "total_btts")
        self.assertEqual(combo["market_identity"]["line"], "2.5")
        self.assertEqual(clean_sheet["market_family"], "clean_sheet")
        self.assertEqual(clean_sheet["market_identity"]["team"], "home")

    def test_dynamic_corner_market_is_tagged_as_count_model(self):
        market = self._market("Corners Under 12.5")

        self.assertEqual(market["market_family"], "corners_total")
        self.assertEqual(market["assessment_type"], QUANTITATIVE)
        self.assertEqual(market["evaluation_engine"], COUNT_MODEL_ENGINE)
        self.assertIn("team_corners", market["required_capabilities"])

    def test_market_insights_include_product_facing_evidence(self):
        market = self._market("Over 2.5")
        evidence = market["insights"]["evidence"]
        bettor_view = market["insights"]["bettor_view"]

        self.assertEqual(evidence["market_family"], "total_goals")
        self.assertEqual(evidence["confidence_label"], "Strong")
        self.assertEqual(evidence["bettor_view"]["confidence_score"], 72.0)
        self.assertEqual(bettor_view["confidence_label"], "Strong")
        self.assertEqual(bettor_view["risk_level"], "low")
        self.assertEqual(market["bettor_view"], bettor_view)
        self.assertIn("summary", evidence)
        self.assertIn("conclusion", evidence)
        self.assertTrue(evidence["positive"])
        self.assertIn("positive_evidence", market["insights"])
        self.assertIn("risk_evidence", market["insights"])
        self.assertIn("analysis_summary", market)
        self.assertIn("analysis_conclusion", market)

    def test_result_market_evidence_uses_team_names(self):
        market = self._market("Home Win")
        positive = " ".join(market["insights"]["evidence"]["positive"])

        self.assertIn("Alpha FC", positive)
        self.assertIn("Beta FC", positive)

    def test_publish_gate_requires_quantitative_evaluation_route(self):
        candidate = {
            "market": "Unsupported Custom Market",
            "conf": 95,
            "odds": 2.0,
            "ev": 0.2,
            "odds_is_real": True,
            "league": "Test League",
            "country": "Testland",
            "market_profile": {},
            "strategy_profile": {},
            "fixture_context": {},
            "risk_flags": [],
            "daily_evaluation_route": {
                "family": "unknown",
                "assessment_type": "none",
                "engine": "",
                "publishes_probability": False,
            },
        }

        self.assertFalse(algo_runner.passes_publish_gate(candidate))

    def test_deepseek_parser_accepts_structured_bettor_evidence(self):
        content = """
        {
          "picks": [
            {
              "index": 0,
              "reasoning": "Alpha FC have the stronger attacking profile and the odds still leave enough room for value. The main risk is that the away side has scored regularly.",
              "model_verdict": "Consider this pick, but keep the away scoring risk visible.",
              "summary": "The statistics give Alpha FC Win usable support.",
              "positive_evidence": ["Alpha average 1.8 goals scored.", "Beta concede 1.4 goals."],
              "risk_evidence": ["Beta have scored regularly enough to create risk."],
              "conclusion": "This is playable rather than a lock."
            }
          ]
        }
        """

        parsed = algo_runner._generated_explanations_from_content(content, expected_count=1)

        self.assertEqual(parsed[0]["summary"], "The statistics give Alpha FC Win usable support.")
        self.assertEqual(parsed[0]["positive_evidence"][0], "Alpha average 1.8 goals scored.")
        self.assertEqual(parsed[0]["risk_evidence"][0], "Beta have scored regularly enough to create risk.")

    def test_existing_deepseek_enrichment_updates_structured_bettor_fields(self):
        picks = [{
            "fixture": "Alpha FC vs Beta FC",
            "market": "Over 2.5",
            "reasoning": "Fallback reasoning",
            "model_verdict": "Fallback verdict",
            "insights": {
                "bettor_view": {"confidence_score": 72},
                "summary": "Fallback summary",
                "positive_evidence": ["Fallback support"],
                "risk_evidence": [],
                "conclusion": "Fallback conclusion",
            },
        }]
        generated = {
            0: {
                "reasoning": "Alpha FC and Beta FC both show enough scoring form to support goals.",
                "model_verdict": "Consider Over 2.5 as a supported but match-state sensitive pick.",
                "summary": "The statistics support Over 2.5.",
                "positive_evidence": ["Alpha FC average 1.8 goals.", "Beta FC average 1.4 goals."],
                "risk_evidence": ["One early red card could change the tempo."],
                "conclusion": "This is a playable goals pick.",
            }
        }

        with patch.dict(
            "os.environ",
            {"DEEPSEEK_API_KEY": "test-key", "ALGO_LLM_REASONING_ENABLED": "true"},
        ), patch("apps.algo.grindalgo.algo_runner._call_deepseek_pick_batch", return_value=generated):
            algo_runner.enhance_pick_explanations_with_llm(picks)

        self.assertEqual(
            picks[0]["reasoning"],
            "Alpha FC and Beta FC both show enough scoring form to support goals.",
        )
        self.assertEqual(picks[0]["insights"]["summary"], "The statistics support Over 2.5.")
        self.assertEqual(picks[0]["insights"]["bettor_view"]["conclusion"], "This is a playable goals pick.")
        self.assertEqual(picks[0]["insights"]["positive_evidence"][0], "Alpha FC average 1.8 goals.")

    def test_pick_serializer_exposes_additive_bettor_fields(self):
        pick = Pick(
            tier=Pick.Tier.VALUE_GEM,
            market="Over 2.5",
            confidence=72,
            odds=1.8,
            ev=0.12,
            insights={
                "summary": "The statistics support goals.",
                "conclusion": "This is playable.",
                "positive_evidence": ["The goal model projects about 2.8 goals."],
                "risk_evidence": ["The line is still close enough to require caution."],
                "bettor_view": {
                    "confidence_score": 72,
                    "confidence_label": "Strong",
                    "risk_level": "low",
                },
            },
        )

        data = PickSerializer(pick).data

        self.assertEqual(data["analysis_summary"], "The statistics support goals.")
        self.assertEqual(data["analysis_conclusion"], "This is playable.")
        self.assertEqual(data["bettor_view"]["confidence_label"], "Strong")
        self.assertEqual(data["positive_evidence"][0], "The goal model projects about 2.8 goals.")
        self.assertEqual(data["risk_evidence"][0], "The line is still close enough to require caution.")
