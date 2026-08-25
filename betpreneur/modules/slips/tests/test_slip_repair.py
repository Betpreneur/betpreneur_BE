"""
Ticket repair (ADR-004).

A repair must be an evidence-based alternative, never a promise of better returns. The
constraints below each *reject* a candidate rather than merely down-ranking it, because
ranking by probability alone recommends near-certainties on every leg.
"""

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from betpreneur.modules.pricing.api import Calibration, TicketRiskService
from betpreneur.modules.slips.domain.repair_plan import (
    DROP,
    KEEP,
    REPLACE,
    contradicts,
    plan_repair,
)
from betpreneur.modules.slips.models import SlipRepair, SlipReview, SlipSelection

PRIOR = Calibration(basis="prior", sample_size=0, bands={})


def _leg(score, *, match="A vs B", market="Over 2.5", odds=2.0, alternative=None,
         alt_score=None, alt_odds=1.4, side="over", family="total_goals"):
    item = {
        "match": match,
        "submitted_market": market,
        "status": "analysed",
        "verdict": "keep",
        "market_taxonomy": {"family": family, "side": side, "recognized": True},
        "canonical_market": {"resolution": "mapped", "period": "full_match", "subject": "match"},
        "matched_fixture": {"match_id": match, "league_id": "1"},
        "provider_payload": {"odds": odds},
        "selected_market": {
            "market": market,
            "odds": odds,
            "advisory_score": score,
            "market_capability": {"confidence_cap": 88, "data_quality": "strong"},
            "statpal_advisory": {"assessment_type": "quantitative_model"},
        },
    }
    if alternative:
        item["replacement_market"] = {
            "market": alternative,
            "odds": alt_odds,
            "advisory_score": alt_score if alt_score is not None else score + 25,
            "market_taxonomy": {"family": family, "side": "over"},
            "statpal_advisory": {"assessment_type": "quantitative_model"},
        }
    return item


def _plan(items, decisions=None):
    risk = TicketRiskService().assess(items, calibration=PRIOR)
    return plan_repair(items, risk, decisions=decisions)


class ThesisTests(SimpleTestCase):
    def test_opposite_result_sides_contradict(self):
        original = {"market_taxonomy": {"side": "home"}}
        alternative = {"market_taxonomy": {"side": "away"}}

        self.assertTrue(contradicts(original, alternative))

    def test_over_and_under_contradict(self):
        self.assertTrue(
            contradicts({"market_taxonomy": {"side": "over"}}, {"market_taxonomy": {"side": "under"}})
        )

    def test_home_and_home_or_draw_do_not_contradict(self):
        self.assertFalse(
            contradicts(
                {"market_taxonomy": {"side": "home"}},
                {"market_taxonomy": {"side": "home_or_draw"}},
            )
        )


