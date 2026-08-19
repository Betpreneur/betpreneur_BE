from unittest.mock import patch
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.algo.models import SlipReview, SlipReviewEvent, SlipReviewStreamToken, SlipSelection
from apps.algo.ticket_risk import SCORE_BANDS, Calibration
from apps.algo.views import (
    _build_bettor_public_payload,
    _has_statpal_hydration_identity,
    _clean_deepseek_recommendation_why,
    _enhance_bettor_public_with_deepseek,
    _manual_review_summary,
    _matched_fixture_with_statpal,
    _public_confidence_label,
    _public_price_check_from_card,
    _public_verdict_object,
    _replacement_market_for_slip,
    recover_stale_slip_reviews,
    _should_skip_core_on_demand,
    _compact_slip_review_list_payload,
    _slip_review_payload,
    _slip_leg_analysis_cache_key,
    _streamed_slip_review_game_payload,
    _stream_ticket_hash,
    _ticket_killers_message,
    _without_blocked_replacement_recommendation,
)
from apps.algo.market_taxonomy import describe_market


def _sample_replace_result():
    return {
        "match": "Norway vs England",
        "submitted_market": "Away Win",
        "status": "analysed",
        "verdict": "replace",
        "message": "Away Win is too risky compared with the suggested alternative.",
        "matched_fixture": {
            "match_id": "1581037",
            "fixture": "Norway vs England",
            "match_score": 100,
        },
        "selected_market": {
            "market": "Away Win",
            "meaning": "Away team to win",
            "confidence": 42,
            "final_confidence": 38,
            "advisory_score": 28,
            "advisory_status": "avoid",
            "risk_flags": ["weak_fixture_market_fit"],
            "advisory_evidence": {
                "historical_accuracy": 41.2,
                "sample_size": 713,
                "statpal": {
                    "odds_value": {
                        "offered_odds": 1.80,
                        "statpal_reference_odds": 2.00,
                        "value_edge_pct": -10.0,
                        "matched_market": "1X2",
                        "matched_outcome": "Away",
                        "bookmaker": "10Bet",
                    }
                },
            },
        },
        "replacement_market": {
            "market": "Over 1.5",
            "meaning": "2 or more total goals",
            "confidence": 75,
            "final_confidence": 72,
            "odds": 1.26,
            "odds_source": "api_football",
            "advisory_score": 70,
            "advisory_status": "playable",
            "advisory_evidence": {
                "historical_accuracy": 80.2,
                "sample_size": 713,
                "similar_market_roi": 8.4,
            },
            "replacement_scope": "broad_fallback",
        },
        "market_taxonomy": {
            "recognized": True,
            "core_supported": True,
        },
        "market_capability": {
            "support_level": "full",
            "data_quality": "strong",
            "confidence_cap": 88,
            "warnings": [],
        },
        "statpal_context": {
            "available": True,
            "hydration_source": "statpal_daily_cache",
            "snapshot_cache_status": "hit",
            "market_snapshot_coverage": {
                "required": ["lineups"],
                "available": ["lineups"],
                "fresh": ["lineups"],
                "missing": [],
                "coverage_percent": 100.0,
            },
            "snapshots": {
                "lineups": {"summary": {"home_confirmed": True}},
            },
        },
        "statpal_advisory": {
            "available": True,
            "message": "Lineup context supports caution.",
        },
        "statpal_refresh": {
            "api_usage": {
                "provider": "statpal",
                "attempted_calls": 2,
                "successful_calls": 1,
                "failed_calls": 1,
                "skipped_by_cache": 3,
                "skipped_without_call": 0,
                "snapshot_types_attempted": ["lineups", "predictions"],
                "snapshot_types_refreshed": ["lineups"],
                "snapshot_types_failed": ["predictions"],
            }
        },
    }


def _sample_market_not_found_with_replacement():
    result = _sample_replace_result()
    result["submitted_market"] = "Cards Over 3.5"
    result["status"] = "market_not_found"
    result["selected_market"] = {
        "market": "Cards Over 3.5",
        "advisory_score": 50,
        "advisory_status": "avoid",
        "advisory_evidence": {},
    }
    result["market_taxonomy"] = {
        "recognized": True,
        "core_supported": False,
    }
    return result


def _sample_market_not_found_without_replacement_but_scored():
    result = _sample_replace_result()
    result["submitted_market"] = "Cards Over 3.5"
    result["status"] = "market_not_found"
    result["verdict"] = "remove"
    result["message"] = "Cards Over 3.5 does not show enough edge."
    result["replacement_market"] = None
    result["selected_market"] = {
        "market": "Cards Over 3.5",
        "advisory_score": 44,
        "advisory_status": "avoid",
        "advisory_evidence": {},
    }
    result["market_taxonomy"] = {
        "recognized": True,
        "core_supported": False,
    }
    return result


def _sample_market_not_found_without_score():
    result = _sample_replace_result()
    result["submitted_market"] = "1H Over 0.5"
    result["status"] = "market_not_found"
    result["verdict"] = "not_assessed"
    result["message"] = "There is not enough data on this market to assess it."
    result["replacement_market"] = None
    result["selected_market"] = {
        "market": "1H Over 0.5",
        "advisory_score": None,
        "advisory_status": "unknown",
        "advisory_evidence": {},
        "statpal_advisory": {"available": False, "assessment_type": "quantitative_model"},
    }
    result["market_taxonomy"] = {
        "recognized": True,
        "core_supported": False,
        "family": "total_goals",
    }
    return result


class PublicPriceCheckTests(SimpleTestCase):
    def test_public_price_check_summarizes_positive_edge(self):
        price_check = _public_price_check_from_card(
            {
                "evidence": {
                    "statpal": {
                        "odds_value": {
                            "offered_odds": 2.20,
                            "statpal_reference_odds": 2.00,
                            "statpal_reference_min_odds": 1.90,
                            "statpal_reference_max_odds": 2.20,
                            "statpal_reference_bookmaker_count": 3,
                            "statpal_reference_spread_pct": 15.0,
                            "value_edge_pct": 10.0,
                            "reference_method": "median_bookmaker_odds",
                            "reference_reliability": "solid",
                            "matched_market": "Over/Under",
                            "matched_outcome": "Over",
                            "bookmaker": "10Bet",
                        }
                    }
                }
            }
        )

        self.assertEqual(price_check["status"], "positive_edge")
        self.assertEqual(price_check["edge_percent"], 10.0)
        self.assertEqual(price_check["reference_bookmaker_count"], 3)
        self.assertEqual(price_check["reference_spread_percent"], 15.0)
        self.assertEqual(price_check["reference_method"], "median_bookmaker_odds")
        self.assertEqual(price_check["reference_reliability"], "solid")
        self.assertIn("better than the StatPal reference", price_check["message"])

    def test_public_price_check_warns_on_volatile_reference(self):
        price_check = _public_price_check_from_card(
            {
                "evidence": {
                    "odds_value": {
                        "offered_odds": 2.20,
                        "statpal_reference_odds": 2.00,
                        "statpal_reference_min_odds": 1.50,
                        "statpal_reference_max_odds": 2.70,
                        "statpal_reference_bookmaker_count": 3,
                        "statpal_reference_spread_pct": 60.0,
                        "value_edge_pct": 10.0,
                        "reference_method": "median_bookmaker_odds",
                        "reference_reliability": "volatile",
                    }
                }
            }
        )

        self.assertEqual(price_check["status"], "positive_edge")
        self.assertEqual(price_check["reference_reliability"], "volatile")
        self.assertIn("edge is unreliable", price_check["message"])

    def test_public_price_check_summarizes_short_price(self):
        price_check = _public_price_check_from_card(
            {
                "evidence": {
                    "odds_value": {
                        "offered_odds": 1.80,
                        "statpal_reference_odds": 2.00,
                        "value_edge_pct": -10.0,
                    }
                }
            }
        )

        self.assertEqual(price_check["status"], "short_price")
        self.assertEqual(price_check["edge_percent"], -10.0)
        self.assertIn("shorter than the StatPal reference", price_check["message"])


