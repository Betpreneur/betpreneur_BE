"""
Explanation layer.

The guardrail is the point: an explanation may only restate evidence the model actually
produced, and may never promise an outcome. Anything else falls back to the template.
"""

from unittest import mock

from django.test import SimpleTestCase

from apps.algo.explain import service, templates
from apps.algo.explain.validator import validate


class ValidatorTests(SimpleTestCase):
    def test_a_number_present_in_the_evidence_is_allowed(self):
        result = validate("The model puts this at about 71.5%.", {"probability": 71.5})

        self.assertTrue(result.ok)

    def test_a_probability_written_as_a_percentage_is_allowed(self):
        result = validate("About 71.5% of the time.", {"probability": 0.715})

        self.assertTrue(result.ok)

    def test_an_invented_number_is_rejected(self):
        result = validate("Chelsea have won 8 of their last 10.", {"probability": 71.5})

        self.assertFalse(result.ok)
        self.assertTrue(any("unsupported_number" in reason for reason in result.reasons))

    def test_guarantee_language_is_rejected(self):
        result = validate("This is a guaranteed winner.", {})

        self.assertFalse(result.ok)
        self.assertTrue(any("certainty_language" in reason for reason in result.reasons))

    def test_banker_language_is_rejected(self):
        self.assertFalse(validate("A banker for today.", {}).ok)

    def test_cannot_lose_language_is_rejected(self):
        self.assertFalse(validate("This one cannot lose.", {}).ok)

    def test_small_bare_integers_are_allowed_in_prose(self):
        result = validate("Both teams have scored in 2 of the sample.", {})

        self.assertTrue(result.ok)

    def test_an_empty_explanation_is_rejected(self):
        self.assertFalse(validate("", {}).ok)

    def test_numbers_nested_in_the_evidence_are_found(self):
        result = validate(
            "It expects around 1.55 goals for the home side.",
            {"evidence": {"expected_goals_home": 1.55}},
        )

        self.assertTrue(result.ok)


class TemplateTests(SimpleTestCase):
    def test_an_unavailable_player_is_explained_as_unpriceable(self):
        text = templates.explain_leg(
            state="assessed", assessment_type="quantitative_model",
            availability={"status": "out", "player": "S. Haller", "reason": "Knee Injury"},
        )

        self.assertIn("unavailable", text)
        self.assertIn("cannot be priced", text)

    def test_a_terminal_state_explains_where_the_leg_stopped(self):
        text = templates.explain_leg(state="unmatched", assessment_type="none")

        self.assertIn("could not find this fixture", text.lower())

    def test_a_heuristic_leg_says_no_percentage_is_shown(self):
        text = templates.explain_leg(
            state="assessed", assessment_type="heuristic", market="Home Win"
        )

        self.assertIn("rather than a modelled probability", text)

    def test_a_modelled_leg_states_the_probability_and_expected_goals(self):
        text = templates.explain_leg(
            state="assessed", assessment_type="quantitative_model", tier="strong",
            probability=71.5,
            evidence={"expected_goals_home": 1.55, "expected_goals_away": 1.15},
        )

        self.assertIn("71.5%", text)
        self.assertIn("1.55", text)

    def test_a_thin_sample_is_disclosed(self):
        text = templates.explain_leg(
            state="assessed", assessment_type="quantitative_model",
            probability=60.0, evidence={"data_quality": "limited"},
        )

        self.assertIn("thin", text)

    def test_every_template_leg_explanation_passes_validation(self):
        cases = [
            dict(state="assessed", assessment_type="quantitative_model", tier="risky",
                 probability=48.8, risk_share=27.2,
                 evidence={"expected_goals_home": 1.55, "expected_goals_away": 1.15}),
            dict(state="insufficient_data", assessment_type="none"),
            dict(state="assessed", assessment_type="heuristic", market="Home Win"),
        ]
        for case in cases:
            text = templates.explain_leg(**case)
            evidence = {**case, **(case.get("evidence") or {})}
            self.assertTrue(validate(text, evidence).ok, f"{text} / {validate(text, evidence).reasons}")

    def test_ticket_summary_always_states_these_are_estimates(self):
        text = templates.explain_ticket(success_percent=7.14, assessed_legs=2, excluded_legs=3)

        self.assertIn("not predictions", text)

    def test_ticket_summary_reports_an_unassessable_ticket(self):
        text = templates.explain_ticket(success_percent=None)

        self.assertIn("no estimate", text)

    def test_prior_calibration_is_disclosed_in_the_ticket_summary(self):
        text = templates.explain_ticket(success_percent=10.0, calibration={"basis": "prior"})

        self.assertIn("conservative prior", text)


class ServiceTests(SimpleTestCase):
    def _card(self):
        return {
            "state": "assessed",
            "assessment": {"type": "quantitative_model", "market_family": "total_goals"},
            "risk_tier": {"code": "strong", "estimated_success_percent": 71.5, "risk_share_percent": 15.4},
            "your_pick": {"market": "Over 2.5"},
            "technical_ref": {},
        }

    def test_the_template_is_used_when_no_model_is_enabled(self):
        explanation = service.explain_leg(self._card())

        self.assertEqual(explanation.source, "template")
        self.assertIn("71.5%", explanation.text)

    def test_a_valid_model_rephrase_is_used(self):
        with mock.patch.object(service, "_llm_enabled", return_value=True), \
             mock.patch.object(service, "_rephrase", return_value="The model puts this at about 71.5%."):
            explanation = service.explain_leg(self._card())

        self.assertEqual(explanation.source, "model")

    def test_a_rephrase_that_invents_a_number_is_discarded(self):
        with mock.patch.object(service, "_llm_enabled", return_value=True), \
             mock.patch.object(service, "_rephrase", return_value="They have won 9 of their last 11 games."):
            explanation = service.explain_leg(self._card())

        self.assertEqual(explanation.source, "template")
        self.assertFalse(explanation.validation["ok"])

    def test_a_rephrase_promising_an_outcome_is_discarded(self):
        with mock.patch.object(service, "_llm_enabled", return_value=True), \
             mock.patch.object(service, "_rephrase", return_value="This is a guaranteed winner."):
            explanation = service.explain_leg(self._card())

        self.assertEqual(explanation.source, "template")

    def test_a_model_failure_falls_back_to_the_template(self):
        with mock.patch.object(service, "_llm_enabled", return_value=True), \
             mock.patch.object(service, "_rephrase", return_value=None):
            explanation = service.explain_leg(self._card())

        self.assertEqual(explanation.source, "template")
        self.assertTrue(explanation.text)
