from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.algo.models import SlipReview
from apps.algo.views import _manual_review_summary, _slip_review_payload


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
        },
        "market_taxonomy": {
            "recognized": True,
            "core_supported": True,
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


class SlipReviewPublicContractTests(TestCase):
    def test_public_review_contract_is_frontend_friendly(self):
        summary = _manual_review_summary([_sample_replace_result()])
        public = summary["public"]
        selection = public["selections"][0]

        self.assertEqual(summary["api_usage"]["attempted_calls"], 2)
        self.assertEqual(summary["api_usage"]["successful_calls"], 1)
        self.assertEqual(summary["api_usage"]["failed_calls"], 1)
        self.assertEqual(summary["api_usage"]["skipped_by_cache"], 3)
        self.assertEqual(public["contract_version"], "match_checker_public_v1")
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
        self.assertNotIn("taxonomy", selection["your_pick"])
        self.assertNotIn("statpal_advisory", selection["your_pick"])
        self.assertEqual(selection["ai_pick"]["market"], "Over 1.5")
        self.assertIn(
            selection["ai_pick"]["recommendation_strength"],
            {"playable", "safer_alternative", "strong_recommendation"},
        )
        self.assertTrue(selection["technical_ref"]["has_technical_details"])
        self.assertEqual(selection["technical_ref"]["statpal_snapshot_types"], ["lineups"])

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
        self.assertEqual(payload["contract_version"], "match_checker_public_v1")
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