class RepairPlanTests(TestCase):
    def test_a_weak_leg_with_a_better_alternative_is_replaced(self):
        plan = _plan([_leg(40, alternative="Over 1.5", alt_score=78)])

        self.assertEqual(plan.decisions[0].action, REPLACE)
        self.assertEqual(plan.decisions[0].revised_market, "Over 1.5")
        self.assertEqual(plan.changes, 1)

    def test_a_strong_leg_is_kept(self):
        plan = _plan([_leg(85)])

        self.assertEqual(plan.decisions[0].action, KEEP)
        self.assertEqual(plan.changes, 0)

    def test_repair_lowers_the_combined_odds_and_says_so(self):
        plan = _plan([_leg(40, odds=3.0, alternative="Over 1.5", alt_score=80, alt_odds=1.3)])

        self.assertLess(plan.revised_combined_odds, plan.original_combined_odds)
        self.assertIn("not a guarantee", plan.disclosure)

    def test_an_alternative_below_the_odds_floor_is_rejected(self):
        plan = _plan([_leg(40, alternative="Under 8.5", alt_score=95, alt_odds=1.02)])

        self.assertNotEqual(plan.decisions[0].action, REPLACE)
        self.assertIn("below_minimum_odds", plan.decisions[0].rejected)

    def test_an_alternative_backing_the_opposite_thesis_is_rejected(self):
        item = _leg(40, market="Home Win", side="home", family="match_result",
                    alternative="Away Win", alt_score=85)
        item["replacement_market"]["market_taxonomy"] = {"family": "match_result", "side": "away"}

        plan = _plan([item])

        self.assertIn("contradicts_original_thesis", plan.decisions[0].rejected)
        self.assertNotEqual(plan.decisions[0].action, REPLACE)

    def test_an_alternative_duplicating_another_leg_is_rejected(self):
        legs = [
            _leg(40, match="A vs B", market="Over 2.5", alternative="Over 1.5", alt_score=80),
            _leg(80, match="C vs D", market="Over 1.5"),
        ]

        plan = _plan(legs)

        self.assertIn("duplicates_another_leg", plan.decisions[0].rejected)

    def test_a_heuristic_alternative_is_rejected(self):
        item = _leg(40, alternative="Over 1.5", alt_score=80)
        item["replacement_market"]["statpal_advisory"] = {"assessment_type": "heuristic"}

        plan = _plan([item])

        self.assertIn("alternative_not_modelled", plan.decisions[0].rejected)

    def test_a_leg_with_no_alternative_reports_why(self):
        plan = _plan([_leg(40)])

        self.assertIn("no_alternative_available", plan.decisions[0].rejected)

    def test_explicit_decisions_override_the_recommendation(self):
        legs = [_leg(85), _leg(40, alternative="Over 1.5", alt_score=80)]

        plan = _plan(legs, decisions={0: DROP, 1: KEEP})

        self.assertEqual(plan.decisions[0].action, DROP)
        self.assertEqual(plan.decisions[1].action, KEEP)
        self.assertEqual(plan.revised_legs, 1)

    def test_dropping_a_leg_removes_it_from_the_revised_odds(self):
        legs = [_leg(85, match="A vs B", odds=2.0), _leg(40, match="C vs D", odds=3.0)]

        plan = _plan(legs, decisions={1: DROP})

        self.assertEqual(plan.revised_legs, 1)
        self.assertAlmostEqual(plan.revised_combined_odds, 2.0, places=2)


class RepairEndpointTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="bettor", email="b@example.com", password="pw"
        )
        self.other = get_user_model().objects.create_user(
            username="stranger", email="s@example.com", password="pw"
        )
        self.review = SlipReview.objects.create(user=self.user, source=SlipReview.Source.SPORTYBET)
        for index, item in enumerate(
            [_leg(85, match="A vs B"), _leg(40, match="C vs D", alternative="Over 1.5", alt_score=80)]
        ):
            SlipSelection.objects.create(
                review=self.review, order=index, submitted_match=item["match"],
                submitted_market=item["submitted_market"], analysis_payload=item,
            )
        # The API authenticates with tokens, so a session login is not enough here.
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.url = reverse("algo-slip-review-repair", args=[self.review.id])

    def test_repair_creates_a_persisted_revision(self):
        response = self.client.post(self.url, {}, content_type="application/json")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(SlipRepair.objects.filter(review=self.review).count(), 1)

    def test_response_compares_original_and_revised(self):
        payload = self.client.post(self.url, {}, content_type="application/json").json()

        self.assertEqual(payload["original"]["legs"], 2)
        self.assertIn("combined_odds", payload["revised"])
        self.assertIn("disclosure", payload)

    def test_explicit_decisions_are_recorded_as_a_custom_repair(self):
        response = self.client.post(
            self.url, {"decisions": [{"index": 1, "action": "drop"}]}, content_type="application/json"
        )

        self.assertEqual(response.json()["mode"], "custom")
        self.assertEqual(response.json()["revised"]["legs"], 1)

    def test_another_users_review_cannot_be_repaired(self):
        self.client.force_authenticate(user=self.other)

        response = self.client.post(self.url, {}, content_type="application/json")

        self.assertEqual(response.status_code, 404)

    def test_a_review_without_selections_is_rejected(self):
        empty = SlipReview.objects.create(user=self.user, source=SlipReview.Source.MANUAL)

        response = self.client.post(
            reverse("algo-slip-review-repair", args=[empty.id]), {}, content_type="application/json"
        )

        self.assertEqual(response.status_code, 400)

    def test_an_invalid_action_is_rejected(self):
        response = self.client.post(
            self.url, {"decisions": [{"index": 0, "action": "explode"}]}, content_type="application/json"
        )

        self.assertEqual(response.status_code, 400)
