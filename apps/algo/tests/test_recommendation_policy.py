from datetime import date
from decimal import Decimal

from django.test import TestCase, override_settings

from apps.algo.models import AlgoRun, MarketPrediction, Pick
from apps.algo.council import council_review
from apps.algo.recommendation_policy import assess_recommendation
from apps.algo.serializers import PickSerializer
from apps.algo.services import AlgoRunnerService
from apps.algo.views import _game_summary_from_fixture


STRICT_SETTINGS = {
    "ALGO_MAX_DAILY_PICKS": "15",
    "ALGO_TOP_PICK_MIN_KICKOFF": "09:00",
    "ALGO_PUBLISH_MIN_CONFIDENCE": "70",
    "ALGO_PUBLISH_MIN_EV": "0.03",
    "ALGO_PUBLISH_WILD_CARDS": "False",
    "ALGO_PUBLISH_DC12": "False",
    "ALGO_LEAGUE_MARKET_MIN_SAMPLE": "8",
    "ALGO_PROBATION_CONFIDENCE_EXTRA": "5",
    "ALGO_PROBATION_EV_EXTRA": "0.03",
    "ALGO_CONFIDENCE_BAND_MIN_SAMPLE": "20",
    "ALGO_CALIBRATION_CONFIDENCE_EXTRA": "3",
    "ALGO_CALIBRATION_EV_EXTRA": "0.02",
    "ALGO_MAX_DAILY_DC12_PICKS": "0",
    "ALGO_MAX_DAILY_SAME_MARKET_PICKS": "0",
}


