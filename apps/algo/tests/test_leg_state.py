"""
Leg lifecycle states and the probability-publishing invariant.

The rule: a probability may only be published for a `quantitative_model` assessment.
A heuristic score is a constant plus context nudges, and multiplying it into a ticket
probability would present a guess as arithmetic.
"""

from django.test import SimpleTestCase

from apps.algo.leg_state import LegState, assess_leg, may_publish_probability
from apps.algo.ticket_risk import Calibration, TicketRiskService


PRIOR = Calibration(basis="prior", sample_size=0, bands={})


def _leg(*, family="total_goals", assessment="quantitative_model", score=80,
         status="analysed", resolution="mapped", data_quality="strong", recognized=True):
    return {
        "match": "A vs B",
        "status": status,
        "verdict": "keep",
        "market_taxonomy": {"family": family, "recognized": recognized},
        "canonical_market": {"resolution": resolution, "period": "full_match", "subject": "match"},
        "selected_market": {
            "advisory_score": score,
            "market_capability": {"confidence_cap": 88, "data_quality": data_quality},
            "statpal_advisory": {"assessment_type": assessment},
        },
    }


class TerminalStateTests(SimpleTestCase):
    def test_expired_fixture(self):
        self.assertEqual(assess_leg(_leg(status="expired")).state, LegState.EXPIRED)

    def test_unmatched_fixture(self):
        self.assertEqual(assess_leg(_leg(status="unmatched")).state, LegState.UNMATCHED)

    def test_ambiguous_fixture(self):
        self.assertEqual(assess_leg(_leg(status="ambiguous_match")).state, LegState.AMBIGUOUS_FIXTURE)

    def test_market_identity_guessed_from_text_is_unknown_market(self):
        self.assertEqual(assess_leg(_leg(resolution="unresolved")).state, LegState.UNKNOWN_MARKET)

    def test_unrecognised_market_is_unknown_market(self):
        self.assertEqual(assess_leg(_leg(recognized=False)).state, LegState.UNKNOWN_MARKET)

    def test_family_without_an_evaluator_is_no_model(self):
        leg = _leg(family="correct_score", assessment="none")

        self.assertEqual(assess_leg(leg).state, LegState.NO_MODEL)

    def test_poor_coverage_is_insufficient_data(self):
        self.assertEqual(
            assess_leg(_leg(data_quality="poor")).state, LegState.INSUFFICIENT_DATA
        )

    def test_missing_score_is_insufficient_data(self):
        self.assertEqual(assess_leg(_leg(score=None)).state, LegState.INSUFFICIENT_DATA)

    def test_every_terminal_carries_an_explanation(self):
        for status in ["expired", "unmatched", "ambiguous_match"]:
            self.assertTrue(assess_leg(_leg(status=status)).message)

    def test_a_modelled_leg_reaches_assessed(self):
        assessment = assess_leg(_leg())

        self.assertEqual(assessment.state, LegState.ASSESSED)
        self.assertEqual(assessment.message, "")


class PublishInvariantTests(SimpleTestCase):
    def test_quantitative_assessment_may_publish_a_probability(self):
        self.assertTrue(may_publish_probability(_leg(assessment="quantitative_model")))

    def test_heuristic_assessment_may_not_publish_a_probability(self):
        leg = _leg(family="match_result", assessment="heuristic")

        self.assertEqual(assess_leg(leg).state, LegState.ASSESSED)
        self.assertFalse(may_publish_probability(leg))

    def test_no_model_may_not_publish_a_probability(self):
        self.assertFalse(may_publish_probability(_leg(family="correct_score", assessment="none")))

    def test_terminal_states_may_not_publish_a_probability(self):
        self.assertFalse(may_publish_probability(_leg(status="unmatched")))


class TicketEstimateGatingTests(SimpleTestCase):
    def test_heuristic_legs_are_excluded_from_the_ticket_probability(self):
        ticket = TicketRiskService().assess(
            [_leg(), _leg(family="match_result", assessment="heuristic")],
            calibration=PRIOR,
        )

        self.assertEqual(ticket.assessed_legs, 1)
        self.assertEqual(ticket.unassessed_legs, 1)

    def test_excluded_heuristic_leg_records_its_reason(self):
        ticket = TicketRiskService().assess(
            [_leg(family="btts", assessment="heuristic")], calibration=PRIOR
        )

        self.assertEqual(ticket.legs[0].unassessed_reason, "heuristic_assessment_only")
        self.assertIsNone(ticket.legs[0].probability)

    def test_a_heuristic_leg_does_not_change_the_estimate(self):
        service = TicketRiskService()

        alone = service.assess([_leg()], calibration=PRIOR)
        with_heuristic = service.assess(
            [_leg(), _leg(family="double_chance", assessment="heuristic")], calibration=PRIOR
        )

        self.assertEqual(alone.success_percent, with_heuristic.success_percent)

    def test_a_ticket_of_only_heuristic_legs_reports_no_estimate(self):
        ticket = TicketRiskService().assess(
            [_leg(family="match_result", assessment="heuristic")] * 3, calibration=PRIOR
        )

        self.assertIsNone(ticket.success_percent)
        self.assertIsNone(ticket.health_percent)
        self.assertEqual(ticket.unassessed_legs, 3)
