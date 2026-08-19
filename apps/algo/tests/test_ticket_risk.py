from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.algo.models import SlipReview, SlipSelection
from apps.algo.ticket_risk import (
    Calibration,
    TicketRiskService,
    risk_level_for,
    ticket_risk_service,
)


PRIOR = Calibration(basis="prior", sample_size=0, bands={})


def _leg(
    score,
    *,
    replacement=None,
    data_quality="strong",
    confidence_cap=88,
    match="Chelsea vs Brentford",
    family="total_goals",
    assessment="quantitative_model",
):
    # Only a quantitative assessment may enter the ticket probability, so these fixtures
    # must declare their family and assessment type the way real legs do.
    item = {
        "match": match,
        "submitted_market": "Home Win",
        "status": "analysed",
        "verdict": "keep",
        "market_taxonomy": {"family": family, "recognized": True},
        "canonical_market": {"resolution": "mapped", "period": "full_match", "subject": "match"},
        "selected_market": {
            "advisory_score": score,
            "market_capability": {"confidence_cap": confidence_cap, "data_quality": data_quality},
            "statpal_advisory": {"assessment_type": assessment},
        },
    }
    if replacement is not None:
        item["replacement_market"] = {"advisory_score": replacement}
    return item


class TicketProbabilityTests(TestCase):
    def test_ticket_probability_is_multiplicative_not_an_average(self):
        service = TicketRiskService()

        one_leg = service.assess([_leg(80)], calibration=PRIOR)
        four_legs = service.assess([_leg(80)] * 4, calibration=PRIOR)

        self.assertLess(four_legs.success_percent, one_leg.success_percent)
        self.assertAlmostEqual(
            four_legs.success_percent,
            (one_leg.success_percent / 100) ** 4 * 100,
            places=1,
        )

    def test_tiny_nonzero_accumulator_probability_is_not_flattened_to_zero(self):
        ticket = TicketRiskService().assess([_leg(64)] * 36, calibration=PRIOR)

        self.assertGreater(ticket.success_percent, 0)
        self.assertLess(ticket.success_percent, 0.01)

    def test_independent_assessed_legs_are_declared_in_correlation_metadata(self):
        legs = [_leg(70), _leg(65)]
        legs[0]["match_id"] = "fixture-1"
        legs[1]["match_id"] = "fixture-2"

        ticket = TicketRiskService().assess(legs, calibration=PRIOR)

        self.assertFalse(ticket.correlation["applied"])
        self.assertEqual(ticket.correlation["legs_assumed_independent"], 2)

    def test_health_is_leg_quality_and_does_not_decay_with_leg_count(self):
        service = TicketRiskService()

        one_leg = service.assess([_leg(80)], calibration=PRIOR)
        ten_legs = service.assess([_leg(80)] * 10, calibration=PRIOR)

        self.assertAlmostEqual(one_leg.health_percent, ten_legs.health_percent, places=1)

    def test_risk_shares_account_for_the_whole_ticket(self):
        ticket = TicketRiskService().assess([_leg(85), _leg(70), _leg(50)], calibration=PRIOR)

        total = sum(leg.risk_share_percent for leg in ticket.legs)

        self.assertAlmostEqual(total, 100.0, places=0)

    def test_the_weakest_leg_carries_the_largest_risk_share(self):
        ticket = TicketRiskService().assess([_leg(85), _leg(70), _leg(45)], calibration=PRIOR)

        shares = [leg.risk_share_percent for leg in ticket.legs]

        self.assertEqual(max(shares), shares[2])


class TicketKillerTests(TestCase):
    def test_killers_are_ranked_worst_first(self):
        ticket = TicketRiskService().assess(
            [_leg(88, match="Strong"), _leg(50, match="Weak"), _leg(60, match="Middling")],
            calibration=PRIOR,
        )

        matches = [killer["match"] for killer in ticket.killers]

        self.assertEqual(matches[0], "Weak")
        self.assertNotIn("Strong", matches)

    def test_killers_are_capped_at_three(self):
        ticket = TicketRiskService().assess([_leg(45)] * 6, calibration=PRIOR)

        self.assertEqual(len(ticket.killers), 3)

    def test_dropping_a_leg_reports_a_positive_lift(self):
        ticket = TicketRiskService().assess([_leg(85), _leg(40)], calibration=PRIOR)

        weakest = ticket.legs[1]

        self.assertGreater(weakest.drop_lift_points, 0)

    def test_repair_lift_is_only_reported_when_the_alternative_is_better(self):
        ticket = TicketRiskService().assess(
            [_leg(40, replacement=75), _leg(80, replacement=50)],
            calibration=PRIOR,
        )

        self.assertIsNotNone(ticket.legs[0].repair_probability)
        self.assertGreater(ticket.legs[0].repair_lift_points, 0)
        self.assertIsNone(ticket.legs[1].repair_probability)

    def test_repairing_raises_the_ticket_estimate(self):
        ticket = TicketRiskService().assess([_leg(40, replacement=80), _leg(75)], calibration=PRIOR)

        self.assertGreater(ticket.repaired_success_percent, ticket.success_percent)


