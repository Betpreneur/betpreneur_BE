from unittest.mock import patch
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.algo.models import SlipReview
from apps.algo.ticket_risk import SCORE_BANDS, Calibration
from apps.algo.views import (
    _has_statpal_hydration_identity,
    _manual_review_summary,
    _matched_fixture_with_statpal,
    _public_price_check_from_card,
    _should_skip_core_on_demand,
    _slip_review_payload,
    _ticket_killers_message,
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
        self.assertTrue(_has_statpal_hydration_identity({}, {"provider_match_id": "202608091"}))
        self.assertTrue(_has_statpal_hydration_identity({}, {}, {"provider": "statpal", "provider_event_id": "202608091"}))

    def test_statpal_hydration_identity_rejects_plain_api_fixture_only(self):
        self.assertFalse(_has_statpal_hydration_identity({"match_id": "1494240", "home_team": "A", "away_team": "B"}))

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
        self.assertIn("comparison", public)
        self.assertIn("ticket_impact", public)
        self.assertNotIn("api_usage", public)
        self.assertNotIn("recommended_changes", public)
        self.assertEqual(public["recommended_change_ids"], ["1581037"])

        self.assertEqual(selection["verdict"]["code"], "replace")
        self.assertEqual(selection["verdict"]["label"], "Replace")
        self.assertEqual(selection["your_pick"]["market"], "Away Win")
        self.assertEqual(selection["your_pick"]["support_level"], "full")
        self.assertEqual(selection["your_pick"]["data_quality"], "strong")
        self.assertEqual(selection["your_pick"]["confidence_cap"], 88)
        self.assertNotIn("taxonomy", selection["your_pick"])
        self.assertNotIn("statpal_advisory", selection["your_pick"])
        self.assertEqual(selection["price_check"]["status"], "short_price")
        self.assertEqual(selection["price_check"]["edge_percent"], -10.0)
        self.assertEqual(selection["price_check"]["reference_odds"], 2.0)
        self.assertIn("price_edge", selection["reason_codes"])
        self.assertTrue(any("shorter than the StatPal reference" in reason for reason in selection["why"]))
        self.assertEqual(selection["ai_pick"]["market"], "Over 1.5")
        self.assertEqual(selection["ai_pick"]["replacement_scope"], "broad_fallback")
        self.assertIn(
            selection["ai_pick"]["recommendation_strength"],
            {"playable", "safer_alternative", "strong_recommendation"},
        )
        self.assertTrue(selection["technical_ref"]["has_technical_details"])
        self.assertEqual(selection["technical_ref"]["market_support_level"], "full")
        self.assertEqual(selection["technical_ref"]["market_data_quality"], "strong")
        self.assertEqual(selection["technical_ref"]["statpal_snapshot_types"], ["lineups"])

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
        self.assertTrue(any("better than the StatPal reference" in reason for reason in selection["why"]))
        self.assertNotIn("statpal_advisory", selection["your_pick"])

    def test_market_not_found_without_replacement_but_scored_counts_as_analysed(self):
        summary = _manual_review_summary([_sample_market_not_found_without_replacement_but_scored()])
        public = summary["public"]

        self.assertEqual(summary["analysed_count"], 1)
        self.assertEqual(summary["remove_count"], 1)
        self.assertEqual(summary["unmatched_count"], 0)
        self.assertEqual(public["ticket"]["analysed_legs"], 1)
        self.assertEqual(public["ticket"]["unmatched_legs"], 0)
        self.assertEqual(public["counts"]["remove"], 1)
        self.assertEqual(public["counts"]["unmatched"], 0)
        self.assertEqual(public["selections"][0]["verdict"]["code"], "remove")

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
        self.assertEqual(payload["contract_version"], "match_checker_public_v2")
        self.assertIn("selections", payload)
        self.assertNotIn("summary", payload)
        self.assertNotIn("intelligence", payload)
        self.assertNotIn("api_usage", payload)
        self.assertNotIn("api_usage", payload["selections"][0])

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
