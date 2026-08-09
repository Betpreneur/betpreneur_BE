"""
Per-review hydration: fetch once per fixture, and not at all when the model serves it.
"""

from unittest import mock

from django.test import SimpleTestCase

from apps.algo.data.planner import (
    FixtureHydrator,
    model_backed_capability,
    plan_slip_hydration,
    snapshots_for_family,
)
from apps.algo.market_taxonomy import describe_market


class SnapshotRequirementTests(SimpleTestCase):
    def test_score_matrix_families_request_statpal_fallback_snapshots(self):
        for family in [
            "match_result",
            "double_chance",
            "draw_no_bet",
            "btts",
            "result_btts",
            "clean_sheet",
            "result_total_goals",
            "total_btts",
            "double_chance_btts",
            "double_chance_total_goals",
            "result_or_total_goals",
            "result_or_btts",
            "result_or_clean_sheet",
            "odd_even",
            "asian_handicap",
            "handicap",
            "first_to_score",
            "total_goals",
            "team_total_goals",
        ]:
            self.assertIn("team_stats", snapshots_for_family(family), family)

    def test_specialised_families_still_need_snapshots(self):
        self.assertTrue(snapshots_for_family("cards_total"))
        self.assertTrue(snapshots_for_family("player_goal"))

    def test_unmodelled_family_needs_nothing(self):
        self.assertEqual(snapshots_for_family("correct_score"), [])


class HydratorTests(SimpleTestCase):
    def setUp(self):
        self.service = mock.Mock()
        self.service.prepare_fixture_context_for_market.return_value = {
            "context": {"snapshots": {"team_stats": {}}}, "refreshed": {},
        }
        self.service.refresh_fixture_team_stats.return_value = {
            "api_usage": {"attempted_calls": 2},
            "refreshed": [{"side": "home"}, {"side": "away"}],
        }
        self.service.fixture_context.return_value = {
            "snapshots": {"team_stats": {"summary": {"team_count": 2}}},
        }
        self.hydrator = FixtureHydrator(snapshot_service=self.service)

    def test_score_matrix_family_can_fetch_statpal_fallback_context(self):
        self.hydrator.bundle_for(describe_market("Home Win"), match_id="1")

        self.service.prepare_fixture_context_for_market.assert_called_once()
        self.assertEqual(self.hydrator.stats.calls_used, 1)

    def test_specialised_family_calls_the_provider_once(self):
        descriptor = describe_market("Cards Over 3.5")

        self.hydrator.bundle_for(descriptor, match_id="1")

        self.assertEqual(self.service.prepare_fixture_context_for_market.call_count, 1)
        self.assertEqual(self.hydrator.stats.calls_used, 1)

    def test_repeat_legs_on_one_fixture_reuse_the_first_fetch(self):
        descriptor = describe_market("Cards Over 3.5")

        for _ in range(5):
            self.hydrator.bundle_for(descriptor, match_id="1")

        self.assertEqual(self.service.prepare_fixture_context_for_market.call_count, 1)
        self.assertEqual(self.hydrator.stats.served_from_cache, 4)

    def test_different_fixtures_are_fetched_separately(self):
        descriptor = describe_market("Cards Over 3.5")

        self.hydrator.bundle_for(descriptor, match_id="1")
        self.hydrator.bundle_for(descriptor, match_id="2")

        self.assertEqual(self.service.prepare_fixture_context_for_market.call_count, 2)
        self.assertEqual(len(self.hydrator.stats.fixtures_hydrated), 2)

    def test_budget_stops_further_calls_and_is_reported(self):
        hydrator = FixtureHydrator(call_budget=1, snapshot_service=self.service)
        descriptor = describe_market("Cards Over 3.5")

        hydrator.bundle_for(descriptor, match_id="1")
        hydrator.bundle_for(descriptor, match_id="2")

        self.assertEqual(self.service.prepare_fixture_context_for_market.call_count, 1)
        self.assertTrue(hydrator.stats.budget_exhausted)

    def test_a_fifty_leg_same_fixture_slip_costs_one_fetch(self):
        cards = describe_market("Cards Over 3.5")
        result = describe_market("Home Win")

        for _ in range(25):
            self.hydrator.bundle_for(cards, match_id="1")
            self.hydrator.bundle_for(result, match_id="1")

        self.assertEqual(self.service.prepare_fixture_context_for_market.call_count, 2)
        self.assertEqual(self.hydrator.stats.served_from_cache, 48)

    def test_team_stats_requirement_fetches_home_and_away_profiles(self):
        descriptor = describe_market("Corners Over 9.5")

        bundle = self.hydrator.bundle_for(
            descriptor,
            match_id="statpal:match-1",
            provider_match_id="match-1",
            provider_competition_id="3037",
            home_team_id="home-1",
            away_team_id="away-1",
        )

        self.service.refresh_fixture_team_stats.assert_called_once_with(
            match_id="statpal:match-1",
            provider_match_id="match-1",
            provider_competition_id="3037",
            home_team_id="home-1",
            away_team_id="away-1",
        )
        self.service.fixture_context.assert_called_once_with(
            match_id="statpal:match-1",
            provider_match_id="match-1",
        )
        self.assertEqual(bundle["context"]["snapshots"]["team_stats"]["summary"]["team_count"], 2)
        self.assertEqual(self.hydrator.stats.calls_used, 3)


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


class PlanTests(SimpleTestCase):
    def _selection(self, event_id, family):
        return {"provider_payload": {
            "provider_event_id": event_id,
            "market_taxonomy": {"family": family},
        }}

    def test_plan_counts_fixtures_the_model_can_serve_alone(self):
        plan = plan_slip_hydration([
            self._selection("sr:match:1", "match_result"),
            self._selection("sr:match:1", "total_goals"),
            self._selection("sr:match:2", "cards_total"),
        ])

        self.assertEqual(plan["legs"], 3)
        self.assertEqual(plan["distinct_fixtures"], 2)
        self.assertEqual(plan["fixtures_served_by_model"], 0)
        self.assertEqual(plan["fixtures_needing_snapshots"], 2)

    def test_a_wholly_score_matrix_slip_plans_fallback_calls(self):
        plan = plan_slip_hydration([
            self._selection(f"sr:match:{index}", "match_result") for index in range(20)
        ])

        self.assertEqual(plan["fixtures_needing_snapshots"], 20)
        self.assertEqual(plan["fixtures_served_by_model"], 0)
        self.assertEqual(plan["estimated_snapshot_calls"], 60)
