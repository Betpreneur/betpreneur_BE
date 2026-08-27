from dataclasses import replace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from betpreneur.modules.markets.api import describe_market
from betpreneur.modules.pricing.api import (
    market_profile_fit_score,
    with_market_capability,
    with_statpal_advisory,
)
from betpreneur.modules.scoring.api import ScoreMatrix, TeamRateProfile
from betpreneur.modules.slips.domain.slip_analysis import (
    _blocked_slip_recommendation_market,
    _consume_review_force_fresh,
    _generated_market_names_for_family,
    _manual_verdict,
    _market_can_skip_core_on_demand,
    _public_market_pick,
    _submitted_market_payload,
    analysis_data_fallback_state,
)
from betpreneur.modules.slips.interface.views import _generated_match_checker_markets
from betpreneur.modules.slips.services.slip_presentation import (
    _replacement_market_for_slip,
    _stats_backed_evidence,
)


class UserPickAdvisoryTests(SimpleTestCase):
    def test_review_force_fresh_is_consumed_once(self):
        context = {"fixture_universe_synced": False}

        self.assertTrue(_consume_review_force_fresh(context))
        self.assertFalse(_consume_review_force_fresh(context))

    def test_matrix_and_count_markets_skip_core_on_demand(self):
        self.assertTrue(_market_can_skip_core_on_demand(describe_market("Home Win")))
        self.assertTrue(_market_can_skip_core_on_demand(describe_market("Cards Over 3.5")))
        self.assertTrue(_market_can_skip_core_on_demand(describe_market("Home Team Shots on Target Over 9.5")))
        self.assertFalse(_market_can_skip_core_on_demand(describe_market("Correct Score 2-1")))

    def test_submitted_market_score_is_capped_by_capability(self):
        submitted = _submitted_market_payload(
            requested_market="Cards Over 3.5",
            market_taxonomy={"family": "cards_total"},
            statpal_advisory={
                "available": True,
                "score": 82,
                "status": "strong",
                "basis": "statpal_cards",
                "warnings": [],
                "evidence": {},
            },
            market_capability={
                "support_level": "medium",
                "data_quality": "limited",
                "confidence_cap": 56,
                "warnings": ["low_statpal_coverage"],
            },
        )

        # The modelled probability is not truncated by data quality -- the two
        # answer different questions. Truncating collapsed distinct probabilities
        # onto the cap value, which is what produced the 64%-everywhere reviews.
        self.assertEqual(submitted["advisory_score"], 82)
        self.assertEqual(submitted["data_confidence"], 56)
        # Thin evidence still holds back what we are willing to *claim*.
        self.assertEqual(submitted["advisory_status"], "caution")
        self.assertTrue(
            submitted["advisory_evidence"]["claim_limited_by_data_quality"]
        )
        self.assertIn("low_statpal_coverage", submitted["advisory_warnings"])

    def test_scored_statpal_fallback_upgrades_stale_poor_capability(self):
        submitted = _submitted_market_payload(
            requested_market="Over 2.5",
            market_taxonomy={"family": "total_goals"},
            statpal_advisory={
                "available": True,
                "score": 91.4,
                "status": "strong",
                "basis": "statpal_goal_market_model",
                "warnings": [],
                "evidence": {"estimated_probability": 96.0},
            },
            market_capability={
                "support_level": "medium",
                "data_quality": "poor",
                "confidence_cap": 0,
                "warnings": ["thin_league_sample", "no_expected_goals_available"],
            },
        )

        self.assertEqual(submitted["advisory_score"], 91.4)
        self.assertEqual(submitted["data_confidence"], 75)
        self.assertEqual(submitted["market_capability"]["data_quality"], "medium")
        self.assertEqual(submitted["market_capability"]["confidence_cap"], 75)
        self.assertNotIn("no_expected_goals_available", submitted["market_capability"]["warnings"])

    def test_statpal_score_replaces_unscored_cached_market(self):
        market = {
            "market": "Over 1.5",
            "confidence": None,
            "final_confidence": None,
            "raw_confidence": None,
            "advisory_score": 0,
            "advisory_status": "avoid",
            "advisory_basis": "match_specific_analysis",
            "advisory_evidence": {},
        }

        upgraded = with_statpal_advisory(
            market,
            {
                "available": True,
                "score": 72,
                "status": "strong",
                "basis": "statpal_goal_market_model",
                "warnings": [],
                "evidence": {
                    "estimated_probability": 72,
                    "expected_total_goals": 2.8,
                },
            },
        )

        self.assertEqual(upgraded["advisory_score"], 72)
        self.assertEqual(upgraded["advisory_status"], "playable")
        self.assertEqual(upgraded["advisory_evidence"]["statpal_merge_mode"], "primary")

    def test_public_market_pick_exposes_confidence_score(self):
        pick = _public_market_pick({"market": "Over 1.5", "advisory_score": 68})

        self.assertEqual(pick["confidence_score"], 68)
        self.assertEqual(pick["confidence_label"], "Moderate")
        self.assertEqual(pick["score"], 68)

    def test_user_pick_is_not_replaced_unless_alternative_is_clearly_better(self):
        selected = {
            "market": "Cards Over 3.5",
            "advisory_score": 66,
            "advisory_status": "playable",
        }
        replacement = {
            "market": "Over 1.5",
            "advisory_score": 69,
            "advisory_status": "playable",
        }

        verdict = _manual_verdict(selected, replacement)

        self.assertEqual(verdict["verdict"], "caution")
        self.assertFalse(verdict["better_market_available"])

    def test_user_pick_is_replaced_when_alternative_is_clearly_better(self):
        selected = {
            "market": "Cards Over 3.5",
            "advisory_score": 50,
            "advisory_status": "avoid",
            "market_taxonomy": {"family": "cards_total"},
        }
        replacement = {
            "market": "Cards Under 5.5",
            "advisory_score": 68,
            "advisory_status": "playable",
            "market_taxonomy": {"family": "cards_total"},
            "market_capability": {"data_quality": "medium"},
            "advisory_evidence": {
                "expected_total_cards": 3.8,
                "line": 5.5,
                "selection": "under",
            },
        }

        verdict = _manual_verdict(selected, replacement)

        self.assertEqual(verdict["verdict"], "replace")
        self.assertTrue(verdict["better_market_available"])

    def test_direct_market_analysis_also_uses_capability_cap(self):
        market = with_market_capability(
            {
                "market": "Over 2.5",
                "advisory_score": 84,
                "advisory_status": "strong",
                "advisory_warnings": [],
                "advisory_evidence": {},
            },
            {
                "support_level": "full",
                "data_quality": "limited",
                "confidence_cap": 70,
                "warnings": ["missing_required_snapshots"],
            },
        )

        self.assertEqual(market["advisory_score"], 84)
        self.assertEqual(market["data_confidence"], 70)
        self.assertEqual(market["advisory_status"], "playable")
        self.assertIn("missing_required_snapshots", market["advisory_warnings"])
        self.assertTrue(
            market["advisory_evidence"]["claim_limited_by_data_quality"]
        )

    def test_replacement_prefers_same_market_family_over_higher_broad_market(self):
        selected = {
            "market": "Over 2.5",
            "advisory_score": 48,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("Over 2.5").to_dict(),
        }
        game = {
            "markets": [
                {
                    "market": "DC: 12",
                    "advisory_score": 92,
                    "final_confidence": 92,
                    "confidence": 92,
                    "council_review": {"decision": "approve"},
                    "market_taxonomy": describe_market("DC: 12").to_dict(),
                    "market_capability": {"data_quality": "medium"},
                    "advisory_evidence": {"home_win_probability": 46, "away_win_probability": 46},
                },
                {
                    "market": "Over 1.5",
                    "advisory_score": 78,
                    "final_confidence": 78,
                    "confidence": 78,
                    "council_review": {"decision": "approve"},
                    "market_taxonomy": describe_market("Over 1.5").to_dict(),
                    "market_capability": {"data_quality": "medium"},
                    "advisory_evidence": {"expected_goals": 2.9, "line": 1.5, "selection": "over"},
                },
            ]
        }

        replacement = _replacement_market_for_slip(game, selected_market=selected)

        self.assertEqual(replacement["market"], "Over 1.5")
        self.assertEqual(replacement["replacement_scope"], "comparable_market")

    def test_result_replacement_preserves_selected_team_thesis(self):
        selected = {
            "market": "Away Win",
            "advisory_score": 35,
            "advisory_status": "avoid",
            "market_taxonomy": {"family": "match_result", "side": "away"},
        }
        generated = [
            {
                "market": "DC: 1X",
                "advisory_score": 78,
                "market_taxonomy": {"family": "double_chance", "side": "home_or_draw"},
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {"home_win_probability": 45, "draw_probability": 33},
            },
            {
                "market": "DC: 12",
                "advisory_score": 76,
                "market_taxonomy": {"family": "double_chance", "side": "home_or_away"},
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {"home_win_probability": 45, "away_win_probability": 31},
            },
            {
                "market": "DC: X2",
                "advisory_score": 66,
                "market_taxonomy": {"family": "double_chance", "side": "draw_or_away"},
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {"draw_probability": 33, "away_win_probability": 33},
            },
        ]

        replacement = _replacement_market_for_slip({"markets": []}, selected_market=selected, generated_markets=generated)

        self.assertEqual(replacement["market"], "DC: X2")

    def test_result_replacement_does_not_flip_home_win_to_x2(self):
        selected = {
            "market": "Home Win",
            "advisory_score": 35,
            "advisory_status": "avoid",
            "market_taxonomy": {"family": "match_result", "side": "home"},
        }
        generated = [
            {
                "market": "DC: X2",
                "advisory_score": 80,
                "market_taxonomy": {"family": "double_chance", "side": "draw_or_away"},
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {"draw_probability": 34, "away_win_probability": 46},
            },
            {
                "market": "DC: 1X",
                "advisory_score": 65,
                "market_taxonomy": {"family": "double_chance", "side": "home_or_draw"},
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {"home_win_probability": 34, "draw_probability": 31},
            },
        ]

        replacement = _replacement_market_for_slip({"markets": []}, selected_market=selected, generated_markets=generated)

        self.assertEqual(replacement["market"], "DC: 1X")

    def test_conflicted_result_model_does_not_use_broad_goal_fallback(self):
        selected = {
            "market": "Home Win",
            "advisory_score": 27,
            "advisory_status": "avoid",
            "advisory_warnings": ["result_model_market_disagreement"],
            "market_taxonomy": {"family": "match_result", "side": "home"},
        }
        generated = [
            {
                "market": "Over 2.5",
                "advisory_score": 73,
                "advisory_status": "playable",
                "market_taxonomy": {"family": "total_goals", "side": "over", "selection": "over", "line": "2.5"},
            }
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertIsNone(replacement)

    def test_result_pick_can_use_goal_market_broad_fallback_when_supported(self):
        selected = {
            "market": "Away Win",
            "advisory_score": 29,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("Away Win").to_dict(),
        }
        generated = [
            {
                "market": "Over 2.5",
                "advisory_score": 73,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("Over 2.5").to_dict(),
                "advisory_evidence": {
                    "expected_goals": 3.2,
                    "line": 2.5,
                    "selection": "over",
                },
            }
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertEqual(replacement["market"], "Over 2.5")
        self.assertEqual(replacement["recommendation_strength"], "safer_alternative")

    def test_stage8_home_win_can_use_goal_market_when_goal_evidence_is_strong(self):
        selected = {
            "market": "Home Win",
            "advisory_score": 31,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("Home Win").to_dict(),
        }
        generated = [
            {
                "market": "Over 2.5",
                "advisory_score": 74,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("Over 2.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {
                    "expected_goals": 3.45,
                    "line": 2.5,
                    "selection": "over",
                },
            }
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertEqual(replacement["market"], "Over 2.5")

    def test_stage8_home_win_can_use_corner_market_when_corner_evidence_is_strong(self):
        selected = {
            "market": "Home Win",
            "advisory_score": 32,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("Home Win").to_dict(),
        }
        generated = [
            {
                "market": "Over 2.5",
                "advisory_score": 62,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("Over 2.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {
                    "expected_goals": 2.7,
                    "line": 2.5,
                    "selection": "over",
                },
            },
            {
                "market": "Corners Over 8.5",
                "advisory_score": 76,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("Corners Over 8.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {
                    "expected_total_corners": 11.2,
                    "line": 8.5,
                    "selection": "over",
                },
            },
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertEqual(replacement["market"], "Corners Over 8.5")

    def test_stage10_team_intelligence_feeds_market_fit_score(self):
        market = {
            "market": "Over 2.5",
            "advisory_evidence": {"team_intelligence_fit_score": 84.4},
        }

        self.assertEqual(market_profile_fit_score(market), 84.4)

    def test_stage10_team_intelligence_can_change_replacement_ranking(self):
        selected = {
            "market": "Home Win",
            "advisory_score": 31,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("Home Win").to_dict(),
        }
        game = {
            "markets": [],
            "team_intelligence": {
                "available": True,
                "home": {
                    "market_profiles": [
                        {
                            "market_family": "total_goals",
                            "market": "Over 2.5",
                            "attempts": 18,
                            "hit_rate": 88,
                            "confidence": 82,
                            "data_quality": "strong",
                        },
                        {
                            "market_family": "total_goals",
                            "market": "Under 4.5",
                            "attempts": 18,
                            "hit_rate": 42,
                            "confidence": 44,
                            "data_quality": "limited",
                        },
                    ],
                },
                "away": {"market_profiles": []},
                "league": {"market_profiles": []},
            },
        }
        generated = [
            {
                "market": "Under 4.5",
                "advisory_score": 75,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("Under 4.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {
                    "expected_goals": 2.4,
                    "line": 4.5,
                    "selection": "under",
                },
            },
            {
                "market": "Over 2.5",
                "advisory_score": 70,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("Over 2.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {
                    "expected_goals": 3.4,
                    "line": 2.5,
                    "selection": "over",
                },
            },
        ]

        replacement = _replacement_market_for_slip(
            game,
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertEqual(replacement["market"], "Over 2.5")
        self.assertEqual(replacement["advisory_evidence"]["team_intelligence_source"], "stored_home_team_market_profile")

    def test_stage11_fresh_team_intelligence_is_primary_data_source(self):
        state = analysis_data_fallback_state(
            {
                "available": True,
                "status": "available",
                "home": {"coverage": {"status": "fresh"}},
                "away": {"coverage": {"status": "fresh"}},
                "league": {"coverage": {"status": "fresh"}, "market_profiles": [{"market": "Over 2.5"}]},
                "missing": [],
            },
            {"snapshots": {"detailed_stats": {"summary": {"matches": 12}}}},
        )

        self.assertEqual(state["primary"], "team_intelligence")
        self.assertTrue(state["provider_snapshots_available"])
        self.assertTrue(state["league_priors_available"])

    def test_stage11_stale_team_intelligence_falls_back_to_provider_snapshots(self):
        state = analysis_data_fallback_state(
            {
                "available": True,
                "status": "available",
                "home": {"coverage": {"status": "stale"}},
                "away": {"coverage": {"status": "fresh"}},
                "league": {"coverage": {"status": "fresh"}, "market_profiles": [{"market": "Over 2.5"}]},
                "missing": [],
            },
            {"snapshots": {"prematch_odds": {"payload": {"markets": []}}}},
        )

        self.assertEqual(state["primary"], "provider_snapshots")
        self.assertIn("team_intelligence_stale", state["warnings"])

    def test_stage11_provider_snapshot_failure_falls_back_to_league_priors(self):
        state = analysis_data_fallback_state(
            {
                "available": False,
                "status": "missing",
                "home": None,
                "away": None,
                "league": {
                    "coverage": {"status": "fresh"},
                    "market_profiles": [{"market_family": "total_goals", "market": "Over 2.5"}],
                },
                "missing": ["home_team_profile", "away_team_profile"],
            },
            {"snapshots": {}},
        )

        self.assertEqual(state["primary"], "league_priors")
        self.assertIn("team_intelligence_missing", state["warnings"])
        self.assertIn("provider_snapshots_missing", state["warnings"])

    def test_overall_corner_recommendation_does_not_use_unbookable_low_line(self):
        selected = {
            "market": "Home Win",
            "advisory_score": 28,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("Home Win").to_dict(),
        }
        generated = [
            {
                "market": "Corners Over 2.5",
                "advisory_score": 89,
                "advisory_status": "strong",
                "market_taxonomy": describe_market("Corners Over 2.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {
                    "expected_total_corners": 5.2,
                    "line": 2.5,
                    "selection": "over",
                },
            },
            {
                "market": "Corners Over 7.5",
                "advisory_score": 72,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("Corners Over 7.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {
                    "expected_total_corners": 9.4,
                    "line": 7.5,
                    "selection": "over",
                },
            },
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertEqual(replacement["market"], "Corners Over 7.5")

    def test_team_corner_recommendation_can_use_low_line_when_team_is_explicit(self):
        selected = {
            "market": "Home Win",
            "advisory_score": 28,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("Home Win").to_dict(),
        }
        generated = [
            {
                "market": "Home Team Corners Over 2.5",
                "advisory_score": 78,
                "advisory_status": "strong",
                "market_taxonomy": describe_market("Home Team Corners Over 2.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {
                    "home_expected_corners": 5.2,
                    "line": 2.5,
                    "selection": "over",
                },
            }
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertEqual(replacement["market"], "Home Team Corners Over 2.5")

    def test_stage8_home_win_goal_replacement_needs_fixture_specific_evidence(self):
        selected = {
            "market": "Home Win",
            "advisory_score": 31,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("Home Win").to_dict(),
        }
        generated = [
            {
                "market": "Over 2.5",
                "advisory_score": 74,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("Over 2.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {
                    "historical_accuracy": 58.4,
                    "sample_size": 933,
                    "similar_market_roi": 6.3,
                },
            }
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertIsNone(replacement)

    def test_stage8_away_win_prefers_dnb_away_when_cross_family_is_only_close(self):
        selected = {
            "market": "Away Win",
            "advisory_score": 29,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("Away Win").to_dict(),
        }
        generated = [
            {
                "market": "DNB Away",
                "advisory_score": 66,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("DNB Away").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {"away_win_probability": 43, "draw_probability": 23},
            },
            {
                "market": "Over 2.5",
                "advisory_score": 74,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("Over 2.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {
                    "expected_goals": 3.0,
                    "line": 2.5,
                    "selection": "over",
                },
            },
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertEqual(replacement["market"], "DNB Away")

    def test_result_pick_does_not_use_broad_shots_or_cards_under_fallbacks(self):
        selected = {
            "market": "Home Win",
            "advisory_score": 28,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("Home Win").to_dict(),
        }
        generated = [
            {
                "market": "Shots On Target Under 10.5",
                "advisory_score": 99,
                "advisory_status": "strong",
                "market_taxonomy": describe_market("Shots On Target Under 10.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {
                    "expected_shots_on_target": 4.9,
                    "line": 10.5,
                    "selection": "under",
                },
            },
            {
                "market": "Cards Under 5.5",
                "advisory_score": 91,
                "advisory_status": "strong",
                "market_taxonomy": describe_market("Cards Under 5.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {
                    "expected_total_cards": 3.0,
                    "line": 5.5,
                    "selection": "under",
                },
            },
            {
                "market": "Over 2.5",
                "advisory_score": 73,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("Over 2.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {
                    "expected_goals": 3.4,
                    "line": 2.5,
                    "selection": "over",
                },
            },
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertEqual(replacement["market"], "Over 2.5")

    def test_team_goal_replacement_cannot_flip_to_opponent_under(self):
        selected = {
            "market": "Home Team Over 0.5",
            "advisory_score": 54,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("Home Team Goals Over 0.5").to_dict(),
        }
        generated = [
            {
                "market": "Away Team Under 2.5",
                "advisory_score": 94,
                "advisory_status": "strong",
                "market_taxonomy": describe_market("Away Team Under 2.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {
                    "expected_team_goals": 0.3,
                    "line": 2.5,
                    "selection": "under",
                },
            },
            {
                "market": "Home Team Over 1.5",
                "advisory_score": 68,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("Home Team Over 1.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {
                    "expected_team_goals": 1.9,
                    "line": 1.5,
                    "selection": "over",
                },
            },
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertEqual(replacement["market"], "Home Team Over 1.5")

    def test_total_goal_replacement_cannot_flip_over_pick_to_broad_under(self):
        selected = {
            "market": "Over 1.5",
            "advisory_score": 54,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("Over 1.5").to_dict(),
        }
        generated = [
            {
                "market": "Under 4.5",
                "advisory_score": 92,
                "advisory_status": "strong",
                "market_taxonomy": describe_market("Under 4.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {
                    "expected_goals": 2.8,
                    "line": 4.5,
                    "selection": "under",
                },
            },
            {
                "market": "Over 2.5",
                "advisory_score": 69,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("Over 2.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {
                    "expected_goals": 3.1,
                    "line": 2.5,
                    "selection": "over",
                },
            },
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertEqual(replacement["market"], "Over 2.5")

    def test_result_pick_generates_fixture_wide_modelled_candidates(self):
        def evaluate_market(descriptor, **kwargs):
            if descriptor.canonical not in {"Over 2.5", "Corners Over 8.5"}:
                return {"available": False, "score": None}
            return {
                "available": True,
                "score": 72,
                "status": "playable",
                "basis": "test_model",
                "warnings": [],
                "evidence": {"line": descriptor.line, "selection": descriptor.selection},
            }

        with patch("betpreneur.modules.slips.interface.views.capability_for_descriptor", return_value={"data_quality": "medium"}), patch(
            "betpreneur.modules.pricing.api.statpal_market_advisory.evaluate_market", side_effect=evaluate_market
        ), patch("betpreneur.modules.pricing.api.statpal_market_advisory.reference_price", return_value={}):
            generated = _generated_match_checker_markets(
                describe_market("Away Win"),
                game={},
                statpal_context={
                    "snapshots": {
                        "detailed_stats": {
                            "summary": {
                                "home_corners": 5,
                                "away_corners": 4,
                            }
                        }
                    }
                },
            )

        by_market = {market["market"]: market for market in generated}
        self.assertIn("Over 2.5", by_market)
        self.assertIn("Corners Over 8.5", by_market)
        self.assertEqual(by_market["Over 2.5"]["generated_source"], "fixture_wide_market_pool")
        self.assertEqual(by_market["Corners Over 8.5"]["generated_source"], "fixture_wide_market_pool")

    def test_result_pick_can_use_thesis_preserving_result_repair(self):
        selected = {
            "market": "Away Win",
            "advisory_score": 29,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("Away Win").to_dict(),
        }
        generated = [
            {
                "market": "DNB Away",
                "advisory_score": 66,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("DNB Away").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {"away_win_probability": 43, "draw_probability": 23},
            }
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertEqual(replacement["market"], "DNB Away")

    def test_priced_early_payout_result_is_not_replaced_by_lower_value_dnb(self):
        selected = _submitted_market_payload(
            requested_market="Home Win 1UP",
            market_taxonomy=describe_market("Home Win 1UP").to_dict(),
            statpal_advisory={
                "available": True,
                "score": 66,
                "status": "modelled",
                "basis": "score_matrix",
                "warnings": [],
                "evidence": {"home_win_probability": 66, "edge_points": 10},
            },
            market_capability={"data_quality": "medium", "confidence_cap": 75, "warnings": []},
            odds=1.31,
        )
        generated = [
            {
                "market": "DNB Home",
                "odds": 1.08,
                "advisory_score": 71,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("DNB Home").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {"home_win_probability": 55, "draw_probability": 20, "edge_points": 12},
            }
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertIsNone(replacement)

    def test_replacement_candidate_must_have_model_probability(self):
        selected = {
            "market": "Home Win",
            "advisory_score": 40,
            "advisory_status": "avoid",
            "market_taxonomy": {"family": "match_result", "side": "home"},
        }
        generated = [
            {
                "market": "Over 2.5",
                "advisory_score": None,
                "final_confidence": None,
                "confidence": None,
                "advisory_status": "playable",
                "market_taxonomy": {"family": "total_goals"},
            }
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertIsNone(replacement)

    def test_analysed_weak_pick_uses_supported_fit_even_without_large_lift(self):
        selected = {
            "market": "Over 2.5",
            "advisory_score": 54,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("Over 2.5").to_dict(),
        }
        generated = [
            {
                "market": "Over 1.5",
                "advisory_score": 56,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("Over 1.5").to_dict(),
                "advisory_evidence": {
                    "expected_goals": 2.4,
                    "line": 1.5,
                    "selection": "over",
                },
            }
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertEqual(replacement["market"], "Over 1.5")
        self.assertEqual(replacement["recommendation_strength"], "best_fit_alternative")

    def test_replacement_never_recommends_easy_over_half_goal_market(self):
        selected = {
            "market": "Over 2.5",
            "advisory_score": 48,
            "advisory_status": "avoid",
            "market_taxonomy": {"family": "total_goals"},
        }
        generated = [
            {
                "market": "Over 0.5",
                "advisory_score": 96,
                "advisory_status": "strong",
                "market_taxonomy": {"family": "total_goals"},
            },
            {
                "market": "Under 3.5",
                "advisory_score": 70,
                "advisory_status": "playable",
                "market_taxonomy": {"family": "total_goals"},
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {
                    "expected_goals": 2.4,
                    "line": 3.5,
                    "selection": "under",
                },
            },
        ]
        blocked = []

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
            blocked_markets_out=blocked,
        )

        self.assertEqual(replacement["market"], "Under 3.5")
        self.assertEqual(blocked, ["Over 0.5"])

    def test_replacement_candidate_requires_strict_model_eligibility(self):
        selected = {
            "market": "Home Win",
            "advisory_score": 42,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("Home Win").to_dict(),
        }
        generated = [
            {
                "market": "Over 2.5",
                "advisory_score": None,
                "market_taxonomy": describe_market("Over 2.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {"expected_goals": 3.1, "line": 2.5, "selection": "over"},
            },
            {
                "market": "Over 3.5",
                "advisory_score": 72,
                "market_taxonomy": describe_market("Over 3.5").to_dict(),
                "market_capability": {"data_quality": "poor"},
                "advisory_evidence": {"expected_goals": 4.1, "line": 3.5, "selection": "over"},
            },
            {
                "market": "GG / BTTS Yes",
                "advisory_score": 70,
                "market_taxonomy": describe_market("GG / BTTS Yes").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {},
            },
            {
                "market": "Away Win",
                "advisory_score": 69,
                "advisory_warnings": ["result_model_market_disagreement"],
                "market_taxonomy": describe_market("Away Win").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {"away_win_probability": 69},
            },
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertIsNone(replacement)

    def test_market_fit_scoring_uses_family_specific_profiles(self):
        self.assertGreater(
            market_profile_fit_score(
                {
                    "market": "Over 2.5",
                    "market_taxonomy": describe_market("Over 2.5").to_dict(),
                    "advisory_evidence": {"expected_goals": 3.4, "line": 2.5, "selection": "over"},
                }
            ),
            80,
        )
        self.assertLess(
            market_profile_fit_score(
                {
                    "market": "1H Over 1.5",
                    "market_taxonomy": describe_market("1H Over 1.5").to_dict(),
                    "advisory_evidence": {"first_half_expected_goals": 0.9, "line": 1.5, "selection": "over"},
                }
            ),
            50,
        )
        self.assertGreater(
            market_profile_fit_score(
                {
                    "market": "Corners Over 8.5",
                    "market_taxonomy": describe_market("Corners Over 8.5").to_dict(),
                    "advisory_evidence": {"expected_total_corners": 11.0, "line": 8.5, "selection": "over"},
                }
            ),
            70,
        )
        self.assertGreater(
            market_profile_fit_score(
                {
                    "market": "Cards Over 4.5",
                    "market_taxonomy": describe_market("Cards Over 4.5").to_dict(),
                    "advisory_evidence": {
                        "expected_total_cards": 4.6,
                        "referee_cards_per_game": 6.2,
                        "line": 4.5,
                        "selection": "over",
                    },
                }
            ),
            55,
        )
        self.assertEqual(
            market_profile_fit_score(
                {
                    "market": "DNB Away",
                    "market_taxonomy": describe_market("DNB Away").to_dict(),
                    "advisory_evidence": {"away_win_probability": 63, "draw_probability": 21},
                }
            ),
            63,
        )

    def test_generated_goal_alternatives_exclude_easy_over_half_goal_markets(self):
        total_names = set(_generated_market_names_for_family(describe_market("Over 2.5")))
        team_names = set(_generated_market_names_for_family(describe_market("Home Team Over 2.5")))
        combo_descriptor = replace(
            describe_market("Home/Draw & Over 2.5"),
            family="double_chance_total_goals",
            canonical="Home/Draw & Over 2.5",
        )
        combo_names = set(_generated_market_names_for_family(combo_descriptor))

        self.assertNotIn("Over 0.5", total_names)
        self.assertNotIn("Home Team Over 0.5", team_names)
        self.assertNotIn("Over 0.5", combo_names)
        self.assertIn("Over 1.5", total_names)
        self.assertIn("Home Team Over 1.5", team_names)
        self.assertIn("Over 1.5", combo_names)

    def test_replacement_returns_none_when_only_easy_over_half_goal_is_available(self):
        selected = {
            "market": "Over 2.5",
            "advisory_score": 48,
            "advisory_status": "avoid",
            "market_taxonomy": {"family": "total_goals"},
        }
        generated = [
            {
                "market": "Over 0.5",
                "advisory_score": 96,
                "advisory_status": "strong",
                "market_taxonomy": {"family": "total_goals"},
            },
            {
                "market": "1H Over 0.5",
                "advisory_score": 88,
                "advisory_status": "strong",
                "market_taxonomy": {"family": "total_goals"},
            },
            {
                "market": "Home Team Over 0.5",
                "advisory_score": 86,
                "advisory_status": "strong",
                "market_taxonomy": {"family": "team_total_goals"},
            },
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertIsNone(replacement)

    def test_recommendation_policy_does_not_block_handicap_plus_half(self):
        self.assertTrue(_blocked_slip_recommendation_market({"market": "Over 0.5"}))
        self.assertTrue(_blocked_slip_recommendation_market({"market": "1H Over 0.5"}))
        self.assertFalse(_blocked_slip_recommendation_market({"market": "AH Home +0.5"}))

    def test_generated_match_corner_markets_start_at_bookable_over_lines(self):
        names = set(_generated_market_names_for_family(describe_market("Corners Over 8.5")))

        self.assertNotIn("Corners Over 2.5", names)
        self.assertNotIn("Corners Over 6.5", names)
        self.assertIn("Corners Over 7.5", names)
        self.assertIn("Corners Over 8.5", names)

    def test_replacement_does_not_broad_replace_decent_specialist_pick(self):
        selected = {
            "market": "Cards Over 3.5",
            "advisory_score": 58,
            "advisory_status": "caution",
            "market_taxonomy": {"family": "cards_total"},
            "market_capability": {"data_quality": "medium"},
        }
        game = {
            "markets": [
                {"market": "Over 1.5", "final_confidence": 84, "confidence": 84, "council_review": {"decision": "approve"}},
                {"market": "DC: 12", "final_confidence": 88, "confidence": 88, "council_review": {"decision": "approve"}},
            ]
        }

        replacement = _replacement_market_for_slip(game, selected_market=selected)

        self.assertIsNone(replacement)

    def test_broad_replacement_can_be_used_as_safer_fallback_for_risky_goal_pick(self):
        selected = {
            "market": "Away Win & Over 2.5",
            "advisory_score": 52,
            "advisory_status": "avoid",
            "market_taxonomy": {"family": "result_total_goals"},
            "market_capability": {"data_quality": "limited"},
        }
        weak_upgrade = {
            "market": "Over 1.5",
            "advisory_score": 62,
            "advisory_status": "caution",
            "market_capability": {"data_quality": "medium"},
            "advisory_evidence": {"expected_goals": 2.4, "line": 1.5, "selection": "over"},
        }
        strong_upgrade = {
            "market": "Over 1.5",
            "advisory_score": 68,
            "advisory_status": "playable",
            "market_capability": {"data_quality": "medium"},
            "advisory_evidence": {"expected_goals": 2.9, "line": 1.5, "selection": "over"},
        }

        weak_verdict = _manual_verdict(selected, weak_upgrade)
        strong_verdict = _manual_verdict(selected, strong_upgrade)

        self.assertEqual(weak_verdict["verdict"], "replace")
        self.assertEqual(strong_verdict["verdict"], "replace")

    def test_specialist_pick_without_comparable_data_is_still_not_assessed_not_broad_replaced(self):
        selected = {
            "market": "Player To Score",
            "advisory_score": 52,
            "advisory_status": "avoid",
            "market_taxonomy": {"family": "player_goal"},
            "market_capability": {"data_quality": "limited"},
        }
        broad_upgrade = {
            "market": "Over 1.5",
            "advisory_score": 68,
            "advisory_status": "playable",
            "market_capability": {"data_quality": "medium"},
            "advisory_evidence": {"expected_goals": 2.8, "line": 1.5, "selection": "over"},
        }

        verdict = _manual_verdict(selected, broad_upgrade)

        self.assertEqual(verdict["verdict"], "remove")

    def test_specialist_pick_does_not_use_broad_fallback_even_when_weak(self):
        selected = {
            "market": "Cards Over 3.5",
            "advisory_score": 44,
            "advisory_status": "avoid",
            "market_taxonomy": {"family": "cards_total"},
            "market_capability": {"data_quality": "poor"},
        }
        game = {
            "markets": [
                {"market": "Over 1.5", "final_confidence": 90, "confidence": 90, "council_review": {"decision": "approve"}},
            ]
        }

        replacement = _replacement_market_for_slip(game, selected_market=selected)

        self.assertIsNone(replacement)

    def test_result_total_combo_generates_model_backed_alternatives(self):
        descriptor = replace(
            describe_market("Away Win & Over 2.5"),
            family="result_total_goals",
            canonical="Away Win & Over 2.5",
        )
        game = {
            "fixture": "Alpha vs Beta",
            "home_team": "Alpha",
            "away_team": "Beta",
            "statpal_provider_competition_id": "league-1",
            "markets": [],
        }

        with patch("betpreneur.modules.scoring.services.service.score_model_service.rates_for_fixture") as rates:
            matrix = ScoreMatrix(
                grid=(
                    (0.0, 0.0, 0.7, 0.0, 0.0, 0.0),
                    (0.1, 0.2, 0.0, 0.0, 0.0, 0.0),
                    (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                    (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                    (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                    (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                ),
                home_rate=1.4,
                away_rate=1.2,
                rho=0,
            )
            rates.return_value = type(
                "Rates",
                (),
                {
                    "usable": True,
                    # Both sides have real history, so result markets are derivable.
                    "differentiated": True,
                    "home_matches": 20,
                    "away_matches": 20,
                    "data_quality": "medium",
                    "league_id": "league-1",
                    "model_version": "test",
                    "matrix": lambda self: matrix,
                    # Reference fixture for the edge calculation: the same league with
                    # team strengths switched off.
                    "reference_matrix": lambda self: matrix,
                },
            )()
            generated = _generated_match_checker_markets(descriptor, game=game, statpal_context={})

        self.assertTrue(any(item["market"] == "Over 1.5" for item in generated))
        self.assertTrue(any(item["market"] == "Away Win" for item in generated))

    def test_replacement_selector_can_offer_safer_broad_fallback_when_enabled(self):
        selected = {
            "market": "Away Win & Over 2.5",
            "advisory_score": 32,
            "advisory_status": "avoid",
            "market_taxonomy": {"family": "result_total_goals"},
        }
        generated = [
            {
                "market": "Over 1.5",
                "advisory_score": 70,
                "advisory_status": "playable",
                "market_taxonomy": {"family": "total_goals"},
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {"expected_goals": 2.7, "line": 1.5, "selection": "over"},
            }
        ]

        default_replacement = _replacement_market_for_slip({"markets": []}, selected_market=selected)
        fallback_replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertIsNone(default_replacement)
        self.assertIsNotNone(fallback_replacement)
        self.assertEqual(fallback_replacement["market"], "Over 1.5")
        self.assertEqual(fallback_replacement["replacement_scope"], "comparable_market")

    def test_generated_player_markets_include_same_player_alternatives(self):
        payload = {
            "player": {
                "id": "p1",
                "name": "Test Forward",
                "team": "A",
                "club_league_statistics": {
                    "club": [
                        {
                            "appearances": "20",
                            "starting_lineups": "18",
                            "minutes_played": "1600",
                            "goals": "3",
                            "shots_total": "44",
                            "shots_on_target": "20",
                            "yellowcards": "1",
                            "assists": "4",
                        }
                    ]
                },
            }
        }
        statpal_context = {
            "market_snapshot_plan": {
                "snapshot_types": ["lineups", "detailed_stats", "injuries_suspensions", "prematch_odds"],
                "missing_snapshot_types": [],
                "coverage_percent": 100,
            },
            "snapshots": {
                "lineups": {"summary": {"projected": True}},
                "detailed_stats": {"summary": {}},
                "injuries_suspensions": {"summary": {}},
                "prematch_odds": {"summary": {}},
            },
        }

        generated = _generated_match_checker_markets(
            describe_market("Test Forward To Score"),
            game={"markets": []},
            statpal_context=statpal_context,
            statpal_payload=payload,
        )

        generated_names = {market["market"] for market in generated}
        self.assertIn("Test Forward Shots Over 1.5", generated_names)
        self.assertIn("Test Forward Shots On Target Over 1.5", generated_names)
        self.assertNotIn("Test Forward Shots On Target Over 0.5", generated_names)

    def test_corner_over_pick_is_not_replaced_by_corner_under(self):
        selected = {
            "market": "Corners Over 6.5",
            "advisory_score": 64,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("Corners Over 6.5").to_dict(),
            "market_capability": {"data_quality": "medium"},
        }
        generated = [
            {
                "market": "Corners Under 11.5",
                "advisory_score": 75,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("Corners Under 11.5").to_dict(),
            }
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
        )

        self.assertIsNone(replacement)

    def test_first_half_corner_pick_is_not_replaced_by_full_match_corner(self):
        selected = {
            "market": "1H Corners Over 3.5",
            "advisory_score": 30,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("1H Corners Over 3.5").to_dict(),
            "market_capability": {"data_quality": "medium"},
        }
        generated = [
            {
                "market": "Corners Over 7.5",
                "advisory_score": 75,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("Corners Over 7.5").to_dict(),
            }
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
        )

        self.assertIsNone(replacement)

    def test_first_half_corner_generation_keeps_first_half_period(self):
        names = set(_generated_market_names_for_family(describe_market("1H Corners Over 3.5")))

        self.assertIn("1H Corners Over 2.5", names)
        self.assertIn("1H Corners Under 4.5", names)
        self.assertNotIn("Corners Under 11.5", names)

    def test_replacement_reason_uses_replacement_corner_line(self):
        user_pick = _public_market_pick(
            {
                "market": "Corners Over 6.5",
                "advisory_score": 64,
                "odds": 1.24,
                "advisory_evidence": {
                    "expected_total_corners": 8.634,
                    "line": 6.5,
                    "selection": "over",
                    "market_family": "corners_total",
                },
            }
        )
        ai_pick = _public_market_pick(
            {
                "market": "Corners Over 7.5",
                "advisory_score": 70,
                "odds": None,
                "advisory_evidence": {
                    "expected_total_corners": 8.634,
                    "line": 7.5,
                    "selection": "over",
                    "market_family": "corners_total",
                },
            }
        )

        evidence = _stats_backed_evidence(
            {
                "user_pick": user_pick,
                "why": ["Expected 8.634 corner events against a line of 6.5."],
            },
            market_payload=ai_pick,
        )

        self.assertIn("Expected 8.634 corner events against a line of 7.5 for Over.", evidence)
        self.assertNotIn("Expected 8.634 corner events against a line of 6.5.", evidence)

    def test_replacement_evidence_uses_recommended_market_side(self):
        ai_pick = _public_market_pick(
            {
                "market": "Shots On Target Under 10.5",
                "advisory_score": 75,
                "odds": None,
                "market_taxonomy": describe_market("Shots On Target Under 10.5").to_dict(),
                "advisory_evidence": {
                    "expected_shots_on_target": 4.492,
                    "line": 10.5,
                    "selection": "over",
                },
            }
        )

        evidence = _stats_backed_evidence(
            {"user_pick": {"market": "Away Win", "confidence_score": 28, "odds": 2.91}},
            market_payload=ai_pick,
            owned_market_only=True,
        )
        joined = " ".join(evidence)

        self.assertIn("for Under", joined)
        self.assertNotIn("for Over", joined)

    def test_replacement_evidence_does_not_reuse_original_odds_when_replacement_unpriced(self):
        ai_pick = _public_market_pick(
            {
                "market": "Over 2.5",
                "advisory_score": 72,
                "odds": None,
                "market_taxonomy": describe_market("Over 2.5").to_dict(),
                "advisory_evidence": {
                    "expected_goals": 3.2,
                    "line": 2.5,
                    "selection": "over",
                },
            }
        )

        evidence = _stats_backed_evidence(
            {"user_pick": {"market": "Home Win", "confidence_score": 28, "odds": 1.52}},
            market_payload=ai_pick,
            owned_market_only=True,
        )
        joined = " ".join(evidence)

        self.assertIn("Over 2.5 rates at 72% confidence.", joined)
        self.assertNotIn("1.52 odds", joined)

    def test_replacement_evidence_is_owned_by_recommended_market_family(self):
        selection = {
            "user_pick": {"market": "Home Win", "confidence_score": 42, "odds": 1.8},
            "why": ["Expected goals: home 2.1, away 0.8."],
            "evidence_payload": {"home_expected_goals": 2.1, "away_expected_goals": 0.8},
            "home_recent_form": {"games": 5, "wins": 4, "draws": 1, "losses": 0},
        }
        corner_pick = _public_market_pick(
            {
                "market": "Corners Over 8.5",
                "advisory_score": 71,
                "odds": 1.55,
                "advisory_evidence": {
                    "expected_total_corners": 10.2,
                    "line": 8.5,
                    "selection": "over",
                },
            }
        )

        evidence = _stats_backed_evidence(
            selection,
            market_payload=corner_pick,
            owned_market_only=True,
        )
        joined = " ".join(evidence)

        self.assertIn("corner events", joined)
        self.assertNotIn("Expected goals", joined)
        self.assertNotIn("Home:", joined)

    def test_goal_replacement_evidence_does_not_reuse_corner_reasons(self):
        selection = {
            "user_pick": {"market": "Corners Over 8.5", "confidence_score": 42, "odds": 1.8},
            "why": ["Expected 10.2 corner events against a line of 8.5 for Over."],
            "evidence_payload": {"expected_total_corners": 10.2, "line": 8.5, "selection": "over"},
        }
        goal_pick = _public_market_pick(
            {
                "market": "Over 2.5",
                "advisory_score": 72,
                "odds": 1.7,
                "advisory_evidence": {
                    "expected_goals": 3.2,
                    "line": 2.5,
                    "selection": "over",
                },
            }
        )

        evidence = _stats_backed_evidence(
            selection,
            market_payload=goal_pick,
            owned_market_only=True,
        )
        joined = " ".join(evidence)

        self.assertIn("Expected goals", joined)
        self.assertNotIn("corner events", joined)

    def test_ranking_prefers_same_family_repair_when_cross_family_is_only_close(self):
        selected = {
            "market": "Home Win",
            "advisory_score": 36,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("Home Win").to_dict(),
        }
        generated = [
            {
                "market": "DC: 1X",
                "advisory_score": 66,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("DC: 1X").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {"home_win_probability": 35, "draw_probability": 31},
            },
            {
                "market": "Over 2.5",
                "advisory_score": 70,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("Over 2.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {"expected_goals": 3.0, "line": 2.5, "selection": "over"},
            },
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertEqual(replacement["market"], "DC: 1X")

    def test_ranking_allows_cross_family_when_meaningfully_stronger(self):
        selected = {
            "market": "Home Win",
            "advisory_score": 36,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("Home Win").to_dict(),
        }
        generated = [
            {
                "market": "DC: 1X",
                "advisory_score": 60,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("DC: 1X").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {"home_win_probability": 30, "draw_probability": 30},
            },
            {
                "market": "Over 2.5",
                "advisory_score": 78,
                "advisory_status": "strong",
                "market_taxonomy": describe_market("Over 2.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {"expected_goals": 4.2, "line": 2.5, "selection": "over"},
            },
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertEqual(replacement["market"], "Over 2.5")
        self.assertEqual(replacement["replacement_scope"], "broad_fallback")

    def test_ranking_does_not_choose_broad_safe_market_on_base_probability_only(self):
        selected = {
            "market": "Over 2.5",
            "advisory_score": 48,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("Over 2.5").to_dict(),
        }
        generated = [
            {
                "market": "Under 4.5",
                "advisory_score": 82,
                "advisory_status": "strong",
                "market_taxonomy": describe_market("Under 4.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {"expected_goals": 4.4, "line": 4.5, "selection": "under"},
            },
            {
                "market": "Over 1.5",
                "advisory_score": 70,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("Over 1.5").to_dict(),
                "market_capability": {"data_quality": "medium"},
                "advisory_evidence": {"expected_goals": 3.2, "line": 1.5, "selection": "over"},
            },
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
            allow_safer_fallback=True,
        )

        self.assertEqual(replacement["market"], "Over 1.5")

    def test_corner_replacement_ranking_prefers_fit_and_similarity_before_probability(self):
        selected = {
            "market": "Corners Over 8.5",
            "advisory_score": 62,
            "advisory_status": "avoid",
            "market_taxonomy": describe_market("Corners Over 8.5").to_dict(),
            "market_capability": {"data_quality": "medium"},
        }
        generated = [
            {
                "market": "Corners Over 6.5",
                "advisory_score": 82,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("Corners Over 6.5").to_dict(),
                "advisory_evidence": {
                    "expected_total_corners": 10.1,
                    "line": 6.5,
                    "selection": "over",
                    "market_family": "corners_total",
                },
            },
            {
                "market": "Corners Over 7.5",
                "advisory_score": 70,
                "advisory_status": "playable",
                "market_taxonomy": describe_market("Corners Over 7.5").to_dict(),
                "advisory_evidence": {
                    "expected_total_corners": 10.1,
                    "line": 7.5,
                    "selection": "over",
                    "market_family": "corners_total",
                },
            },
        ]

        replacement = _replacement_market_for_slip(
            {"markets": []},
            selected_market=selected,
            generated_markets=generated,
        )

        self.assertEqual(replacement["market"], "Corners Over 7.5")


class GeneratedCardsAlternativeTests(TestCase):
    """
    Cards alternatives are only offered when the count model can actually price them.

    Before the count model existed, a cards alternative could be generated from a
    constant that ignored the line. Suggesting a swap we cannot model is exactly the
    recommendation the product should not make.
    """

    def setUp(self):
        for team_id, name in (("h1", "Alpha"), ("a1", "Beta")):
            TeamRateProfile.objects.create(
                provider="statpal", team_id=team_id, team_name=name,
                team_name_normalized=name.lower(), corners_home=6.0, corners_away=4.5,
                cards_home=2.2, cards_away=2.4, matches=20,
            )
        self.game = {
            "markets": [{"market": "Over 1.5"}],
            "hid": "h1", "aid": "a1", "hname": "Alpha", "aname": "Beta",
        }

    def test_a_priceable_cards_alternative_is_offered(self):
        selected = {
            "market": "Cards Over 5.5",
            "advisory_score": 42,
            "advisory_status": "avoid",
            "market_taxonomy": {"family": "cards_total"},
            "market_capability": {"data_quality": "strong"},
        }

        generated = _generated_match_checker_markets(
            describe_market("Cards Over 5.5"), game=self.game, statpal_context={}
        )
        replacement = _replacement_market_for_slip(
            {"markets": [{"market": "Over 1.5", "final_confidence": 90,
                          "council_review": {"decision": "approve"}}]},
            selected_market=selected,
            generated_markets=generated,
        )

        self.assertIsNotNone(replacement)
        self.assertIn("Cards", replacement["market"])
        self.assertTrue(replacement["generated"])

    def test_no_cards_alternative_without_team_rates(self):
        TeamRateProfile.objects.all().delete()

        generated = _generated_match_checker_markets(
            describe_market("Cards Over 5.5"), game=self.game, statpal_context={}
        )

        self.assertEqual([item for item in generated if "Cards" in item.get("market", "")], [])
