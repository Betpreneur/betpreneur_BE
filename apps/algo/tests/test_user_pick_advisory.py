from django.test import SimpleTestCase

from apps.algo.views import (
    _generated_match_checker_markets,
    _manual_verdict,
    _replacement_market_for_slip,
    _submitted_market_payload,
    _with_market_capability,
)
from apps.algo.market_taxonomy import describe_market


class UserPickAdvisoryTests(SimpleTestCase):
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

    def test_broad_replacement_requires_clearer_upgrade(self):
        selected = {
            "market": "Player To Score",
            "advisory_score": 52,
            "advisory_status": "avoid",
            "market_taxonomy": {"family": "player_goal"},
            "market_capability": {"data_quality": "limited"},
        }
        weak_upgrade = {"market": "Over 1.5", "advisory_score": 62, "advisory_status": "caution"}
        strong_upgrade = {"market": "Over 1.5", "advisory_score": 68, "advisory_status": "playable"}

        weak_verdict = _manual_verdict(selected, weak_upgrade)
        strong_verdict = _manual_verdict(selected, strong_upgrade)

        self.assertEqual(weak_verdict["verdict"], "remove")
        self.assertEqual(strong_verdict["verdict"], "remove")

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

    def test_generated_cards_markets_provide_same_family_replacement(self):
        selected = {
            "market": "Cards Over 5.5",
            "advisory_score": 42,
            "advisory_status": "avoid",
            "market_taxonomy": {"family": "cards_total"},
            "market_capability": {"data_quality": "strong"},
        }
        statpal_context = {
            "market_snapshot_plan": {
                "snapshot_types": ["detailed_stats", "lineups", "prematch_odds", "injuries_suspensions"],
                "missing_snapshot_types": [],
                "coverage_percent": 100,
            },
            "snapshots": {
                "detailed_stats": {
                    "summary": {
                        "home_yellow_cards": 2.1,
                        "away_yellow_cards": 2.0,
                    }
                },
                "lineups": {"summary": {"projected": True}},
                "prematch_odds": {"summary": {}},
                "injuries_suspensions": {"summary": {}},
            },
        }
        generated = _generated_match_checker_markets(
            describe_market("Cards Over 5.5"),
            game={"markets": [{"market": "Over 1.5"}]},
            statpal_context=statpal_context,
        )

        replacement = _replacement_market_for_slip(
            {"markets": [{"market": "Over 1.5", "final_confidence": 90, "council_review": {"decision": "approve"}}]},
            selected_market=selected,
            generated_markets=generated,
        )

        self.assertIsNotNone(replacement)
        self.assertEqual(replacement["replacement_scope"], "comparable_market")
        self.assertIn("Cards", replacement["market"])
        self.assertTrue(replacement["generated"])

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
        self.assertIn("Test Forward Shots On Target Over 0.5", generated_names)
