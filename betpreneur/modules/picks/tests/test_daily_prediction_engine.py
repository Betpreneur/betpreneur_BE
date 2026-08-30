from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from betpreneur.modules.picks.interface.views import _compact_games_payload
from betpreneur.modules.picks.models import AlgoFixture, AlgoRun, MarketPrediction, Pick
from betpreneur.modules.picks.services.runner_service import AlgoRunnerService
from betpreneur.modules.prediction.api import (
    CountModelOutput,
    FixtureFeatureSet,
    FixturePrediction,
    MarketProbability,
    PredictionDiagnostics,
    TeamStrengthSnapshot,
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

    def test_daily_prediction_real_odds_prefers_statpal_market_payload(self):
        service = AlgoRunnerService()
        fixture = {
            "match_id": "statpal:fixture-123",
            "aps_id": "api-fixture-123",
            "statpal_context": {
                "snapshots": {
                    "prematch_odds": {
                        "payload": {
                            "markets": [
                                {
                                    "name": "1x2",
                                    "bookmakers": [
                                        {
                                            "odds": [
                                                {"name": "Home", "value": 1.45},
                                                {"name": "Draw", "value": 4.1},
                                                {"name": "Away", "value": 8.5},
                                            ]
                                        },
                                        {
                                            "odds": [
                                                {"name": "Home", "value": 1.5},
                                                {"name": "Draw", "value": 4.0},
                                                {"name": "Away", "value": 9.0},
                                            ]
                                        },
                                    ],
                                },
                                {
                                    "name": "Double Chance",
                                    "bookmakers": [
                                        {
                                            "odds": [
                                                {"name": "Home/Draw", "value": 1.08},
                                                {"name": "Draw/Away", "value": 2.65},
                                            ]
                                        }
                                    ],
                                },
                                {
                                    "name": "Totals",
                                    "bookmakers": [
                                        {
                                            "totals": [
                                                {
                                                    "line": 2.5,
                                                    "odds": [
                                                        {"name": "Over", "value": 1.82},
                                                        {"name": "Under", "value": 2.02},
                                                    ],
                                                }
                                            ]
                                        }
                                    ],
                                },
                                {
                                    "name": "Cards",
                                    "bookmakers": [
                                        {
                                            "totals": [
                                                {
                                                    "line": 3.5,
                                                    "odds": [
                                                        {"name": "Over", "value": 1.72},
                                                        {"name": "Under", "value": 2.05},
                                                    ],
                                                }
                                            ]
                                        }
                                    ],
                                },
                                {
                                    "name": "Home Team Corners",
                                    "bookmakers": [
                                        {
                                            "totals": [
                                                {
                                                    "line": 4.5,
                                                    "odds": [
                                                        {"name": "Over", "value": 1.9},
                                                        {"name": "Under", "value": 1.86},
                                                    ],
                                                }
                                            ]
                                        }
                                    ],
                                },
                            ]
                        }
                    }
                }
            },
        }

        with patch(
            "betpreneur.modules.catalog.services.legacy_runner.get_api_football_odds",
            return_value={
                "aw": 14.0,
                "o25": 9.25,
                "_meta": {
                    "aw": {"source": "api_football", "best": 14.0},
                    "o25": {"source": "api_football", "best": 9.25},
                },
            },
        ):
            odds = service._daily_prediction_real_odds(fixture)

        self.assertEqual(odds["aw"], 9.0)
        self.assertEqual(odds["o25"], 1.82)
        self.assertEqual(odds["o35"], 9.25)
        self.assertEqual(odds["x2"], 2.65)
        self.assertEqual(odds["Cards Over 3.5"], 1.72)
        self.assertEqual(odds["Home Team Corners Over 4.5"], 1.9)
        self.assertEqual(odds["_meta"]["aw"]["source"], "statpal")
        self.assertEqual(odds["_meta"]["aw"]["bookmaker_count"], 2)
        self.assertEqual(odds["_meta"]["o25"]["source"], "statpal")

    def test_daily_prediction_markets_use_expanded_discovery_pool(self):
        service = AlgoRunnerService()

        markets = service._daily_prediction_markets(
            {
                "Home Team Corners Over 4.5": 1.88,
                "Cards Over 3.5": 1.72,
                "Unsupported Custom Market": 9.99,
                "_meta": {},
            }
        )

        self.assertIn("Home Team Over 1.5", markets)
        self.assertIn("BTTS No", markets)
        self.assertIn("Cards Over 3.5", markets)
        self.assertIn("Booking Points Over 45.5", markets)
        self.assertIn("Shots On Target Over 7.5", markets)
        self.assertIn("Home Team Corners Over 4.5", markets)
        self.assertNotIn("AH Home +0.5", markets)
        self.assertNotIn("AH Away +0.5", markets)
        self.assertNotIn("Unsupported Custom Market", markets)

    def test_prediction_scoring_hydration_restores_team_news(self):
        service = AlgoRunnerService()
        statpal_context = {
            "snapshots": {},
            "lineups": {
                "status": "projected",
                "home_formation": "4-3-3",
                "away_formation": "4-2-3-1",
            },
            "injuries_suspensions": {
                "home": {"to_miss_count": 2, "questionable_count": 1},
                "away": {"to_miss_count": 0, "questionable_count": 0},
            },
        }

        team_news = service._team_news_for_prediction_fixture(
            {"match_id": "statpal:fixture-123"},
            statpal_context,
        )

        self.assertTrue(team_news["available"])
        self.assertTrue(team_news["injuries_available"])
        self.assertTrue(team_news["lineups_available"])
        self.assertEqual(team_news["home"]["injuries"], 2)
        self.assertEqual(team_news["home"]["formation"], "4-3-3")
        self.assertIn("statpal_projected_lineups", team_news["flags"])

    def test_prediction_corner_profile_keeps_against_and_total_fields(self):
        service = AlgoRunnerService()
        prediction = FixturePrediction(
            fixture_id="fixture-123",
            fixture_name="Alpha FC vs Beta FC",
            features=FixtureFeatureSet(
                fixture_id="fixture-123",
                fixture_name="Alpha FC vs Beta FC",
                home_team=TeamStrengthSnapshot(team_id="home", team_name="Alpha FC"),
                away_team=TeamStrengthSnapshot(team_id="away", team_name="Beta FC"),
                features={
                    "home": {
                        "season_profile": {
                            "matches_played": 10,
                            "corners_for": 62,
                            "corners_against": 41,
                        }
                    },
                    "away": {
                        "season_profile": {
                            "matches_played": 10,
                            "corners_for": 38,
                            "corners_against": 57,
                        }
                    },
                },
            ),
            counts=CountModelOutput(
                expected_total_corners=10.2,
                expected_team_counts={"corners": {"home": 5.9, "away": 4.3}},
                diagnostics=PredictionDiagnostics(
                    data_quality="strong",
                    metadata={"sources": {"corners": ["team_season_profile"]}},
                ),
            ),
        )

        profile = service._prediction_corner_profile_payload(prediction)

        self.assertEqual(profile["expected_total"], 10.2)
        self.assertEqual(profile["home"]["avg_for"], 6.2)
        self.assertEqual(profile["home"]["avg_against"], 4.1)
        self.assertEqual(profile["home"]["avg_total"], 10.3)
        self.assertEqual(profile["home"]["opponent_avg_against"], 5.7)
        self.assertEqual(profile["away"]["avg_for"], 3.8)
        self.assertEqual(profile["away"]["avg_against"], 5.7)
        self.assertEqual(profile["away"]["avg_total"], 9.5)

    def test_prediction_recent_form_payload_does_not_double_divide_averages(self):
        service = AlgoRunnerService()
        prediction = SimpleNamespace(
            features=SimpleNamespace(
                features={
                    "home": {
                        "recent_form": {
                            "all": {
                                "10": {
                                    "matches": 10,
                                    "wins": 3,
                                    "draws": 4,
                                    "losses": 3,
                                    "goals_for": 1.7,
                                    "goals_against": 1.6,
                                    "goals_for_per_match": 1.7,
                                    "goals_against_per_match": 1.6,
                                    "form": ["D", "W", "L"],
                                    "scope": "all",
                                }
                            }
                        },
                        "season_profile": {"source": "statpal", "data_quality": "limited"},
                    }
                }
            )
        )

        payload = service._prediction_recent_form_payload(prediction, "home")

        self.assertEqual(payload["games"], 10)
        self.assertEqual(payload["avg_scored"], 1.7)
        self.assertEqual(payload["avg_conceded"], 1.6)

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
        no_edge_review = {
            "decision": "caution",
            "tier": "watchlist",
            "raw_confidence": 55,
            "final_confidence": 55,
            "consensus_score": 47.45,
            "disagreement_score": None,
            "reasons": ["below_exposure_score", "too_much_uncertainty", "tier_watchlist"],
            "reviewers": ["prediction_policy"],
        }
        watchlist_review = {
            **no_edge_review,
            "raw_confidence": 70,
            "final_confidence": 70,
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
            confidence=66,
            raw_confidence=34,
            odds=14.0,
            ev=2.339,
            eligible=True,
            insights={
                "summary": "Away Win has 34% calibrated model confidence.",
                "raw_probability": 0.34,
                "calibrated_probability": 0.34,
                "data_quality": "limited",
                "conclusion": "Away Win is modelled, but product policy needs stronger reliability support.",
                "positive_evidence": [
                    "Home win probability: 52%.",
                    "Away win probability: 24%.",
                ],
                "council_review": no_edge_review,
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
            confidence=62,
            raw_confidence=62,
            odds=1.93,
            ev=0.02,
            eligible=True,
            insights={
                "summary": "Under 3.5 has 62% calibrated model confidence.",
                "raw_probability": 0.62,
                "calibrated_probability": 0.62,
                "data_quality": "limited",
                "conclusion": "Under 3.5 is modelled, but product policy needs stronger reliability support.",
                "positive_evidence": [
                    "Projected total goals: 1.68.",
                    "Line 3.5 is above the model projection.",
                ],
                "council_review": watchlist_review,
            },
        )

        payload = _compact_games_payload(run.target_date)
        game = payload["games"][0]

        self.assertEqual(game["top_market"]["market"], "Under 3.5")
        self.assertIsNone(game["recommended_market"])
