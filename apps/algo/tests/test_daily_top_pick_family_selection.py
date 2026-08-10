from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from apps.algo.services import AlgoRunnerService


class DailyTopPickFamilySelectionTests(SimpleTestCase):
    def test_default_family_limit_scales_with_daily_card_size(self):
        service = AlgoRunnerService()

        self.assertEqual(service._family_daily_limit("total_goals", 5), 2)
        self.assertEqual(service._family_daily_limit("total_goals", 15), 6)

    @override_settings(GRIND_ALGO={"ALGO_MAX_DAILY_SAME_MARKET_FAMILY_PICKS": "3"})
    def test_configured_family_limit_overrides_default(self):
        service = AlgoRunnerService()

        self.assertEqual(service._family_daily_limit("total_goals", 15), 3)

    @override_settings(GRIND_ALGO={"ALGO_MAX_DAILY_FAMILY_TOTAL_GOALS_PICKS": "4"})
    def test_specific_family_limit_overrides_global_limit(self):
        service = AlgoRunnerService()

        self.assertEqual(service._family_daily_limit("total_goals", 15), 4)

    @override_settings(GRIND_ALGO={"ALGO_MARKET_FAMILY_OVERFLOW_MIN_CONFIDENCE": "82"})
    def test_family_overflow_requires_strong_candidate(self):
        service = AlgoRunnerService()

        self.assertFalse(service._prediction_can_overflow_family(SimpleNamespace(confidence=81)))
        self.assertTrue(service._prediction_can_overflow_family(SimpleNamespace(confidence=82)))

    def test_prediction_family_comes_from_insights_route(self):
        service = AlgoRunnerService()
        prediction = SimpleNamespace(
            market="Over 2.5",
            insights={"daily_evaluation_route": {"family": "total_goals"}},
        )

        self.assertEqual(service._prediction_market_family(prediction), "total_goals")

    def test_prediction_family_falls_back_to_catalog(self):
        service = AlgoRunnerService()
        prediction = SimpleNamespace(market="Home Win", insights={})

        self.assertEqual(service._prediction_market_family(prediction), "match_result")

    @override_settings(GRIND_ALGO={"ALGO_DAILY_OPTIMIZATION_MODE": "safer"})
    def test_daily_optimization_mode_reads_setting(self):
        service = AlgoRunnerService()

        self.assertEqual(service._daily_optimization_mode(), "safer")

    @override_settings(GRIND_ALGO={"ALGO_DAILY_OPTIMIZATION_MODE": "wild"})
    def test_daily_optimization_mode_falls_back_to_balanced(self):
        service = AlgoRunnerService()

        self.assertEqual(service._daily_optimization_mode(), "balanced")

    def test_prediction_optimization_profile_classifies_safer_pick(self):
        service = AlgoRunnerService()
        prediction = SimpleNamespace(confidence=82, ev=0.04, odds=1.6, risk_flags=[])

        profile = service._prediction_optimization_profile(prediction)

        self.assertEqual(profile["mode"], "safer")
        self.assertEqual(profile["label"], "Safer")

    def test_prediction_optimization_profile_classifies_value_pick(self):
        service = AlgoRunnerService()
        prediction = SimpleNamespace(confidence=72, ev=0.12, odds=2.2, risk_flags=[])

        profile = service._prediction_optimization_profile(prediction)

        self.assertEqual(profile["mode"], "value")
        self.assertEqual(profile["label"], "Value")

    def test_prediction_optimization_profile_classifies_balanced_pick(self):
        service = AlgoRunnerService()
        prediction = SimpleNamespace(confidence=68, ev=0.04, odds=1.8, risk_flags=[])

        profile = service._prediction_optimization_profile(prediction)

        self.assertEqual(profile["mode"], "balanced")
        self.assertEqual(profile["label"], "Balanced")

    @override_settings(GRIND_ALGO={"ALGO_DAILY_OPTIMIZATION_MODE": "value"})
    def test_mode_filter_matches_configured_value_mode(self):
        service = AlgoRunnerService()
        value_pick = SimpleNamespace(confidence=72, ev=0.12, odds=2.2, risk_flags=[])
        safer_pick = SimpleNamespace(confidence=84, ev=0.04, odds=1.55, risk_flags=[])

        self.assertTrue(service._prediction_matches_optimization_mode(value_pick, service._daily_optimization_mode()))
        self.assertFalse(service._prediction_matches_optimization_mode(safer_pick, service._daily_optimization_mode()))

    def test_pick_optimization_counts_use_persisted_profile(self):
        service = AlgoRunnerService()
        picks = [
            SimpleNamespace(insights={"optimization_profile": {"mode": "safer"}}),
            SimpleNamespace(insights={"optimization_profile": {"mode": "value"}}),
            SimpleNamespace(insights={"optimization_profile": {"mode": "value"}}),
        ]

        self.assertEqual(service._pick_optimization_counts(picks), {"safer": 1, "value": 2})
