"""
Unassessed legs must not be reported as bad picks.

A live scan returned "Avoid — too risky to trust" for five selections that the payload
simultaneously described as `state: insufficient_data` and `risk_tier: Not assessed`.
The verdict branches default to `no_edge -> remove`, so a market nobody evaluated came
out looking like one we had judged and rejected.

Absence of evidence is not evidence of a bad pick — and it is equally wrong to call it
safe, so the risk band is `unknown` rather than `low`.
"""

from django.test import SimpleTestCase

from apps.algo.views import (
    _manual_verdict,
    _market_was_assessed,
    _public_selection_risk,
    _public_verdict_object,
)


def _market(**overrides):
    base = {
        "market": "Corners Over 7.5",
        "advisory_score": None,
        "statpal_advisory": {"available": False, "assessment_type": "quantitative_model"},
    }
    base.update(overrides)
    return base


class AssessmentDetectionTests(SimpleTestCase):
    def test_a_market_without_a_score_was_not_assessed(self):
        self.assertFalse(_market_was_assessed(_market()))

    def test_a_market_whose_evaluator_declined_was_not_assessed(self):
        # A declined evaluator leaves no score behind.
        market = _market(statpal_advisory={"available": False})

        self.assertFalse(_market_was_assessed(market))

    def test_a_family_with_no_model_was_not_assessed(self):
        market = _market(statpal_advisory={"available": True, "assessment_type": "none"})

        self.assertFalse(_market_was_assessed(market))

    def test_a_scored_market_was_assessed(self):
        market = _market(advisory_score=71, statpal_advisory={"available": True, "assessment_type": "quantitative_model"})

        self.assertTrue(_market_was_assessed(market))

    def test_a_market_scored_by_the_core_algo_was_assessed(self):
        # The core algo writes display_score / confidence rather than advisory_score;
        # checking only the advisory key would call every one of those unassessed.
        for key in ("display_score", "final_confidence", "confidence"):
            self.assertTrue(_market_was_assessed({key: 68}), key)


class VerdictTests(SimpleTestCase):
    def test_an_unassessed_market_is_not_told_to_avoid(self):
        verdict = _manual_verdict(_market(), None)

        self.assertEqual(verdict["verdict"], "not_assessed")
        self.assertNotIn("risky", verdict["message"].lower())

    def test_an_unassessed_market_reports_no_score(self):
        verdict = _manual_verdict(_market(), None)

        self.assertIsNone(verdict["advisory_score"])
        self.assertEqual(verdict["advisory_status"], "unknown")

    def test_an_unassessed_market_claims_no_better_alternative(self):
        verdict = _manual_verdict(_market(), {"market": "Over 1.5", "advisory_score": 80})

        self.assertFalse(verdict["better_market_available"])

    def test_a_genuinely_weak_market_is_still_told_to_avoid(self):
        market = _market(
            market="Home Win", advisory_score=48, advisory_status="avoid",
            recommendation_status="no_edge",
            statpal_advisory={"available": True, "assessment_type": "quantitative_model"},
        )

        self.assertEqual(_manual_verdict(market, None)["verdict"], "remove")

    def test_a_strong_market_is_still_kept(self):
        market = _market(
            market="Over 1.5", advisory_score=85, advisory_status="strong",
            recommendation_status="recommended", selected=True,
            statpal_advisory={"available": True, "assessment_type": "quantitative_model"},
        )

        self.assertEqual(_manual_verdict(market, None)["verdict"], "keep")


class PublicVocabularyTests(SimpleTestCase):
    def test_the_label_says_not_assessed(self):
        self.assertEqual(_public_verdict_object("not_assessed")["label"], "Not assessed")

    def test_the_message_does_not_judge_the_pick(self):
        message = _public_verdict_object("not_assessed", submitted_market="Corners Over 7.5")["message"]

        self.assertIn("could not assess", message)
        self.assertNotIn("risky", message.lower())

    def test_an_unassessed_leg_carries_unknown_risk(self):
        self.assertEqual(_public_selection_risk("not_assessed", {"score": None}), "unknown")

    def test_a_leg_with_no_score_is_never_reported_as_low_risk(self):
        # "Low" would imply a safety we never established.
        self.assertEqual(_public_selection_risk(None, {"score": None, "status": ""}), "unknown")

    def test_a_scored_strong_leg_is_still_low_risk(self):
        self.assertEqual(_public_selection_risk("keep", {"score": 85, "status": "strong"}), "low")

    def test_a_scored_weak_leg_is_still_high_risk(self):
        self.assertEqual(_public_selection_risk("remove", {"score": 40, "status": "avoid"}), "high")