class DataQualityTests(TestCase):
    def test_unreadable_market_is_unassessed_rather_than_scored_down(self):
        ticket = TicketRiskService().assess(
            [_leg(95, data_quality="poor", confidence_cap=40)],
            calibration=PRIOR,
        )
        leg = ticket.legs[0]

        self.assertEqual(leg.tier, "unknown")
        self.assertIsNone(leg.probability)
        self.assertEqual(leg.unassessed_reason, "insufficient_market_data")

    def test_unassessed_leg_does_not_drag_down_the_ticket_estimate(self):
        service = TicketRiskService()

        alone = service.assess([_leg(80)], calibration=PRIOR)
        with_unreadable = service.assess(
            [_leg(80), _leg(95, data_quality="poor", confidence_cap=40)],
            calibration=PRIOR,
        )

        self.assertEqual(alone.success_percent, with_unreadable.success_percent)
        self.assertEqual(with_unreadable.assessed_legs, 1)
        self.assertEqual(with_unreadable.unassessed_legs, 1)

    def test_thin_coverage_cannot_reach_a_headline_tier(self):
        ticket = TicketRiskService().assess(
            [_leg(95, data_quality="limited", confidence_cap=70)],
            calibration=PRIOR,
        )

        self.assertEqual(ticket.legs[0].tier, "borderline")
        self.assertTrue(ticket.legs[0].capped_by_data_quality)

    def test_good_coverage_allows_the_top_tier(self):
        ticket = TicketRiskService().assess([_leg(95)], calibration=PRIOR)

        self.assertEqual(ticket.legs[0].tier, "very_strong")


class CalibrationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="bettor", email="bettor@example.com", password="pw"
        )
        self.review = SlipReview.objects.create(user=self.user, source=SlipReview.Source.SPORTYBET)

    def _settled(self, score, outcome, count=1):
        for _ in range(count):
            SlipSelection.objects.create(
                review=self.review,
                submitted_match="A vs B",
                submitted_market="Over 2.5",
                settlement_market="Over 2.5",
                match_id="1",
                match_date=date(2026, 8, 8),
                advisory_score=score,
                outcome=outcome,
            )

    def test_calibration_falls_back_to_prior_without_enough_settled_legs(self):
        self._settled(85, SlipSelection.Outcome.WIN, count=5)

        calibration = ticket_risk_service.calibration()

        self.assertEqual(calibration.basis, "prior")
        self.assertEqual(calibration.probability(85), PRIOR.probability(85))

    def test_calibration_uses_settled_outcomes_once_there_is_enough_evidence(self):
        self._settled(85, SlipSelection.Outcome.WIN, count=90)
        self._settled(85, SlipSelection.Outcome.LOSS, count=10)

        calibration = ticket_risk_service.calibration()

        self.assertEqual(calibration.basis, "blended")
        self.assertEqual(calibration.sample_size, 100)
        # Observed 90% pulls the estimate above the conservative prior.
        self.assertGreater(calibration.probability(85), PRIOR.probability(85))

    def test_a_band_that_underperforms_is_marked_down(self):
        self._settled(85, SlipSelection.Outcome.LOSS, count=90)
        self._settled(85, SlipSelection.Outcome.WIN, count=10)

        calibration = ticket_risk_service.calibration()

        self.assertLess(calibration.probability(85), PRIOR.probability(85))

    def test_unsettled_and_unsettleable_legs_are_excluded_from_calibration(self):
        self._settled(85, SlipSelection.Outcome.PENDING, count=50)
        self._settled(85, SlipSelection.Outcome.UNSETTLEABLE, count=50)

        calibration = ticket_risk_service.calibration()

        self.assertEqual(calibration.sample_size, 0)
        self.assertEqual(calibration.basis, "prior")


class RiskLevelTests(TestCase):
    def test_ticket_with_no_assessable_legs_is_unknown(self):
        ticket = TicketRiskService().assess(
            [_leg(90, data_quality="poor", confidence_cap=30)],
            calibration=PRIOR,
        )

        self.assertEqual(risk_level_for(ticket), "unknown")

    def test_ticket_carrying_an_avoid_leg_is_high_risk(self):
        ticket = TicketRiskService().assess([_leg(90), _leg(90), _leg(30)], calibration=PRIOR)

        self.assertEqual(risk_level_for(ticket), "high")

    def test_ticket_of_strong_legs_is_low_risk(self):
        ticket = TicketRiskService().assess([_leg(90), _leg(88), _leg(85)], calibration=PRIOR)

        self.assertEqual(risk_level_for(ticket), "low")