@override_settings(GRIND_ALGO=STRICT_SETTINGS)
class RecommendationPolicyTests(TestCase):
    def setUp(self):
        self.run = AlgoRun.objects.create(target_date=date(2026, 6, 4))
        self.service = AlgoRunnerService()

    def prediction(self, *, match_id, confidence, ev, eligible=True, risk_flags=None, insights=None, kickoff="12:00 WAT"):
        return MarketPrediction.objects.create(
            run=self.run,
            match_date=self.run.target_date,
            fixture=f"Home {match_id} vs Away {match_id}",
            match_id=match_id,
            kickoff=kickoff,
            market="Under 3.5",
            confidence=confidence,
            raw_confidence=confidence,
            odds=Decimal("1.50"),
            ev=Decimal(str(ev)),
            odds_source="api_football",
            eligible=eligible,
            risk_flags=risk_flags or [],
            home_recent_form={
                "games": 8,
                "draws": 2,
                "avg_scored": 1.1,
                "avg_conceded": 1.0,
                "over25_rate": 37.5,
            },
            away_recent_form={
                "games": 8,
                "draws": 2,
                "avg_scored": 0.9,
                "avg_conceded": 1.1,
                "over25_rate": 37.5,
            },
            fixture_context={
                "country": "England",
                "league": "Test League",
                "goal_model": {
                    "expected_total": 2.4,
                    "draw_confidence": 24,
                },
            },
            insights=insights or {
                "league_trust": {
                    "status": "trusted",
                    "league_sample": 12,
                    "market_sample": 20,
                },
                "calibration_trust": {
                    "status": "trusted",
                    "sample": 40,
                    "hit_rate": 65.0,
                },
            },
        )

    def test_strict_gate_recommends_only_qualified_market(self):
        qualified = self.prediction(match_id="1", confidence=82, ev="0.090")
        thin_edge = self.prediction(match_id="2", confidence=78, ev="0.010")
        wild_card = self.prediction(match_id="3", confidence=68, ev="0.120")
        risky = self.prediction(
            match_id="4",
            confidence=85,
            ev="0.080",
            risk_flags=["goal_line_boundary"],
        )

        self.assertTrue(assess_recommendation(qualified)["recommended"])
        self.assertFalse(assess_recommendation(thin_edge)["recommended"])
        self.assertFalse(assess_recommendation(wild_card)["recommended"])
        self.assertFalse(assess_recommendation(risky)["recommended"])

        selected = self.service._select_prediction_ids(self.run)

        self.assertEqual(selected[Pick.Tier.BANKER], [qualified.id])
        self.assertEqual(selected[Pick.Tier.VALUE_GEM], [])
        self.assertEqual(selected[Pick.Tier.WILD_CARD], [])

    def test_top_picks_skip_games_before_morning_window(self):
        self.prediction(match_id="early", confidence=90, ev="0.150", kickoff="01:00 WAT")
        morning = self.prediction(match_id="morning", confidence=82, ev="0.090", kickoff="09:00 WAT")

        selected = self.service._select_prediction_ids(self.run)

        self.assertEqual(selected[Pick.Tier.BANKER], [morning.id])

    def test_previously_blocked_country_or_league_can_be_recommended(self):
        japan_candidate = {
            "confidence": 85,
            "ev": 0.09,
            "odds_source": "api_football",
            "country": "Japan",
            "league": "J1 League",
            "eligible": True,
            "risk_flags": [],
            "insights": {
                "league_trust": {"status": "trusted"},
                "calibration_trust": {"status": "trusted"},
            },
        }
        sweden_candidate = {
            **japan_candidate,
            "country": "Sweden",
            "league": "Allsvenskan",
        }

        country_assessment = assess_recommendation(japan_candidate)
        league_assessment = assess_recommendation(sweden_candidate)

        self.assertTrue(country_assessment["recommended"])
        self.assertNotIn("blocked_country", country_assessment["recommendation_reasons"])
        self.assertTrue(league_assessment["recommended"])
        self.assertNotIn("blocked_league", league_assessment["recommendation_reasons"])

    def test_dc12_publication_is_paused_by_default(self):
        candidate = {
            "market": "DC: 12",
            "confidence": 86,
            "ev": 0.10,
            "odds_source": "api_football",
            "eligible": True,
            "risk_flags": [],
            "insights": {
                "league_trust": {"status": "trusted"},
                "calibration_trust": {"status": "trusted"},
            },
        }

        assessment = assess_recommendation(candidate)

        self.assertFalse(assessment["recommended"])
        self.assertIn("dc12_publication_paused", assessment["recommendation_reasons"])

    def test_council_review_approves_strong_aligned_candidate(self):
        review = council_review({
            "market": "Under 3.5",
            "confidence": 84,
            "ev": 0.09,
            "odds_source": "api_football",
            "odds_meta": {"bookmaker_count": 4, "spread_pct": 6},
            "risk_flags": [],
            "home_recent_form": {
                "games": 8,
                "draws": 2,
                "avg_scored": 1.1,
                "avg_conceded": 1.0,
                "over25_rate": 37.5,
            },
            "away_recent_form": {
                "games": 8,
                "draws": 2,
                "avg_scored": 0.9,
                "avg_conceded": 1.1,
                "over25_rate": 37.5,
            },
            "fixture_context": {
                "goal_model": {
                    "expected_total": 2.4,
                    "draw_confidence": 24,
                }
            },
            "insights": {
                "league_trust": {
                    "status": "trusted",
                    "league_hit_rate": 63,
                    "league_roi": 6,
                    "market_hit_rate": 64,
                    "market_roi": 5,
                    "market_sample": 30,
                },
                "calibration_trust": {
                    "status": "trusted",
                    "hit_rate": 80,
                    "avg_confidence": 81,
                },
            },
        })

        self.assertEqual(review["decision"], "approve")
        self.assertGreaterEqual(review["consensus_score"], 75)
        self.assertIn(review["tier"], ["banker", "value_gem"])

    def test_market_fit_can_reject_dc12_despite_strong_history(self):
        review = council_review({
            "market": "DC: 12",
            "confidence": 84,
            "ev": 0.09,
            "odds_source": "api_football",
            "odds_meta": {"bookmaker_count": 4},
            "risk_flags": ["market_recovered"],
            "home_recent_form": {
                "games": 8,
                "draws": 4,
                "avg_scored": 0.8,
                "avg_conceded": 0.8,
            },
            "away_recent_form": {
                "games": 8,
                "draws": 4,
                "avg_scored": 0.7,
                "avg_conceded": 0.8,
            },
            "fixture_context": {
                "goal_model": {
                    "expected_total": 1.7,
                    "draw_confidence": 36,
                }
            },
            "insights": {
                "league_trust": {
                    "status": "trusted",
                    "league_hit_rate": 60,
                    "league_roi": 2,
                    "market_hit_rate": 79,
                    "market_roi": 3,
                    "market_sample": 30,
                },
                "calibration_trust": {
                    "status": "trusted",
                    "hit_rate": 78,
                    "avg_confidence": 80,
                },
            },
        })

        self.assertEqual(review["decision"], "reject")
        self.assertIn("weak_fixture_market_fit", review["reasons"])

    def test_h2h_draw_pressure_rejects_dc12(self):
        review = council_review({
            "market": "DC: 12",
            "confidence": 85,
            "ev": 0.09,
            "odds_source": "api_football",
            "odds_meta": {"bookmaker_count": 4},
            "risk_flags": [],
            "home_recent_form": {
                "games": 8,
                "draws": 1,
                "avg_scored": 1.4,
                "avg_conceded": 1.0,
            },
            "away_recent_form": {
                "games": 8,
                "draws": 1,
                "avg_scored": 1.3,
                "avg_conceded": 1.1,
            },
            "fixture_context": {
                "goal_model": {
                    "expected_total": 2.4,
                    "draw_confidence": 24,
                },
                "h2h": {
                    "games": 10,
                    "t1w": 1,
                    "draws": 4,
                    "t2w": 5,
                    "avg_goals": 1.4,
                },
            },
            "insights": {
                "league_trust": {
                    "status": "trusted",
                    "league_hit_rate": 66,
                    "league_roi": 5,
                    "market_hit_rate": 80,
                    "market_roi": 8,
                    "market_sample": 40,
                },
                "calibration_trust": {
                    "status": "trusted",
                    "hit_rate": 80,
                    "avg_confidence": 82,
                },
            },
        })

        self.assertEqual(review["decision"], "reject")
        self.assertIn("h2h_draw_pressure_against_dc12", review["reasons"])

    def test_exceptional_fixture_fit_can_lift_restricted_market(self):
        candidate = {
            "market": "Over 2.5",
            "confidence": 86,
            "ev": 0.12,
            "odds_source": "api_football",
            "odds_meta": {"bookmaker_count": 4},
            "eligible": False,
            "risk_flags": ["market_suppressed", "negative_market_roi", "low_market_hit_rate"],
            "home_recent_form": {
                "games": 8,
                "draws": 1,
                "avg_scored": 2.0,
                "avg_conceded": 1.4,
                "over25_rate": 75,
            },
            "away_recent_form": {
                "games": 8,
                "draws": 1,
                "avg_scored": 1.8,
                "avg_conceded": 1.5,
                "over25_rate": 70,
            },
            "fixture_context": {
                "goal_model": {
                    "expected_total": 3.1,
                    "draw_confidence": 18,
                }
            },
            "insights": {
                "league_trust": {
                    "status": "restricted",
                    "reasons": ["weak_overall_market_record"],
                    "league_hit_rate": 58,
                    "league_roi": 4,
                    "market_hit_rate": 44,
                    "market_roi": -9,
                    "market_sample": 40,
                },
                "calibration_trust": {
                    "status": "trusted",
                    "hit_rate": 78,
                    "avg_confidence": 80,
                },
            },
        }
        candidate["insights"]["council_review"] = council_review(candidate)

        assessment = assess_recommendation(candidate)

        self.assertTrue(assessment["recommended"])
        self.assertEqual(candidate["insights"]["council_review"]["decision"], "caution")
        self.assertIn("exceptional_fixture_market_fit", candidate["insights"]["council_review"]["reasons"])

    def test_recommendation_candidate_includes_council_review(self):
        prediction = self.prediction(match_id="5", confidence=82, ev="0.090")

        candidate = self.service._recommendation_candidate(prediction, {
            "markets": {
                "Under 3.5": {
                    "count": 25,
                    "hit_rate": 65,
                    "roi_flat": 6,
                }
            },
            "league_markets": {
                "": {}
            },
            "confidence_bands": {
                "80+": {
                    "count": 30,
                    "hit_rate": 78,
                    "roi_flat": 5,
                    "avg_confidence": 81,
                }
            },
        })

        self.assertIn("council_review", candidate["insights"])
        self.assertIn("reviewers", candidate["insights"]["council_review"])

    def test_council_final_confidence_sets_tier(self):
        cautious = self.prediction(match_id="6", confidence=82, ev="0.100")
        selected = self.service._select_prediction_ids(self.run_with_performance({
            "markets": {
                "Under 3.5": {
                    "count": 5,
                    "hit_rate": 58,
                    "roi_flat": 2,
                }
            },
            "league_markets": {
                "::Under 3.5": {
                    "count": 5,
                    "hit_rate": 58,
                    "roi_flat": 2,
                }
            },
            "confidence_bands": {
                "80+": {
                    "count": 30,
                    "hit_rate": 80,
                    "roi_flat": 5,
                    "avg_confidence": 81,
                }
            },
        }))

        self.assertEqual(selected[Pick.Tier.BANKER], [])
        self.assertEqual(selected[Pick.Tier.VALUE_GEM], [cautious.id])

        candidate = self.service._recommendation_candidate(cautious, self.run.result["performance_profile"])
        review = candidate["insights"]["council_review"]
        self.assertEqual(review["decision"], "caution")
        self.assertEqual(review["tier"], "value_gem")
        self.assertLess(review["final_confidence"], 80)

    def test_pick_serializer_uses_council_tier_not_raw_confidence(self):
        pick = Pick.objects.create(
            run=self.run,
            match_date=self.run.target_date,
            fixture="Home 8 vs Away 8",
            home_team="Home 8",
            away_team="Away 8",
            league="Test League",
            kickoff="12:00 WAT",
            match_id="8",
            tier=Pick.Tier.VALUE_GEM,
            market="Under 3.5",
            meaning="3 or fewer total goals",
            confidence=82,
            odds=Decimal("1.50"),
            ev=Decimal("0.100"),
            stake=Decimal("1000.00"),
            source="APS",
            insights={
                "council_review": {
                    "decision": "caution",
                    "tier": Pick.Tier.VALUE_GEM,
                    "raw_confidence": 82,
                    "final_confidence": 78,
                    "consensus_score": 75,
                    "disagreement_score": 26,
                    "reasons": ["league_market_probation"],
                    "reviewers": [],
                }
            },
        )

        data = PickSerializer(pick).data

        self.assertEqual(data["tier"], Pick.Tier.VALUE_GEM)
        self.assertEqual(data["confidence"], 82)
        self.assertEqual(data["final_confidence"], 78)
        self.assertEqual(data["council_review"]["decision"], "caution")

    def test_council_reject_blocks_otherwise_recommended_pick(self):
        self.prediction(match_id="7", confidence=90, ev="0.200")
        selected = self.service._select_prediction_ids(self.run_with_performance({
            "markets": {
                "Under 3.5": {
                    "count": 5,
                    "hit_rate": 58,
                    "roi_flat": -20,
                }
            },
            "league_markets": {
                "::Under 3.5": {
                    "count": 5,
                    "hit_rate": 58,
                    "roi_flat": -20,
                }
            },
            "confidence_bands": {
                "80+": {
                    "count": 30,
                    "hit_rate": 80,
                    "roi_flat": 5,
                    "avg_confidence": 81,
                }
            },
        }))

        self.assertEqual(selected[Pick.Tier.BANKER], [])
        self.assertEqual(selected[Pick.Tier.VALUE_GEM], [])
        self.assertEqual(selected[Pick.Tier.WILD_CARD], [])

    def run_with_performance(self, performance):
        self.run.result = {"performance_profile": performance}
        self.run.save(update_fields=["result"])
        return self.run

    def test_games_keep_best_market_but_show_no_recommendation(self):
        fixture = {
            "fixture": "Home vs Away",
            "match_id": "10",
            "markets": [
                {
                    "market": "Under 3.5",
                    "meaning": "3 or fewer total goals",
                    "confidence": 76,
                    "raw_confidence": 76,
                    "odds": 1.50,
                    "ev": 0.01,
                    "odds_source": "api_football",
                    "eligible": True,
                    "risk_flags": [],
                }
            ],
        }

        game = _game_summary_from_fixture(fixture, {}, request=None)

        self.assertEqual(game["best_market"]["market"], "Under 3.5")
        self.assertIsNone(game["recommended_market"])
        self.assertEqual(game["recommendation_status"], "watchlist")

    def test_probation_league_market_needs_extra_edge(self):
        fixture = {
            "fixture": "Home vs Away",
            "match_id": "11",
            "markets": [
                {
                    "market": "Over 1.5",
                    "meaning": "2 or more total goals",
                    "confidence": 72,
                    "raw_confidence": 72,
                    "odds": 1.55,
                    "ev": 0.05,
                    "odds_source": "api_football",
                    "eligible": True,
                    "risk_flags": [],
                    "insights": {
                        "league_trust": {
                            "status": "probation",
                            "reasons": ["limited_league_market_sample"],
                        },
                        "calibration_trust": {"status": "trusted"},
                    },
                },
                {
                    "market": "Under 3.5",
                    "meaning": "3 or fewer total goals",
                    "confidence": 80,
                    "raw_confidence": 80,
                    "odds": 1.50,
                    "ev": 0.08,
                    "odds_source": "api_football",
                    "eligible": True,
                    "risk_flags": [],
                    "insights": {
                        "league_trust": {
                            "status": "probation",
                            "reasons": ["limited_league_market_sample"],
                        },
                        "calibration_trust": {"status": "trusted"},
                    },
                },
            ],
        }

        game = _game_summary_from_fixture(fixture, {}, request=None)

        self.assertEqual(game["best_market"]["market"], "Under 3.5")
        self.assertEqual(game["recommended_market"]["market"], "Under 3.5")
        self.assertEqual(game["recommended_market"]["recommendation_status"], "strong")

    def test_all_games_recommendation_is_blocked_by_council_reject(self):
        fixture = {
            "fixture": "Home vs Away",
            "match_id": "12",
            "markets": [
                {
                    "market": "Under 3.5",
                    "meaning": "3 or fewer total goals",
                    "confidence": 84,
                    "raw_confidence": 84,
                    "odds": 1.55,
                    "ev": 0.08,
                    "odds_source": "api_football",
                    "eligible": True,
                    "risk_flags": [],
                    "insights": {
                        "league_trust": {"status": "trusted"},
                        "calibration_trust": {"status": "trusted"},
                        "council_review": {
                            "decision": "reject",
                            "tier": "",
                            "raw_confidence": 84,
                            "final_confidence": 58,
                            "consensus_score": 48,
                            "disagreement_score": 42,
                            "reasons": ["market_history_veto"],
                        },
                    },
                }
            ],
        }

        game = _game_summary_from_fixture(fixture, {}, request=None)

        self.assertEqual(game["best_market"]["market"], "Under 3.5")
        self.assertIsNone(game["recommended_market"])
        self.assertFalse(game["best_market"]["recommended"])
        self.assertIn("council_reject", game["best_market"]["recommendation_reasons"])

    def test_all_games_recommendation_can_pass_with_council_approval(self):
        fixture = {
            "fixture": "Home vs Away",
            "match_id": "13",
            "markets": [
                {
                    "market": "Under 3.5",
                    "meaning": "3 or fewer total goals",
                    "confidence": 84,
                    "raw_confidence": 84,
                    "odds": 1.55,
                    "ev": 0.08,
                    "odds_source": "api_football",
                    "eligible": True,
                    "risk_flags": [],
                    "insights": {
                        "league_trust": {"status": "trusted"},
                        "calibration_trust": {"status": "trusted"},
                        "council_review": {
                            "decision": "approve",
                            "tier": "banker",
                            "raw_confidence": 84,
                            "final_confidence": 82,
                            "consensus_score": 80,
                            "disagreement_score": 12,
                            "reasons": [],
                        },
                    },
                }
            ],
        }

        game = _game_summary_from_fixture(fixture, {}, request=None)

        self.assertEqual(game["recommended_market"]["market"], "Under 3.5")
        self.assertEqual(game["recommended_market"]["final_confidence"], 82)
        self.assertEqual(game["recommended_market"]["council_review"]["decision"], "approve")

    def test_weak_confidence_band_blocks_publication(self):
        candidate = {
            "confidence": 72,
            "ev": 0.06,
            "odds_source": "api_football",
            "eligible": True,
            "risk_flags": [],
            "insights": {
                "league_trust": {"status": "trusted"},
                "calibration_trust": {
                    "status": "restricted",
                    "reasons": ["weak_confidence_band_record"],
                    "sample": 30,
                    "hit_rate": 44.0,
                },
            },
        }

        assessment = assess_recommendation(candidate)

        self.assertFalse(assessment["recommended"])
        self.assertIn("weak_confidence_band_record", assessment["recommendation_reasons"])