class MatchedFixtureStatPalPayloadTests(SimpleTestCase):
    def test_matched_fixture_keeps_statpal_ids_for_debug_and_hydration(self):
        payload = _matched_fixture_with_statpal(
            {"match_id": "1558588", "home_team": "Anderlecht", "away_team": "RAAL La Louviere"},
            {"fixture": "Anderlecht vs RAAL La Louviere"},
            {
                "match_id": "statpal:202608091558588",
                "provider_match_id": "202608091558588",
                "provider_competition_id": "3038",
                "home_team_id": "2340001",
                "away_team_id": "2340002",
                "home_team": "Anderlecht",
                "away_team": "RAAL La Louviere",
            },
        )

        self.assertEqual(payload["match_id"], "1558588")
        self.assertEqual(payload["statpal_provider_match_id"], "202608091558588")
        self.assertEqual(payload["statpal_provider_competition_id"], "3038")
        self.assertEqual(payload["statpal_home_team_id"], "2340001")
        self.assertEqual(payload["statpal_away_team_id"], "2340002")

    def test_statpal_hydration_identity_detects_provider_or_team_ids(self):
        self.assertTrue(_has_statpal_hydration_identity({"match_id": "statpal:123"}))
        self.assertTrue(_has_statpal_hydration_identity({"statpal_home_team_id": "home-1"}))
        self.assertTrue(_has_statpal_hydration_identity({}, {"home_team_id": "home-1"}))
        self.assertTrue(_has_statpal_hydration_identity({}, {"provider_match_id": "202608091"}))
        self.assertTrue(_has_statpal_hydration_identity({}, {}, {"provider": "statpal", "provider_event_id": "202608091"}))

    def test_statpal_hydration_identity_rejects_plain_api_fixture_only(self):
        self.assertFalse(_has_statpal_hydration_identity({"match_id": "1494240", "home_team": "A", "away_team": "B"}))
        self.assertFalse(_has_statpal_hydration_identity({"match_id": "1494240", "home_team_id": "api-home"}))

    def test_skip_core_requires_existing_game_or_statpal_identity_for_any_skip_market(self):
        for market in ["Home Win", "Cards Over 3.5", "Florian Wirtz To Score"]:
            descriptor = describe_market(market)

            self.assertFalse(
                _should_skip_core_on_demand(
                    descriptor,
                    candidate={"match_id": "1494240", "home_team": "A", "away_team": "B"},
                ),
                market,
            )
            self.assertTrue(_should_skip_core_on_demand(descriptor, game={"markets": []}), market)
            self.assertTrue(
                _should_skip_core_on_demand(
                    descriptor,
                    candidate={"match_id": "statpal:1494240", "home_team": "A", "away_team": "B"},
                ),
                market,
            )


class SlipReviewPublicContractTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        bands = {name: {"wins": 0, "settled": 0, "hit_rate_percent": None} for name, _, _ in SCORE_BANDS}
        self._calibration_patch = patch(
            "apps.algo.views.ticket_risk_service.calibration",
            return_value=Calibration(basis="prior", sample_size=0, bands=bands),
        )
        self._calibration_patch.start()

    def tearDown(self):
        self._calibration_patch.stop()
        super().tearDown()

    def test_public_review_contract_is_frontend_friendly(self):
        summary = _manual_review_summary([_sample_replace_result()])
        public = summary["public"]
        selection = public["selections"][0]

        self.assertEqual(summary["api_usage"]["attempted_calls"], 2)
        self.assertEqual(summary["api_usage"]["successful_calls"], 1)
        self.assertEqual(summary["api_usage"]["failed_calls"], 1)
        self.assertEqual(summary["api_usage"]["skipped_by_cache"], 3)
        self.assertEqual(public["contract_version"], "match_checker_public_v2")
        self.assertEqual(public["response_mode"], "public")
        self.assertIn("ticket_health", public)
        self.assertIn("ticket_summary", public)
        self.assertIn("comparison", public)
        self.assertIn("ticket_impact", public)
        self.assertNotIn("api_usage", public)
        self.assertNotIn("recommended_changes", public)
        self.assertEqual(public["recommended_change_ids"], ["1581037"])
        self.assertEqual(public["ticket_summary"]["total_legs"], 1)
        self.assertIn("pick_breakdown", public["ticket_summary"])
        self.assertIn("overall_confidence_score", public["ticket_summary"]["user_ticket"])
        self.assertIn("overall_confidence_score", public["ticket_summary"]["ai_ticket"])
        self.assertIn("confidence_score_change", public["ticket_summary"]["improvement"])

        self.assertEqual(selection["verdict"]["code"], "replace")
        self.assertEqual(selection["verdict"]["label"], "Replace")
        self.assertEqual(selection["your_pick"]["market"], "Away Win")
        self.assertEqual(selection["user_pick"]["market"], "Away Win")
        self.assertIn("confidence_score", selection["user_pick"])
        self.assertIn("confidence_label", selection["user_pick"])
        self.assertIn("data_confidence_score", selection["user_pick"])
        self.assertEqual(selection["user_pick"]["verdict"], "replace")
        self.assertIn("verdict_label", selection["user_pick"])
        self.assertIn("message", selection["user_pick"])
        self.assertIn("evidence", selection)
        self.assertTrue(selection["evidence"])
        self.assertIn("our_view", selection)
        self.assertEqual(selection["recommendation"]["action"], "replace")
        self.assertEqual(selection["recommendation"]["market"], "Over 1.5")
        self.assertIn("confidence", selection["recommendation"])
        self.assertIn("why", selection["recommendation"])
        self.assertEqual(selection["your_pick"]["support_level"], "full")
        self.assertEqual(selection["your_pick"]["data_quality"], "strong")
        self.assertEqual(selection["your_pick"]["confidence_cap"], 88)
        self.assertNotIn("taxonomy", selection["your_pick"])
        self.assertNotIn("statpal_advisory", selection["your_pick"])
        self.assertEqual(selection["price_check"]["status"], "short_price")
        self.assertEqual(selection["price_check"]["edge_percent"], -10.0)
        self.assertEqual(selection["price_check"]["reference_odds"], 2.0)
        self.assertIn("price_short", selection["reason_codes"])
        self.assertFalse(any("StatPal reference" in reason for reason in selection["why"]))
        self.assertIn("model_probability_percent", selection["your_pick"])
        self.assertIn("decision_score", selection["your_pick"])
        self.assertIn("data_confidence_score", selection["your_pick"])
        self.assertIn("confidence_label", selection["your_pick"])
        self.assertNotEqual(selection["your_pick"]["model_probability_percent"], selection["your_pick"]["decision_score"])
        self.assertIn("value_rating", selection["your_pick"])
        self.assertIn("market_consensus", selection)
        self.assertIn("model_fair_odds", public["comparison"]["original"])
        self.assertIn("repaired", public["comparison"])
        self.assertEqual(selection["ai_pick"]["market"], "Over 1.5")
        self.assertTrue(selection["ai_pick"]["available"])
        self.assertIn("confidence_score", selection["ai_pick"])
        self.assertIn("confidence_label", selection["ai_pick"])
        self.assertIn("data_confidence_score", selection["ai_pick"])
        self.assertEqual(selection["ai_pick"]["replacement_scope"], "broad_fallback")
        self.assertIn("selection_lift_points", selection["ai_pick"])
        self.assertIn("confidence_gain", selection["comparison"])
        self.assertIn("ticket_success_lift", selection["comparison"])
        self.assertIn(
            selection["ai_pick"]["recommendation_strength"],
            {"playable", "safer_alternative", "strong_recommendation"},
        )
        self.assertTrue(selection["technical_ref"]["has_technical_details"])
        self.assertEqual(selection["technical_ref"]["market_support_level"], "full")
        self.assertEqual(selection["technical_ref"]["market_data_quality"], "strong")
        self.assertEqual(selection["technical_ref"]["statpal_snapshot_types"], ["lineups"])
        self.assertEqual(selection["technical_ref"]["statpal_hydration_source"], "statpal_daily_cache")
        self.assertEqual(selection["technical_ref"]["statpal_snapshot_cache_status"], "hit")
        self.assertEqual(selection["technical_ref"]["statpal_required_snapshot_types"], ["lineups"])
        self.assertEqual(selection["technical_ref"]["statpal_missing_snapshot_types"], [])
        self.assertEqual(selection["technical_ref"]["statpal_snapshot_coverage_percent"], 100.0)

    def test_public_review_does_not_expose_blocked_over_half_goal_replacement(self):
        result = _sample_replace_result()
        result["replacement_market"] = {
            "market": "Over 0.5",
            "meaning": "1 or more total goals",
            "confidence": 96,
            "final_confidence": 96,
            "odds": None,
            "odds_source": "estimated",
            "advisory_score": 96,
            "advisory_status": "strong",
            "market_taxonomy": describe_market("Over 0.5").to_dict(),
        }

        summary = _manual_review_summary([result])
        public = summary["public"]
        selection = public["selections"][0]

        self.assertEqual(summary["replace_count"], 0)
        self.assertEqual(summary["caution_count"], 1)
        self.assertEqual(public["recommended_change_ids"], [])
        self.assertEqual(selection["verdict"]["code"], "caution")
        self.assertFalse(selection["ai_pick"]["available"])
        self.assertEqual(selection["technical_ref"]["blocked_recommendation_markets"], ["Over 0.5"])
        self.assertNotEqual(selection["recommendation"]["action"], "replace")
        bettor_payload = _build_bettor_public_payload(
            SimpleNamespace(id=35, source="sportybet", status="completed"),
            public,
            enhance=False,
        )
        self.assertNotEqual(
            (bettor_payload["recommended_ticket"]["picks"][0] or {}).get("market"),
            "Over 0.5",
        )
        self.assertNotEqual(bettor_payload["games"][0]["recommendation"]["action"], "replace")

    def test_user_submitted_over_half_goal_is_still_assessed_and_returned(self):
        result = _sample_replace_result()
        result["submitted_market"] = "Over 0.5"
        result["verdict"] = "caution"
        result["message"] = "Over 0.5 is supported, but it is not a replacement recommendation."
        result["replacement_market"] = None
        result["selected_market"] = {
            "market": "Over 0.5",
            "meaning": "1 or more total goals",
            "confidence": 70,
            "final_confidence": 68,
            "advisory_score": 68,
            "advisory_status": "playable",
            "advisory_evidence": {},
        }
        result["market_taxonomy"] = describe_market("Over 0.5").to_dict()

        summary = _manual_review_summary([result])
        public = summary["public"]
        selection = public["selections"][0]
        bettor_payload = _build_bettor_public_payload(
            SimpleNamespace(id=36, source="sportybet", status="completed"),
            public,
            enhance=False,
        )

        self.assertEqual(summary["replace_count"], 0)
        self.assertEqual(summary["caution_count"], 1)
        self.assertEqual(selection["your_pick"]["market"], "Over 0.5")
        self.assertEqual(selection["ai_pick"]["market"], "Over 0.5")
        self.assertTrue(selection["ai_pick"]["available"])
        self.assertEqual(selection["technical_ref"]["blocked_recommendation_markets"], [])
        self.assertEqual(bettor_payload["recommended_ticket"]["picks"][0]["market"], "Over 0.5")
        self.assertFalse(bettor_payload["recommended_ticket"]["picks"][0]["changed"])

    def test_deepseek_recommendation_text_cannot_reintroduce_blocked_over_half_goal(self):
        game = {
            "user_pick": {"market": "Over 2.5"},
            "recommendation": {
                "action": "replace",
                "pick": {"market": "Under 3.5"},
            },
        }

        cleaned = _clean_deepseek_recommendation_why(
            game,
            [
                "Over 0.5 is safer for this game.",
                "Under 3.5 has stronger support from the goal profile.",
            ],
        )

        self.assertEqual(cleaned, ["Under 3.5 has stronger support from the goal profile."])

    def test_deepseek_recommendation_text_allows_over_half_when_user_submitted_it(self):
        game = {
            "user_pick": {"market": "Over 0.5"},
            "recommendation": {
                "action": "caution",
                "pick": {"market": "Over 0.5"},
            },
        }

        cleaned = _clean_deepseek_recommendation_why(
            game,
            ["Over 0.5 is your submitted pick and only needs one goal."],
        )

        self.assertEqual(cleaned, ["Over 0.5 is your submitted pick and only needs one goal."])

    def test_deepseek_enhancement_filters_blocked_over_half_recommendation_text(self):
        payload = {
            "id": 37,
            "source": "sportybet",
            "status": "completed",
            "games": [
                {
                    "match": "Alpha vs Beta",
                    "user_pick": {"market": "Over 2.5", "summary": "Original summary"},
                    "analysis": {
                        "positive_evidence": ["The goal model projects about 2.6 total goals."],
                        "risk_evidence": ["Over 2.5 is close to the line."],
                        "conclusion": "Original conclusion.",
                    },
                    "recommendation": {
                        "action": "replace",
                        "pick": {"market": "Under 3.5"},
                        "why": ["Original recommendation."],
                    },
                }
            ],
        }
        parsed = {
            "games": [
                {
                    "index": 0,
                    "user_pick_summary": "DeepSeek summary.",
                    "positive_evidence": ["The goal model projects about 2.6 total goals."],
                    "risk_evidence": ["Over 2.5 is close to the line."],
                    "conclusion": "DeepSeek conclusion.",
                    "recommendation_why": [
                        "Over 0.5 is safer for this fixture.",
                        "Under 3.5 has stronger support from the goal profile.",
                    ],
                }
            ]
        }

        with patch("apps.algo.grindalgo.algo_runner.llm_reasoning_enabled", return_value=True), patch(
            "apps.algo.grindalgo.algo_runner._deepseek_chat_completion",
            return_value="{}",
        ), patch("apps.algo.grindalgo.algo_runner._parse_llm_json", return_value=parsed):
            enhanced = _enhance_bettor_public_with_deepseek(payload)

        self.assertEqual(enhanced["games"][0]["user_pick"]["summary"], "DeepSeek summary.")
        self.assertEqual(
            enhanced["games"][0]["recommendation"]["why"],
            ["Under 3.5 has stronger support from the goal profile."],
        )

    def test_slip_review_debug_logging_exposes_decision_inputs(self):
        from apps.algo.views import _log_slip_review_debug

        summary = _manual_review_summary([_sample_replace_result()])
        review = type("Review", (), {"id": 33, "status": "completed", "source": "sportybet"})()

        with self.assertLogs("apps.algo.views", level="INFO") as captured:
            _log_slip_review_debug(review, summary)

        text = "\n".join(captured.output)
        self.assertIn("Slip review public summary review=33", text)
        self.assertIn("user_conf=", text)
        self.assertIn("ai_conf=", text)
        self.assertIn("success_delta=", text)
        self.assertIn("Slip review leg debug review=33", text)
        self.assertIn("user_prob=", text)
        self.assertIn("data_conf=", text)
        self.assertIn("confidence_gain=", text)
        self.assertIn("statpal_coverage=", text)
        self.assertIn("blocked_recommendations=", text)
        self.assertIn("reason_codes=", text)

    def test_bettor_public_payload_is_product_facing(self):
        summary = _manual_review_summary([_sample_replace_result()])
        review = SimpleNamespace(id=34, source="sportybet", status="completed")

        payload = _build_bettor_public_payload(review, summary["public"], enhance=False)

        self.assertEqual(payload["id"], 34)
        self.assertEqual(payload["source"], "sportybet")
        self.assertEqual(set(payload.keys()), {"id", "source", "status", "ticket", "games", "recommended_ticket", "disclaimer"})
        self.assertIn("user_picks", payload["ticket"])
        self.assertIn("recommended_picks", payload["ticket"])
        self.assertIn("verdict", payload["ticket"])
        self.assertEqual(len(payload["games"]), 1)
        game = payload["games"][0]
        self.assertEqual(game["user_pick"]["market"], "Away Win")
        self.assertIn(game["user_pick"]["verdict"], {"risky", "caution", "keep", "review"})
        self.assertIn("positive_evidence", game["analysis"])
        self.assertIn("risk_evidence", game["analysis"])
        self.assertIn("conclusion", game["analysis"])
        self.assertIn("action", game["recommendation"])
        self.assertNotIn("technical_ref", game)
        self.assertNotIn("reason_codes", game)
        self.assertEqual(payload["recommended_ticket"]["picks"][0]["match"], game["match"])

    def test_bettor_public_payload_displays_tiny_success_percent(self):
        review = SimpleNamespace(id=38, source="sportybet", status="completed")
        payload = _build_bettor_public_payload(
            review,
            {
                "ticket_summary": {
                    "total_legs": 36,
                    "pick_breakdown": {},
                    "user_ticket": {
                        "overall_confidence_score": 59,
                        "estimated_success_percent": 0.00042,
                    },
                    "ai_ticket": {
                        "overall_confidence_score": 65,
                        "estimated_success_percent": 0.0042,
                    },
                    "improvement": {},
                },
                "selections": [],
            },
            enhance=False,
        )

        self.assertEqual(payload["ticket"]["user_picks"]["estimated_success_display"], "<0.01%")
        self.assertEqual(payload["ticket"]["recommended_picks"]["estimated_success_display"], "<0.01%")
        self.assertEqual(payload["recommended_ticket"]["estimated_success_display"], "<0.01%")

    def test_market_not_found_with_replacement_counts_as_analysed(self):
        summary = _manual_review_summary([_sample_market_not_found_with_replacement()])
        public = summary["public"]

        self.assertEqual(summary["analysed_count"], 1)
        self.assertEqual(summary["replace_count"], 1)
        self.assertEqual(summary["unmatched_count"], 0)
        self.assertEqual(public["ticket"]["analysed_legs"], 1)
        self.assertEqual(public["ticket"]["unmatched_legs"], 0)
        self.assertEqual(public["counts"]["replace"], 1)
        self.assertEqual(public["counts"]["unmatched"], 0)
        self.assertEqual(public["selections"][0]["verdict"]["code"], "replace")

    def test_public_review_exposes_positive_price_edge_without_internal_payload(self):
        result = _sample_replace_result()
        result["selected_market"]["advisory_evidence"]["statpal"]["odds_value"] = {
            "offered_odds": 2.20,
            "statpal_reference_odds": 2.00,
            "value_edge_pct": 10.0,
            "matched_market": "Over/Under",
            "matched_outcome": "Over",
            "bookmaker": "10Bet",
        }

        selection = _manual_review_summary([result])["public"]["selections"][0]

        self.assertEqual(selection["price_check"]["status"], "positive_edge")
        self.assertEqual(selection["price_check"]["offered_odds"], 2.2)
        self.assertEqual(selection["price_check"]["reference_odds"], 2.0)
        self.assertIn("price_edge", selection["reason_codes"])
        self.assertFalse(any("StatPal reference" in reason for reason in selection["why"]))
        self.assertNotIn("statpal_advisory", selection["your_pick"])

    def test_bettor_public_uses_stats_evidence_not_reference_price_copy(self):
        result = _sample_replace_result()
        result["home_recent_form"] = {
            "games": 8,
            "wins": 3,
            "draws": 3,
            "losses": 2,
            "avg_scored": 2.12,
            "avg_conceded": 1.62,
        }
        result["away_recent_form"] = {
            "games": 8,
            "wins": 4,
            "draws": 2,
            "losses": 2,
            "avg_scored": 1.5,
            "avg_conceded": 1.0,
        }
        result["selected_market"]["advisory_evidence"]["statpal"]["odds_value"] = {
            "offered_odds": 1.80,
            "statpal_reference_odds": 1.80,
            "value_edge_pct": 0.0,
        }
        review = SimpleNamespace(id=35, source="sportybet", status="completed")

        payload = _build_bettor_public_payload(
            review,
            _manual_review_summary([result])["public"],
            enhance=False,
        )

        game = payload["games"][0]
        joined = " ".join(game["analysis"]["positive_evidence"] + game["analysis"]["risk_evidence"] + game["recommendation"]["why"])
        self.assertIn("Home: 3W-3D-2L in 8", joined)
        self.assertIn("Away: 4W-2D-2L in 8", joined)
        self.assertNotIn("StatPal reference", joined)

    def test_bettor_public_summary_separates_review_from_risky(self):
        assessed = _sample_replace_result()
        review_leg = _sample_market_not_found_without_score()
        review = SimpleNamespace(id=36, source="sportybet", status="partial")

        payload = _build_bettor_public_payload(
            review,
            _manual_review_summary([assessed, review_leg])["public"],
            enhance=False,
        )

        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["ticket"]["user_picks"]["summary"]["review"], 1)
        self.assertEqual(payload["recommended_ticket"]["picks"][1]["action"], "review")
        self.assertFalse(payload["recommended_ticket"]["picks"][1]["included_in_estimate"])
        self.assertIn("needs changing", payload["ticket"]["verdict"]["title"])

    def test_bettor_public_filters_provider_context_evidence(self):
        result = _sample_replace_result()
        result["statpal_context"] = {
            "snapshots": {
                "detailed_stats": {},
                "injuries_suspensions": {},
                "lineups": {},
                "predictions": {},
            },
            "hydration_source": "statpal_daily_cache",
            "snapshot_cache_status": "hit",
        }
        result["selected_market"]["advisory_evidence"] = {
            "note": "StatPal context available: detailed_stats, injuries_suspensions, lineups, predictions."
        }
        result["selected_market"]["advisory_warnings"] = [
            "StatPal context available: detailed_stats, injuries_suspensions, lineups, predictions."
        ]
        review = SimpleNamespace(id=37, source="sportybet", status="partial")

        payload = _build_bettor_public_payload(
            review,
            _manual_review_summary([result])["public"],
            enhance=False,
        )

        game = payload["games"][0]
        joined = " ".join(
            game["analysis"]["positive_evidence"]
            + game["analysis"]["risk_evidence"]
            + game["recommendation"]["why"]
        )
        self.assertNotIn("StatPal", joined)
        self.assertNotIn("detailed_stats", joined)
        self.assertNotIn("injuries_suspensions", joined)

    def test_public_confidence_label_matches_pick_band(self):
        self.assertEqual(_public_confidence_label(64), "Moderate")
        self.assertEqual(_public_confidence_label(65), "Moderate")

    def test_public_only_payload_is_minimal_while_analysing_without_db(self):
        review = SimpleNamespace(
            id=36,
            source="sportybet",
            status="analysing",
            summary={},
            created_at="2026-08-10T22:56:48Z",
            updated_at="2026-08-10T22:57:00Z",
        )

        payload = _slip_review_payload(review, public_only=True)

        self.assertEqual(payload["status"], "analysing")
        self.assertEqual(payload["progress"]["phase"], "analysing")
        self.assertIn("created_at", payload)
        self.assertIn("updated_at", payload)
        self.assertNotIn("ticket", payload)
        self.assertNotIn("games", payload)
        self.assertNotIn("recommended_ticket", payload)

    def test_market_not_found_without_replacement_but_scored_counts_as_analysed(self):
        summary = _manual_review_summary([_sample_market_not_found_without_replacement_but_scored()])
        public = summary["public"]

        self.assertEqual(summary["analysed_count"], 1)
        self.assertEqual(summary["remove_count"], 0)
        self.assertEqual(summary["caution_count"], 1)
        self.assertEqual(summary["unmatched_count"], 0)
        self.assertEqual(public["ticket"]["analysed_legs"], 1)
        self.assertEqual(public["ticket"]["unmatched_legs"], 0)
        self.assertEqual(public["counts"]["remove"], 0)
        self.assertEqual(public["counts"]["caution"], 1)
        self.assertEqual(public["counts"]["unmatched"], 0)
        self.assertEqual(public["selections"][0]["verdict"]["code"], "caution")

    def test_market_not_found_without_score_counts_as_not_assessed_not_unmatched(self):
        summary = _manual_review_summary([_sample_market_not_found_without_score()])
        public = summary["public"]

        self.assertEqual(summary["analysed_count"], 0)
        self.assertEqual(summary["unmatched_count"], 0)
        self.assertEqual(summary["not_assessed_count"], 1)
        self.assertEqual(public["ticket"]["unmatched_legs"], 0)
        self.assertEqual(public["counts"]["unmatched"], 0)
        self.assertEqual(public["counts"]["not_assessed"], 1)
        self.assertEqual(public["selections"][0]["state"], "insufficient_data")

    def test_match_shots_on_target_text_stays_match_level_market(self):
        descriptor = describe_market("Shots On Target Under 9.5")

        self.assertEqual(descriptor.family, "shots_on_target_total")
        self.assertEqual(descriptor.side, "under")
        self.assertEqual(descriptor.line, "9.5")

    def test_caution_verdict_for_avoid_bucket_does_not_say_playable(self):
        verdict = _public_verdict_object(
            "caution",
            submitted_market="GG / BTTS Yes",
            pick_status="avoid",
        )

        self.assertNotIn("playable", verdict["message"].lower())
        self.assertIn("high risk", verdict["message"].lower())

    def test_broad_replacement_must_be_materially_stronger(self):
        selected = {
            "market": "Both Halves Over 1.5 - Yes",
            "advisory_score": 54.0,
            "market_taxonomy": describe_market("Both Halves Over 1.5 - Yes").to_dict(),
        }
        weak_broad = {
            "market": "Corners Under 12.5",
            "advisory_score": 55.9,
            "market_taxonomy": describe_market("Corners Under 12.5").to_dict(),
        }
        strong_broad = {
            "market": "Cards Under 3.5",
            "advisory_score": 72.0,
            "market_taxonomy": describe_market("Cards Under 3.5").to_dict(),
        }

        self.assertIsNone(
            _replacement_market_for_slip(
                {"markets": []},
                selected_market=selected,
                generated_markets=[weak_broad],
                allow_safer_fallback=True,
            )
        )
        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=[weak_broad, strong_broad],
            allow_safer_fallback=True,
        )

        self.assertEqual(replacement["market"], "Cards Under 3.5")

    def test_blocked_replacement_sanitizer_downgrades_replace_to_caution(self):
        item = _sample_replace_result()
        item["replacement_market"] = {
            "market": "Over 0.5",
            "advisory_score": 96,
            "advisory_status": "strong",
        }

        sanitized = _without_blocked_replacement_recommendation(item)

        self.assertIsNone(sanitized["replacement_market"])
        self.assertEqual(sanitized["verdict"], "caution")
        self.assertFalse(sanitized["better_market_available"])

    def test_ticket_killers_recommend_changing_not_removing(self):
        message = _ticket_killers_message(
            SimpleNamespace(
                killers=[
                    {
                        "drop_lift_points": 12.34,
                        "risk_share_percent": 55.5,
                    }
                ],
            )
        )

        self.assertNotIn("Removing", message)
        self.assertIn("Changing", message)


