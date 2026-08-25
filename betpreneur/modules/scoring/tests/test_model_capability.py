"""Model-backed capability.

Split out of catalog's hydration-planner tests: capability depends on whether a
model is fitted, which is scoring's knowledge, so catalog cannot test it.
"""
from django.test import SimpleTestCase

from betpreneur.modules.scoring.services.capability import model_backed_capability


class ModelBackedCapabilityTests(SimpleTestCase):
    def test_a_fitted_market_is_scoreable_without_any_snapshots(self):
        capability = model_backed_capability("match_result", "strong")

        self.assertTrue(capability["scoreable"])
        self.assertEqual(capability["coverage_percent"], 100.0)
        self.assertEqual(capability["required_snapshots"], [])

    def test_confidence_is_capped_below_the_snapshot_era_ceiling(self):
        # No xG exists on StatPal, so a fitted model is shots-informed at best.
        self.assertLess(model_backed_capability("match_result", "strong")["confidence_cap"], 88)

    def test_absence_of_expected_goals_is_declared_only_for_unfitted_model(self):
        capability = model_backed_capability("btts", "strong")

        self.assertNotIn("no_expected_goals_available", capability["warnings"])

        capability = model_backed_capability("btts", "poor")

        self.assertIn("no_expected_goals_available", capability["warnings"])

    def test_an_unfitted_league_is_not_scoreable(self):
        capability = model_backed_capability("match_result", "poor")

        self.assertFalse(capability["scoreable"])
        self.assertEqual(capability["confidence_cap"], 0)
