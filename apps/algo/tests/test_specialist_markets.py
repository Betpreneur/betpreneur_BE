"""
Specialist picks get specialist answers, or none.

Three defects, one theme -- a market being answered by something that is not it:

* "Erling Haaland Shots On Target Over 1.5" was classified as a *match total*. The team
  branch fired unless the literal word "player" appeared, so a real booking was read
  against team lines of 6.5-11.5 and the player's name was discarded.
* `_allows_broad_replacement` returned `group not in {"unknown"}` -- true for everything.
  The guard existed in name only, so a player pick could be "improved" into Over 1.5.
* Player probabilities were published as `40 + probability * 0.55`, compressing every
  market into a 40-95 band, and a market with no recorded stat scored a flat 45.
"""

from django.test import SimpleTestCase

from apps.algo.market_taxonomy import describe_market
from apps.algo.statpal_advisory import statpal_market_advisory as advisory
from apps.algo.views import (
    SPECIALIST_REPLACEMENT_GROUPS,
    _allows_broad_replacement,
    _market_family_group,
    _market_is_better_for_slip,
    _replacement_market_for_slip,
)

STRIKER = {
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
                    "saves": "",
                }
            ]
        },
    }
}


class PlayerMarketIdentityTests(SimpleTestCase):
    def test_a_named_player_shots_on_target_pick_is_a_player_market(self):
        descriptor = describe_market("Erling Haaland Shots On Target Over 1.5")

        self.assertEqual(descriptor.family, "player_shots_on_target")

    def test_a_match_total_shots_on_target_stays_a_team_market(self):
        for name in ("Shots On Target Over 8.5", "Home Team Shots On Target Over 4.5"):
            with self.subTest(market=name):
                self.assertIn(
                    describe_market(name).family,
                    {"shots_on_target_total", "team_shots_on_target"},
                )

    def test_a_player_shots_on_target_pick_groups_with_player_markets(self):
        """Wrong grouping would let a team-shots market replace a player pick."""
        name = "Erling Haaland Shots On Target Over 1.5"
        market = {"market": name, "market_taxonomy": describe_market(name).to_dict()}

        self.assertEqual(_market_family_group(market), "player")


class SpecialistGuardTests(SimpleTestCase):
    def _market(self, name, family, score=52):
        return {
            "market": name,
            "advisory_score": score,
            "advisory_status": "avoid",
            "market_taxonomy": {"family": family},
            "market_capability": {"data_quality": "limited"},
        }

    def test_specialist_groups_refuse_a_cross_family_swap(self):
        for name, family in (
            ("Player To Score", "player_goal"),
            ("Corners Over 9.5", "corners_total"),
            ("Cards Over 3.5", "cards_total"),
        ):
            with self.subTest(market=name):
                self.assertFalse(_allows_broad_replacement(self._market(name, family)))

    def test_a_goals_pick_may_still_take_a_broad_alternative(self):
        self.assertTrue(_allows_broad_replacement(self._market("Over 1.5", "total_goals")))

    def test_a_player_pick_is_not_improved_into_a_goals_market(self):
        selected = self._market("Player To Score", "player_goal")
        broad = {"market": "Over 1.5", "advisory_score": 68, "advisory_status": "playable"}

        self.assertFalse(_market_is_better_for_slip(selected, broad))

    def test_a_card_pick_is_not_improved_into_a_result_market(self):
        selected = self._market("Player To Be Booked", "player_card")
        broad = {"market": "Home Win", "advisory_score": 70, "advisory_status": "playable"}

        self.assertFalse(_market_is_better_for_slip(selected, broad))

    def test_the_candidate_path_agrees_with_the_verdict_path(self):
        """Only the candidate path was guarded; the two must not drift again."""
        selected = self._market("Cards Over 3.5", "cards_total", score=44)
        game = {
            "markets": [
                {
                    "market": "Over 1.5",
                    "final_confidence": 90,
                    "confidence": 90,
                    "council_review": {"decision": "approve"},
                }
            ]
        }

        self.assertIsNone(_replacement_market_for_slip(game, selected_market=selected))
        self.assertFalse(
            _market_is_better_for_slip(selected, {"market": "Over 1.5", "advisory_score": 90})
        )

    def test_the_guarded_groups_are_the_specialist_ones(self):
        self.assertEqual(
            SPECIALIST_REPLACEMENT_GROUPS,
            frozenset({"player", "corners", "cards", "shots_on_target", "unknown"}),
        )


class PlayerProbabilityTests(SimpleTestCase):
    def _score(self, market):
        return advisory.evaluate_market(describe_market(market), statpal_payload=STRIKER)

    def test_the_score_is_the_modelled_probability(self):
        result = self._score("Test Forward To Score")

        self.assertEqual(result["score"], result["evidence"]["estimated_probability"])

    def test_a_thin_scorer_is_reported_as_thin(self):
        """3 goals in 20 games is roughly a 14% chance, not the 38% the band produced."""
        result = self._score("Test Forward To Score")

        self.assertLess(result["score"], 25)

    def test_different_markets_get_different_numbers(self):
        scores = {
            self._score(market)["score"]
            for market in (
                "Test Forward To Score",
                "Test Forward Shots Over 1.5",
                "Test Forward Shots On Target Over 1.5",
                "Test Forward To Be Booked",
            )
        }

        self.assertEqual(len(scores), 4)

    def test_a_market_with_no_recorded_stat_declines(self):
        """An outfielder has no saves. That is "not assessed", not a flat 45."""
        result = self._score("Test Forward Saves Over 2.5")

        self.assertFalse(result["available"])
        self.assertIsNone(result["score"])
        self.assertEqual(result["basis"], "player_market_stat_missing")

    def test_a_shots_market_reflects_the_players_shot_rate(self):
        """44 shots in 20 games is 2.2 per game, so over 1.5 is better than even."""
        result = self._score("Test Forward Shots Over 1.5")

        self.assertGreater(result["score"], 55)
