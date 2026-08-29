from datetime import date
from unittest.mock import patch

from django.test import TestCase

from betpreneur.modules.picks.interface.views import _compact_games_payload
from betpreneur.modules.picks.models import AlgoFixture, AlgoRun, MarketPrediction, Pick
from betpreneur.modules.picks.services.runner_service import AlgoRunnerService
from betpreneur.modules.prediction.api import (
    FixturePrediction,
    MarketProbability,
    PredictionDiagnostics,
)


class DailyPredictionEngineTests(TestCase):
    def _market_probability(self, market="Over 2.5"):
        return MarketProbability(
            fixture_id="fixture-123",
            market=market,
            raw_probability=0.78,
            calibrated_probability=0.75,
            confidence_score=75,
            model="poisson_goals",
            data_quality="strong",
            model_sources=("prediction.poisson", "prediction.calibration"),
            explanation_facts=(
                "Projected total goals: 3.20.",
                "Line 2.5 is below the model projection.",
            ),
            diagnostics=PredictionDiagnostics(
                data_quality="strong",
                model_version="test-v1",
                model_sources=("prediction.poisson",),
                metadata={
                    "sample_count": 300,
                    "market_family": "total_goals",
                    "market_support_level": "full",
                },
            ),
        )

    def test_daily_fixture_scoring_uses_prediction_api_and_persists_policy_context(self):
        run = AlgoRun.objects.create(target_date=date(2026, 8, 28), status=AlgoRun.Status.RUNNING)
        fixture = AlgoFixture.objects.create(
            run=run,
            match_date=run.target_date,
            fixture="Alpha FC vs Beta FC",
            home_team="Alpha FC",
            away_team="Beta FC",
            match_id="fixture-123",
            source_payload={
                "match_id": "fixture-123",
                "fixture": "Alpha FC vs Beta FC",
                "home_team": "Alpha FC",
                "away_team": "Beta FC",
                "league": "Premier League",
                "country": "England",
                "statpal_context": {
                    "prematch_odds": {
                        "over25_odds": 1.8,
                    }
                },
            },
        )
        probability = self._market_probability()
        prediction = FixturePrediction(
            fixture_id="fixture-123",
            fixture_name="Alpha FC vs Beta FC",
            market_probabilities=(probability,),
            diagnostics=PredictionDiagnostics(data_quality="strong"),
        )
        service = AlgoRunnerService()

        with (
            patch(
                "betpreneur.modules.picks.services.runner_service.predict_fixture",
                return_value=prediction,
            ) as predict,
            patch(
                "betpreneur.modules.catalog.services.legacy_runner.score_aps_fixture_for_pipeline"
            ) as legacy_score,
            patch.object(service, "_hydrate_statpal_scoring_context", side_effect=lambda payload: payload),
            patch.object(service, "_enrich_fixture_statpal_diagnostics", side_effect=lambda payload: payload),
            patch.object(
                service,
                "_write_slip_review_market_cache",
                return_value={"enabled": True, "cached": 1},
            ),
        ):
            result = service.score_fixture_for_run(fixture.id)

        self.assertEqual(result["status"], "scored")
        predict.assert_called_once()
        legacy_score.assert_not_called()

        row = MarketPrediction.objects.get(run=run, match_id="fixture-123", market="Over 2.5")
        self.assertEqual(row.confidence, 75)
        self.assertEqual(float(row.odds), 1.8)
        self.assertEqual(row.odds_source, "statpal")
        self.assertTrue(row.eligible)
        self.assertEqual(row.insights["prediction_engine"], "prediction.api.predict_fixture")
        self.assertIn("value_assessment", row.insights)
        self.assertIn("recommendation_score", row.insights)
        self.assertTrue(row.insights["top_picks_policy"]["publishable"])

    def test_top_picks_selection_requires_publishable_product_policy(self):
        run = AlgoRun.objects.create(target_date=date(2026, 8, 28), status=AlgoRun.Status.RUNNING)
        blocked = MarketPrediction.objects.create(
            run=run,
            match_date=run.target_date,
            fixture="Alpha FC vs Beta FC",
            match_id="fixture-1",
            market="Over 2.5",
            confidence=82,
            raw_confidence=82,
            odds=1.8,
            ev=0.2,
            eligible=True,
            insights={
                "market_family": "total_goals",
                "top_picks_policy": {"publishable": False, "tier": Pick.Tier.BANKER},
            },
        )
        allowed = MarketPrediction.objects.create(
            run=run,
            match_date=run.target_date,
            fixture="Gamma FC vs Delta FC",
            match_id="fixture-2",
            market="Over 2.5",
            confidence=75,
            raw_confidence=75,
            odds=1.9,
            ev=0.25,
            eligible=True,
            insights={
                "market_family": "total_goals",
                "top_picks_policy": {"publishable": True, "tier": Pick.Tier.VALUE_GEM},
            },
        )
        service = AlgoRunnerService()

        with patch.object(
            service,
            "_recommendation_candidate",
            return_value={
                "insights": {
                    "council_review": {
                        "decision": "approve",
                        "tier": Pick.Tier.VALUE_GEM,
                    }
                }
            },
        ):
            selected_ids = service._select_prediction_ids(run)

        self.assertIn(allowed.id, selected_ids)
        self.assertNotIn(blocked.id, selected_ids)

    def test_compact_games_ranks_after_public_policy_gate(self):
        run = AlgoRun.objects.create(target_date=date(2026, 8, 29), status=AlgoRun.Status.SUCCESS)
        AlgoFixture.objects.create(
            run=run,
            match_date=run.target_date,
            fixture="Celtic vs Falkirk",
            home_team="Celtic",
            away_team="Falkirk",
            league="Premier League",
            country="scotland",
            kickoff="14:00",
            match_id="statpal:2026082930121",
            market_count=2,
            markets_70_plus=2,
            markets_65_plus=2,
        )
        shared_watchlist = {
            "decision": "caution",
            "tier": "watchlist",
            "raw_confidence": 70,
            "final_confidence": 70,
            "consensus_score": 47.45,
            "disagreement_score": None,
            "reasons": ["below_exposure_score", "too_much_uncertainty", "tier_watchlist"],
            "reviewers": ["prediction_policy"],
        }
        MarketPrediction.objects.create(
            run=run,
            match_date=run.target_date,
            fixture="Celtic vs Falkirk",
            home_team="Celtic",
            away_team="Falkirk",
            league="Premier League",
            match_id="statpal:2026082930121",
            market="Away Win",
            meaning="Away team to win",
            confidence=70,
            raw_confidence=70,
            odds=14.0,
            ev=2.339,
            eligible=True,
            insights={
                "summary": "Away Win has 70% calibrated model confidence.",
                "conclusion": "Away Win is modelled, but product policy needs stronger reliability support.",
                "positive_evidence": [
                    "Home win probability: 52%.",
                    "Away win probability: 24%.",
                ],
                "council_review": shared_watchlist,
            },
        )
        MarketPrediction.objects.create(
            run=run,
            match_date=run.target_date,
            fixture="Celtic vs Falkirk",
            home_team="Celtic",
            away_team="Falkirk",
            league="Premier League",
            match_id="statpal:2026082930121",
            market="Under 3.5",
            meaning="3 or fewer total goals",
            confidence=70,
            raw_confidence=91,
            odds=1.93,
            ev=0.755,
            eligible=True,
            insights={
                "summary": "Under 3.5 has 70% calibrated model confidence.",
                "conclusion": "Under 3.5 is modelled, but product policy needs stronger reliability support.",
                "positive_evidence": [
                    "Projected total goals: 1.68.",
                    "Line 3.5 is above the model projection.",
                ],
                "council_review": shared_watchlist,
            },
        )

        payload = _compact_games_payload(run.target_date)
        game = payload["games"][0]

        self.assertEqual(game["top_market"]["market"], "Under 3.5")
        self.assertIsNone(game["recommended_market"])
