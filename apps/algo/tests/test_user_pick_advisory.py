from unittest.mock import patch
from dataclasses import replace

from django.test import SimpleTestCase, TestCase

from apps.algo.views import (
    _consume_review_force_fresh,
    _generated_match_checker_markets,
    _generated_market_names_for_family,
    _blocked_slip_recommendation_market,
    _market_can_skip_core_on_demand,
    _manual_verdict,
    _replacement_market_for_slip,
    _submitted_market_payload,
    _with_market_capability,
)
from apps.algo.market_taxonomy import describe_market
from apps.algo.models import TeamRateProfile
from apps.algo.scoring.dixon_coles import ScoreMatrix


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

        self.assertEqual(submitted["advisory_score"], 56)
        self.assertEqual(submitted["advisory_status"], "caution")
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

        self.assertEqual(submitted["advisory_score"], 75)
        self.assertEqual(submitted["market_capability"]["data_quality"], "medium")
        self.assertEqual(submitted["market_capability"]["confidence_cap"], 75)
        self.assertNotIn("no_expected_goals_available", submitted["market_capability"]["warnings"])

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
        }

        verdict = _manual_verdict(selected, replacement)

        self.assertEqual(verdict["verdict"], "replace")
        self.assertTrue(verdict["better_market_available"])

    def test_direct_market_analysis_also_uses_capability_cap(self):
        market = _with_market_capability(
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

        self.assertEqual(market["advisory_score"], 70)
        self.assertEqual(market["advisory_status"], "playable")
        self.assertIn("missing_required_snapshots", market["advisory_warnings"])
        self.assertTrue(market["advisory_evidence"]["cap_applied"])

    def test_replacement_prefers_same_market_family_over_higher_broad_market(self):
        selected = {
            "market": "Over 2.5",
            "advisory_score": 48,
            "advisory_status": "avoid",
            "market_taxonomy": {"family": "total_goals"},
        }
        game = {
            "markets": [
                {"market": "DC: 12", "final_confidence": 92, "confidence": 92, "council_review": {"decision": "approve"}},
                {"market": "Over 1.5", "final_confidence": 78, "confidence": 78, "council_review": {"decision": "approve"}},
            ]
        }

        replacement = _replacement_market_for_slip(game, selected_market=selected)

        self.assertEqual(replacement["market"], "Over 1.5")
        self.assertEqual(replacement["replacement_scope"], "comparable_market")

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
        weak_upgrade = {"market": "Over 1.5", "advisory_score": 62, "advisory_status": "caution"}
        strong_upgrade = {"market": "Over 1.5", "advisory_score": 68, "advisory_status": "playable"}

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
        broad_upgrade = {"market": "Over 1.5", "advisory_score": 68, "advisory_status": "playable"}

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

        with patch("apps.algo.scoring.service.score_model_service.rates_for_fixture") as rates:
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
                    "data_quality": "medium",
                    "league_id": "league-1",
                    "model_version": "test",
                    "matrix": lambda self: matrix,
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
        generated = [{"market": "Over 1.5", "advisory_score": 70, "advisory_status": "playable", "market_taxonomy": {"family": "total_goals"}}]

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
