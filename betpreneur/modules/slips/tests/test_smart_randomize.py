"""
Smart randomize must not contradict the review it is built from.

Two ways it did. It filtered verdicts with a denylist that let `risky` through, so a
pick the review told the user to avoid could be promoted into the generated ticket.
And it ranked purely on the modelled probability, ignoring how much evidence stood
behind it -- which, once ADR-005 stopped folding data quality into the number, meant a
leg the review labelled `caution` could top a ticket sold as the strongest picks.
"""

from django.test import SimpleTestCase

from betpreneur.modules.slips.domain.slip_analysis import (
    SMART_RANDOMIZE_MIN_CONFIDENCE,
    _smart_randomize_candidates,
    _smart_randomize_pick_for_game,
    _smart_randomize_select_candidates,
)


def _game(match="A vs B", *, confidence, verdict, data_confidence=None, game_id=1):
    return {
        "id": game_id,
        "match": match,
        "kickoff": "",
        "user_pick": {
            "market": "Over 2.5",
            "odds": "1.80",
            "confidence_score": confidence,
            "data_confidence_score": data_confidence,
            "verdict": verdict,
        },
        "recommendation": {},
    }


class EligibilityTests(SimpleTestCase):
    def test_a_pick_the_review_calls_risky_is_not_offered(self):
        self.assertIsNone(_smart_randomize_pick_for_game(_game(confidence=62.0, verdict="risky")))

    def test_a_removed_pick_is_not_offered(self):
        self.assertIsNone(_smart_randomize_pick_for_game(_game(confidence=62.0, verdict="remove")))

    def test_an_unreviewed_pick_is_not_offered(self):
        self.assertIsNone(_smart_randomize_pick_for_game(_game(confidence=62.0, verdict="review")))

    def test_kept_and_cautioned_picks_are_offered(self):
        for verdict in ("keep", "caution"):
            with self.subTest(verdict=verdict):
                self.assertIsNotNone(
                    _smart_randomize_pick_for_game(_game(confidence=62.0, verdict=verdict))
                )

    def test_unknown_verdicts_fail_closed(self):
        """A verdict code added later must be opted in, not inherited."""
        self.assertIsNone(
            _smart_randomize_pick_for_game(_game(confidence=90.0, verdict="some_future_code"))
        )

    def test_the_floor_matches_the_avoid_boundary(self):
        """Below this the review says avoid; the two must not disagree."""
        self.assertEqual(SMART_RANDOMIZE_MIN_CONFIDENCE, 55.0)
        self.assertIsNone(_smart_randomize_pick_for_game(_game(confidence=54.9, verdict="caution")))
        self.assertIsNotNone(
            _smart_randomize_pick_for_game(_game(confidence=55.0, verdict="caution"))
        )


class EvidenceAwareRankingTests(SimpleTestCase):
    def test_thin_evidence_does_not_outrank_solid_evidence(self):
        solid = _game("Solid", confidence=80.0, verdict="keep", data_confidence=92.0, game_id=1)
        thin = _game("Thin", confidence=88.0, verdict="caution", data_confidence=58.0, game_id=2)

        ordered, _ = _smart_randomize_candidates({"games": [thin, solid]})

        # The higher raw probability is the thin one; evidence decides the order.
        self.assertEqual([item["match"] for item in ordered], ["Solid", "Thin"])

    def test_the_reported_probability_is_still_the_modelled_one(self):
        """Evidence changes the ranking, never the number (ADR-005)."""
        pick = _smart_randomize_pick_for_game(
            _game(confidence=88.0, verdict="caution", data_confidence=58.0)
        )

        self.assertEqual(pick["confidence_score"], 88.0)
        self.assertEqual(pick["data_confidence_score"], 58.0)
        self.assertEqual(pick["ranking_score"], 58.0)

    def test_evidence_below_the_floor_makes_a_pick_ineligible(self):
        """An 88% estimate on 40 points of evidence is not a strongest pick."""
        self.assertIsNone(
            _smart_randomize_pick_for_game(
                _game(confidence=88.0, verdict="keep", data_confidence=40.0)
            )
        )

    def test_a_pick_without_evidence_data_ranks_on_its_probability(self):
        pick = _smart_randomize_pick_for_game(_game(confidence=72.0, verdict="keep"))

        self.assertEqual(pick["ranking_score"], 72.0)

    def test_selection_spreads_market_families_when_scores_are_comparable(self):
        candidates = [
            {"id": 1, "match": "A", "market": "Over 1.5", "ranking_score": 82.0},
            {"id": 2, "match": "B", "market": "Over 2.5", "ranking_score": 81.0},
            {"id": 3, "match": "C", "market": "Under 3.5", "ranking_score": 80.0},
            {"id": 4, "match": "D", "market": "Home Win", "ranking_score": 79.0},
        ]

        selected = _smart_randomize_select_candidates(candidates, 3)

        self.assertIn("Home Win", [item["market"] for item in selected])


class ReplacementTests(SimpleTestCase):
    def test_a_replaced_pick_offers_the_replacement_not_the_users_market(self):
        game = {
            "id": 1,
            "match": "A vs B",
            "kickoff": "",
            "user_pick": {
                "market": "Home Win",
                "confidence_score": 61.0,
                "data_confidence_score": 80.0,
                "verdict": "risky",
            },
            "recommendation": {
                "action": "replace",
                "pick": {
                    "market": "Over 1.5",
                    "odds": "1.40",
                    "confidence_score": 84.0,
                    "data_confidence_score": 88.0,
                },
            },
        }

        pick = _smart_randomize_pick_for_game(game)

        self.assertEqual(pick["market"], "Over 1.5")
        self.assertEqual(pick["source"], "ai_pick")
        self.assertTrue(pick["changed_from_user_pick"])
