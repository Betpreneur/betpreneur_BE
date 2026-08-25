from django.test import TestCase

from betpreneur.modules.pricing.api import match_checker_status
from betpreneur.modules.slips.domain.slip_analysis import (
    _selection_flagged_risky,
    _settlement_market_for,
    _ticket_health_label,
)
from betpreneur.modules.slips.interface.views import (
    _review_status_from_summary,
    _slip_intelligence,
)
from betpreneur.modules.slips.models import SlipReview


def _unscored_leg(market="Over 2.5", *, match_id="1498717"):
    """A leg whose fixture matched but whose on-demand analysis failed."""
    return {
        "match": "Midland vs Deportivo Maipu",
        "submitted_market": market,
        "market_taxonomy": {"canonical": market, "recognized": True, "core_supported": True},
        "status": "matched_unscored",
        "verdict": "pending_analysis",
        "message": "Fixture matched, but on-demand analysis could not produce market predictions yet.",
        "matched_fixture": {
            "match_id": match_id,
            "match_date": "2026-08-08",
            "fixture": "Midland vs Deportivo Maipu",
        },
        "provider_payload": {"odds": 5.0},
        "on_demand_analysis": {"status": "failed", "error": "scoring failed", "run_id": 158},
    }


class UnanalysedStatusTests(TestCase):
    def test_missing_score_is_unknown_not_avoid(self):
        self.assertEqual(match_checker_status(None), "unknown")

    def test_a_genuine_zero_score_is_still_avoid(self):
        self.assertEqual(match_checker_status(0), "avoid")
        self.assertEqual(match_checker_status(54), "avoid")

    def test_missing_health_score_is_unknown_not_very_poor(self):
        self.assertEqual(_ticket_health_label(None), "Unknown")
        self.assertEqual(_ticket_health_label(0), "Very Poor")


class ReviewStatusTests(TestCase):
    def test_slip_with_no_analysed_legs_is_not_reported_partial(self):
        summary = {"count": 7, "analysed_count": 0, "pending_analysis_count": 7, "expired_count": 0}

        self.assertEqual(_review_status_from_summary(summary), SlipReview.Status.UNANALYSED)

    def test_slip_with_some_analysed_legs_is_partial(self):
        summary = {"count": 7, "analysed_count": 3, "pending_analysis_count": 4, "expired_count": 0}

        self.assertEqual(_review_status_from_summary(summary), SlipReview.Status.PARTIAL)

    def test_fully_analysed_slip_is_completed(self):
        summary = {"count": 7, "analysed_count": 7, "pending_analysis_count": 0, "expired_count": 0}

        self.assertEqual(_review_status_from_summary(summary), SlipReview.Status.COMPLETED)

    def test_slip_with_nothing_at_all_is_failed(self):
        summary = {"count": 0, "analysed_count": 0, "pending_analysis_count": 0, "expired_count": 0}

        self.assertEqual(_review_status_from_summary(summary), SlipReview.Status.FAILED)


class UnanalysedSlipIntelligenceTests(TestCase):
    def test_unanalysed_ticket_is_reported_as_unknown_not_risky(self):
        _, intelligence = _slip_intelligence([_unscored_leg() for _ in range(7)])
        health = intelligence["public"]["ticket_health"]

        self.assertIsNone(health["score"])
        self.assertEqual(health["label"], "Unknown")
        self.assertEqual(health["risk_level"], "unknown")
        self.assertIn("could be analysed", health["summary"])

    def test_unanalysed_legs_do_not_render_as_avoid(self):
        _, intelligence = _slip_intelligence([_unscored_leg() for _ in range(3)])

        for card in intelligence["public"]["selections"]:
            self.assertEqual(card["your_pick"]["status"], "unknown")
            self.assertIsNone(card["your_pick"]["score"])

    def test_impact_message_does_not_claim_no_risky_picks_when_nothing_was_analysed(self):
        _, intelligence = _slip_intelligence([_unscored_leg() for _ in range(7)])
        message = intelligence["public"]["ticket_impact"]["message"]

        self.assertNotIn("No major risky picks", message)
        self.assertIn("7 selections", message)


class TrackingHonestyTests(TestCase):
    def test_legs_with_settleable_markets_are_reported_as_tracked(self):
        _, intelligence = _slip_intelligence([_unscored_leg() for _ in range(7)])
        tracking = intelligence["public"]["tracking"]

        self.assertTrue(tracking["enabled"])
        self.assertEqual(tracking["tracked_selections"], 7)

    def test_legs_with_unsupported_markets_are_not_reported_as_tracked(self):
        legs = [_unscored_leg(market="Cards Over 3.5"), _unscored_leg(market="Over 9.5")]

        _, intelligence = _slip_intelligence(legs)
        tracking = intelligence["public"]["tracking"]
        learning = intelligence["learning_tracking"]

        self.assertFalse(tracking["enabled"])
        self.assertEqual(tracking["tracked_selections"], 0)
        self.assertEqual(learning["status"], "not_tracked")
        self.assertEqual(learning["outcome_tracking"], "unavailable")
        self.assertTrue(learning["reason"])

    def test_leg_without_a_resolved_fixture_date_is_not_tracked(self):
        leg = _unscored_leg()
        leg["matched_fixture"] = {"match_id": "1498717", "fixture": "Midland vs Deportivo Maipu"}

        _, intelligence = _slip_intelligence([leg])

        self.assertEqual(intelligence["public"]["tracking"]["tracked_selections"], 0)


class SettlementInputTests(TestCase):
    def test_settlement_market_prefers_the_orientation_corrected_market(self):
        item = {
            "analysis_market": "Away Win",
            "market_taxonomy": {"canonical": "Home Win"},
            "matched_fixture": {"match_orientation": "reversed"},
        }

        self.assertEqual(_settlement_market_for(item), "Away Win")

    def test_settlement_market_falls_back_to_taxonomy_for_unscored_legs(self):
        self.assertEqual(_settlement_market_for(_unscored_leg()), "Over 2.5")

    def test_settlement_market_is_blank_for_unsupported_markets(self):
        self.assertEqual(_settlement_market_for(_unscored_leg(market="Cards Over 3.5")), "")

    def test_flagged_risky_covers_the_verdicts_shown_as_concerns(self):
        for verdict in ["remove", "replace", "caution"]:
            self.assertTrue(_selection_flagged_risky({"verdict": verdict}), verdict)
        for verdict in ["keep", "pending_analysis", "expired", "unmatched"]:
            self.assertFalse(_selection_flagged_risky({"verdict": verdict}), verdict)