class SlipReviewPayloadDbTests(TestCase):
    def _randomize_review_payload(self):
        games = [
            {
                "id": "a",
                "match": "Alpha vs Beta",
                "kickoff": "2026-08-20T18:00:00Z",
                "user_pick": {
                    "market": "Home Win",
                    "odds": 1.55,
                    "confidence_score": 72,
                    "confidence_label": "Strong",
                    "verdict": "keep",
                    "summary": "Supported.",
                },
                "analysis": {"positive_evidence": [], "risk_evidence": [], "conclusion": ""},
                "recommendation": {
                    "action": "keep",
                    "pick": {
                        "market": "Home Win",
                        "odds": 1.55,
                        "confidence_score": 72,
                        "confidence_label": "Strong",
                    },
                    "why": [],
                },
            },
            {
                "id": "b",
                "match": "Gamma vs Delta",
                "kickoff": "2026-08-20T19:00:00Z",
                "user_pick": {
                    "market": "Draw",
                    "odds": 3.2,
                    "confidence_score": 42,
                    "confidence_label": "Low",
                    "verdict": "risky",
                    "summary": "Risky.",
                },
                "analysis": {"positive_evidence": [], "risk_evidence": [], "conclusion": ""},
                "recommendation": {
                    "action": "replace",
                    "pick": {
                        "market": "DC: X2",
                        "odds": 1.42,
                        "confidence_score": 80,
                        "confidence_label": "Very Strong",
                    },
                    "why": [],
                },
            },
            {
                "id": "c",
                "match": "Epsilon vs Zeta",
                "kickoff": "2026-08-20T20:00:00Z",
                "user_pick": {
                    "market": "BTTS Yes",
                    "odds": 1.8,
                    "confidence_score": None,
                    "confidence_label": "Unknown",
                    "verdict": "review",
                    "summary": "Needs review.",
                },
                "analysis": {"positive_evidence": [], "risk_evidence": [], "conclusion": ""},
                "recommendation": {"action": "review", "pick": None, "why": []},
            },
            {
                "id": "d",
                "match": "Eta vs Theta",
                "kickoff": "2026-08-20T21:00:00Z",
                "user_pick": {
                    "market": "Over 1.5",
                    "odds": 1.35,
                    "confidence_score": 65,
                    "confidence_label": "Moderate",
                    "verdict": "caution",
                    "summary": "Playable.",
                },
                "analysis": {"positive_evidence": [], "risk_evidence": [], "conclusion": ""},
                "recommendation": {
                    "action": "caution",
                    "pick": {
                        "market": "Over 1.5",
                        "odds": 1.35,
                        "confidence_score": 65,
                        "confidence_label": "Moderate",
                    },
                    "why": [],
                },
            },
            {
                "id": "e",
                "match": "Iota vs Kappa",
                "kickoff": "2026-08-20T22:00:00Z",
                "user_pick": {
                    "market": "Under 3.5",
                    "odds": 1.44,
                    "confidence_score": 55,
                    "confidence_label": "Borderline",
                    "verdict": "risky",
                    "summary": "Borderline.",
                },
                "analysis": {"positive_evidence": [], "risk_evidence": [], "conclusion": ""},
                "recommendation": {"action": "caution", "pick": None, "why": []},
            },
            {
                "id": "f",
                "match": "Lambda vs Mu",
                "kickoff": "2026-08-20T23:00:00Z",
                "user_pick": {
                    "market": "Away Win",
                    "odds": 1.62,
                    "confidence_score": 78,
                    "confidence_label": "Strong",
                    "verdict": "keep",
                    "summary": "Strong.",
                },
                "analysis": {"positive_evidence": [], "risk_evidence": [], "conclusion": ""},
                "recommendation": {
                    "action": "keep",
                    "pick": {
                        "market": "Away Win",
                        "odds": 1.62,
                        "confidence_score": 78,
                        "confidence_label": "Strong",
                    },
                    "why": [],
                },
            },
        ]
        return {
            "id": 999,
            "source": "sportybet",
            "status": "completed",
            "ticket": {"total_games": len(games)},
            "games": games,
            "recommended_ticket": {"picks": []},
            "disclaimer": "Confidence scores are estimates.",
        }

    def test_public_only_review_payload_is_minimal_while_analysing(self):
        user = get_user_model().objects.create_user(username="progress")
        review = SlipReview.objects.create(
            user=user,
            source=SlipReview.Source.SPORTYBET,
            status=SlipReview.Status.ANALYSING,
            title="SportyBet review",
            summary={},
        )

        payload = _slip_review_payload(review, public_only=True)

        self.assertEqual(payload["id"], review.id)
        self.assertEqual(payload["source"], "sportybet")
        self.assertEqual(payload["status"], "analysing")
        self.assertEqual(payload["progress"]["phase"], "analysing")
        self.assertIn("created_at", payload)
        self.assertIn("updated_at", payload)
        self.assertNotIn("ticket", payload)
        self.assertNotIn("games", payload)
        self.assertNotIn("recommended_ticket", payload)

    def test_public_only_review_payload_hides_internal_summary(self):
        user = get_user_model().objects.create_user(username="tester")
        summary = _manual_review_summary([_sample_replace_result()])
        review = SlipReview.objects.create(
            user=user,
            source=SlipReview.Source.SPORTYBET,
            status=SlipReview.Status.COMPLETED,
            title="SportyBet review",
            summary=summary,
        )

        payload = _slip_review_payload(review, public_only=True)

        self.assertEqual(payload["id"], review.id)
        self.assertEqual(payload["source"], "sportybet")
        self.assertIn("ticket", payload)
        self.assertIn("games", payload)
        self.assertIn("recommended_ticket", payload)
        self.assertIn("disclaimer", payload)
        self.assertEqual(payload["ticket"]["total_games"], 1)
        self.assertIn("user_picks", payload["ticket"])
        self.assertIn("recommended_picks", payload["ticket"])
        self.assertEqual(payload["games"][0]["user_pick"]["market"], "Away Win")
        self.assertIn("analysis", payload["games"][0])
        self.assertIn("positive_evidence", payload["games"][0]["analysis"])
        self.assertIn("risk_evidence", payload["games"][0]["analysis"])
        self.assertIn("recommendation", payload["games"][0])
        self.assertEqual(payload["recommended_ticket"]["picks"][0]["match"], payload["games"][0]["match"])
        self.assertNotIn("summary", payload)
        self.assertNotIn("intelligence", payload)
        self.assertNotIn("api_usage", payload)
        self.assertNotIn("technical_ref", payload["games"][0])
        self.assertNotIn("reason_codes", payload["games"][0])

    def test_public_only_review_payload_exposes_smart_randomize_options(self):
        user = get_user_model().objects.create_user(username="randomize-options")
        review = SlipReview.objects.create(
            user=user,
            source=SlipReview.Source.SPORTYBET,
            status=SlipReview.Status.COMPLETED,
            title="SportyBet review",
            summary={"bettor_public": self._randomize_review_payload()},
        )

        payload = _slip_review_payload(review, public_only=True)

        self.assertTrue(payload["smart_randomize"]["available"])
        self.assertEqual(payload["smart_randomize"]["eligible_games"], 5)
        self.assertEqual(payload["smart_randomize"]["options"], [2, 4])

    def test_randomize_endpoint_returns_top_requested_picks(self):
        user = get_user_model().objects.create_user(username="randomize-user")
        review = SlipReview.objects.create(
            user=user,
            source=SlipReview.Source.SPORTYBET,
            status=SlipReview.Status.COMPLETED,
            title="SportyBet review",
            summary={"bettor_public": self._randomize_review_payload()},
        )
        client = APIClient()
        client.force_authenticate(user=user)

        response = client.post(f"/api/algo/slip-reviews/{review.id}/randomize/", {"games": 4}, format="json")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["requested_games"], 4)
        self.assertEqual(payload["available_options"], [2, 4])
        self.assertEqual([pick["match"] for pick in payload["picks"]], [
            "Gamma vs Delta",
            "Lambda vs Mu",
            "Alpha vs Beta",
            "Eta vs Theta",
        ])
        self.assertEqual(payload["picks"][0]["market"], "DC: X2")
        self.assertTrue(payload["picks"][0]["changed_from_user_pick"])
        self.assertEqual(payload["ticket"]["total_games"], 4)
        self.assertGreater(payload["ticket"]["confidence_score"], 70)
        self.assertTrue(any(item["match"] == "Epsilon vs Zeta" for item in payload["excluded"]))

    def test_randomize_endpoint_rejects_unavailable_size(self):
        user = get_user_model().objects.create_user(username="randomize-size")
        review = SlipReview.objects.create(
            user=user,
            source=SlipReview.Source.SPORTYBET,
            status=SlipReview.Status.COMPLETED,
            title="SportyBet review",
            summary={"bettor_public": self._randomize_review_payload()},
        )
        client = APIClient()
        client.force_authenticate(user=user)

        response = client.post(f"/api/algo/slip-reviews/{review.id}/randomize/", {"games": 6}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["available_options"], [2, 4])

    def test_randomize_endpoint_waits_for_analysis_to_finish(self):
        user = get_user_model().objects.create_user(username="randomize-progress")
        review = SlipReview.objects.create(
            user=user,
            source=SlipReview.Source.SPORTYBET,
            status=SlipReview.Status.ANALYSING,
            title="SportyBet review",
            summary={},
        )
        client = APIClient()
        client.force_authenticate(user=user)

        response = client.post(f"/api/algo/slip-reviews/{review.id}/randomize/", {"games": 2}, format="json")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["status"], "analysing")

    def test_public_only_partial_review_is_reported_as_completed(self):
        user = get_user_model().objects.create_user(username="partial-user")
        summary = _manual_review_summary([_sample_replace_result(), _sample_market_not_found_without_score()])
        review = SlipReview.objects.create(
            user=user,
            source=SlipReview.Source.SPORTYBET,
            status=SlipReview.Status.PARTIAL,
            title="SportyBet review",
            summary=summary,
        )

        payload = _slip_review_payload(review, public_only=True)
        compact = _compact_slip_review_list_payload(review)

        self.assertEqual(payload["status"], SlipReview.Status.COMPLETED)
        self.assertEqual(compact["status"], SlipReview.Status.COMPLETED)

    def test_full_api_review_payload_also_hides_api_usage(self):
        user = get_user_model().objects.create_user(username="tester2")
        summary = _manual_review_summary([_sample_replace_result()])
        review = SlipReview.objects.create(
            user=user,
            source=SlipReview.Source.SPORTYBET,
            status=SlipReview.Status.COMPLETED,
            title="SportyBet review",
            summary=summary,
        )

        payload = _slip_review_payload(review, public_only=False)

        self.assertNotIn("api_usage", payload)
        self.assertNotIn("api_usage", payload["summary"])
        self.assertNotIn("api_usage", payload["intelligence"])
        self.assertNotIn("api_usage", payload["public"])

    def test_events_endpoint_returns_events_after_cursor(self):
        user = get_user_model().objects.create_user(username="event-user")
        review = SlipReview.objects.create(
            user=user,
            source=SlipReview.Source.SPORTYBET,
            status=SlipReview.Status.ANALYSING,
            title="SportyBet review",
            summary={
                "progress": {
                    "phase": "analysing_legs",
                    "total": 2,
                    "completed": 1,
                    "percent": 50.0,
                    "message": "Analysed 1 of 2 selections.",
                }
            },
        )
        first = SlipReviewEvent.objects.create(
            review=review,
            event_type="review.progress",
            payload={"completed": 0},
        )
        second = SlipReviewEvent.objects.create(
            review=review,
            event_type="leg.completed",
            payload={"completed": 1},
        )
        client = APIClient()
        client.force_authenticate(user=user)

        response = client.get(f"/api/algo/slip-reviews/{review.id}/events/", {"after_id": first.id})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["review_id"], review.id)
        self.assertEqual(payload["status"], SlipReview.Status.ANALYSING)
        self.assertEqual(payload["latest_event_id"], second.id)
        self.assertEqual(payload["progress"]["completed"], 1)
        self.assertEqual([event["id"] for event in payload["events"]], [second.id])
        self.assertEqual(payload["events"][0]["event_type"], "leg.completed")

    def test_events_endpoint_maps_partial_to_completed(self):
        user = get_user_model().objects.create_user(username="event-partial-user")
        review = SlipReview.objects.create(
            user=user,
            source=SlipReview.Source.SPORTYBET,
            status=SlipReview.Status.PARTIAL,
            title="SportyBet review",
            summary={
                "progress": {
                    "phase": "completed",
                    "total": 2,
                    "completed": 2,
                    "percent": 100.0,
                    "message": "Slip review completed.",
                    "final_status": SlipReview.Status.PARTIAL,
                }
            },
        )
        event = SlipReviewEvent.objects.create(
            review=review,
            event_type="review.completed",
            payload={
                "status": SlipReview.Status.PARTIAL,
                "progress": {"phase": "completed", "final_status": SlipReview.Status.PARTIAL},
            },
        )
        client = APIClient()
        client.force_authenticate(user=user)

        response = client.get(f"/api/algo/slip-reviews/{review.id}/events/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], SlipReview.Status.COMPLETED)
        self.assertEqual(payload["progress"]["final_status"], SlipReview.Status.COMPLETED)
        self.assertEqual(payload["events"][0]["id"], event.id)
        self.assertEqual(payload["events"][0]["payload"]["status"], SlipReview.Status.COMPLETED)
        self.assertEqual(payload["events"][0]["payload"]["progress"]["final_status"], SlipReview.Status.COMPLETED)

    def test_streamed_leg_payload_contains_public_game_card(self):
        user = get_user_model().objects.create_user(username="stream-card")
        review = SlipReview.objects.create(
            user=user,
            source=SlipReview.Source.SPORTYBET,
            status=SlipReview.Status.ANALYSING,
            title="SportyBet review",
            summary={},
        )

        payload = _streamed_slip_review_game_payload(review, 0, _sample_replace_result())

        self.assertEqual(payload["index"], 0)
        self.assertEqual(payload["order"], 1)
        self.assertEqual(payload["game"]["match"], "Norway vs England")
        self.assertEqual(payload["game"]["user_pick"]["market"], "Away Win")
        self.assertIn("analysis", payload["game"])
        self.assertIn("positive_evidence", payload["game"]["analysis"])
        self.assertIn("recommendation", payload["game"])
        self.assertEqual(payload["recommended_pick"]["match"], "Norway vs England")

    def test_slip_leg_cache_key_uses_market_descriptor_code(self):
        cache_key, raw_key = _slip_leg_analysis_cache_key(
            {
                "provider": "sportybet",
                "match": "Sporting vs Vitoria SC Guimaraes",
                "market": "Draw 2UP",
                "provider_payload": {
                    "odds": "4.20",
                    "marketId": "12",
                    "outcomeId": "34",
                },
            }
        )

        self.assertEqual(len(cache_key), 64)
        self.assertTrue(raw_key["market"])

    def test_stream_token_endpoint_returns_scoped_short_lived_ticket(self):
        user = get_user_model().objects.create_user(username="stream-token")
        review = SlipReview.objects.create(
            user=user,
            source=SlipReview.Source.SPORTYBET,
            status=SlipReview.Status.ANALYSING,
            title="SportyBet review",
            summary={},
        )
        client = APIClient()
        client.force_authenticate(user=user)

        response = client.post(f"/api/algo/slip-reviews/{review.id}/stream-token/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["expires_in"], 1800)
        self.assertIn(f"/ws/slip-reviews/{review.id}/?ticket=", payload["ws_path"])
        self.assertNotIn("?token=", payload["ws_path"])
        ticket = payload["ticket"]
        self.assertFalse(SlipReviewStreamToken.objects.filter(token_hash=ticket).exists())
        stored = SlipReviewStreamToken.objects.get(review=review, user=user)
        self.assertEqual(stored.token_hash, _stream_ticket_hash(ticket))

    def test_compact_slip_review_list_payload_contains_pick_summary_only(self):
        user = get_user_model().objects.create_user(username="compact-list")
        review = SlipReview.objects.create(
            user=user,
            source=SlipReview.Source.SPORTYBET,
            status=SlipReview.Status.COMPLETED,
            title="SportyBet review",
            summary={},
        )
        SlipSelection.objects.create(
            review=review,
            order=1,
            submitted_match="Wolves vs Blackburn",
            submitted_market="Home Win",
            status="analysed",
            verdict="replace",
            odds="1.80",
            analysis_payload={
                "replacement_market": {
                    "market": "DC: X2",
                    "confidence": 65,
                }
            },
        )

        payload = _compact_slip_review_list_payload(review)

        self.assertEqual(payload["id"], review.id)
        self.assertEqual(payload["number_of_games"], 1)
        self.assertEqual(payload["status"], SlipReview.Status.COMPLETED)
        self.assertEqual(payload["picks"][0]["your_pick"]["market"], "Home Win")
        self.assertEqual(payload["picks"][0]["ai_pick"]["market"], "DC: X2")
        self.assertNotIn("summary", payload)
        self.assertNotIn("intelligence", payload)

    def test_stale_recovery_finalizes_from_persisted_completed_legs(self):
        user = get_user_model().objects.create_user(username="stale-finalize")
        review = SlipReview.objects.create(
            user=user,
            source=SlipReview.Source.SPORTYBET,
            status=SlipReview.Status.ANALYSING,
            title="SportyBet review",
            summary={"progress": {"phase": "analysing_legs", "total": 1, "completed": 1}},
        )
        stale_time = timezone.now() - timezone.timedelta(minutes=45)
        SlipReview.objects.filter(id=review.id).update(updated_at=stale_time)
        SlipSelection.objects.create(
            review=review,
            order=1,
            submitted_match="Norway vs England",
            submitted_market="Away Win",
            status="analysed",
            verdict="replace",
            analysis_payload=_sample_replace_result(),
        )

        result = recover_stale_slip_reviews(stale_after_seconds=60, limit=5)
        review.refresh_from_db()

        self.assertEqual(result["recovered"], 1)
        self.assertIn(review.status, {SlipReview.Status.COMPLETED, SlipReview.Status.PARTIAL})
        self.assertTrue(SlipReviewEvent.objects.filter(review=review, event_type="review.completed").exists())

    def test_stale_recovery_fails_review_without_completed_legs(self):
        user = get_user_model().objects.create_user(username="stale-fail")
        review = SlipReview.objects.create(
            user=user,
            source=SlipReview.Source.SPORTYBET,
            status=SlipReview.Status.ANALYSING,
            title="SportyBet review",
            summary={"progress": {"phase": "analysing_legs", "total": 1, "completed": 0}},
        )
        stale_time = timezone.now() - timezone.timedelta(minutes=45)
        SlipReview.objects.filter(id=review.id).update(updated_at=stale_time)

        result = recover_stale_slip_reviews(stale_after_seconds=60, limit=5)
        review.refresh_from_db()

        self.assertEqual(result["failed"], 1)
        self.assertEqual(review.status, SlipReview.Status.FAILED)
        self.assertEqual(review.summary["error_code"], "stale_review_timeout")
        self.assertTrue(SlipReviewEvent.objects.filter(review=review, event_type="review.failed").exists())
