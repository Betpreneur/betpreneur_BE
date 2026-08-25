"""
Player availability gating.

A prop on an injured or suspended player is a dead bet, not a risky one, so it must be
reported unavailable rather than scored down. Identity is matched on name because
SportyBet's Sportradar player ids and StatPal's own ids are disjoint id spaces.
"""

from django.test import SimpleTestCase, TestCase

from betpreneur.modules.markets.api import describe_market
from betpreneur.modules.pricing.services.advisory import statpal_market_advisory
from betpreneur.modules.scoring.api import (
    PlayerAvailability,
    name_keys,
    normalize_person,
    parse_injuries_payload,
    player_availability_service,
)


def _payload(to_miss=None, questionable=None):
    def bucket(players):
        if players is None:
            return None
        return {"player": players}
    return {"injuries_suspensions": {"league": [{
        "id": "1", "name": "Test League",
        "match": [{
            "main_id": "2026080711940",
            "home": {"id": "h1", "name": "Alpha", "sidelined": {
                "to_miss": bucket(to_miss), "questionable": bucket(questionable),
            }},
            "away": {"id": "a1", "name": "Beta", "sidelined": {}},
        }],
    }]}}


class NameMatchingTests(SimpleTestCase):
    def test_accents_and_punctuation_are_stripped(self):
        self.assertEqual(normalize_person("Ángel Di María"), "angel di maria")

    def test_trailing_team_name_is_dropped(self):
        self.assertEqual(normalize_person("Haller, Sebastian (Sanfrecce Hiroshima)"), "haller sebastian")

    def test_surname_initial_form_is_generated_for_both_feed_styles(self):
        sporty = name_keys("Haller, Sebastian (Sanfrecce Hiroshima)")
        statpal = name_keys("S. Haller")

        self.assertTrue(sporty & statpal, f"{sporty} vs {statpal}")

    def test_unrelated_players_do_not_match(self):
        self.assertFalse(name_keys("Lionel Messi") & name_keys("Cristiano Ronaldo"))

    def test_an_empty_name_yields_no_keys(self):
        self.assertEqual(name_keys(""), set())


class PayloadParsingTests(SimpleTestCase):
    def test_a_single_player_is_read_from_a_bare_dict(self):
        rows = parse_injuries_payload(
            _payload(to_miss={"id": "1", "name": "S. Nakano", "status": "Surgery"})
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "out")
        self.assertEqual(rows[0]["reason"], "Surgery")

    def test_several_players_are_read_from_a_list(self):
        rows = parse_injuries_payload(_payload(to_miss=[
            {"id": "1", "name": "Q. Butler", "status": "Knee Injury"},
            {"id": "2", "name": "T. Sabitzer", "status": "Muscle Injury"},
        ]))

        self.assertEqual(len(rows), 2)

    def test_questionable_players_are_doubtful_not_out(self):
        rows = parse_injuries_payload(
            _payload(questionable={"id": "3", "name": "A. Player", "status": "Knock"})
        )

        self.assertEqual(rows[0]["status"], "doubtful")

    def test_an_empty_feed_yields_nothing(self):
        self.assertEqual(parse_injuries_payload({}), [])


class AvailabilityServiceTests(TestCase):
    def setUp(self):
        player_availability_service.refresh(payload=_payload(
            to_miss={"id": "1", "name": "S. Haller", "status": "Knee Injury"},
            questionable={"id": "2", "name": "B. Linssen", "status": "Knock"},
        ))

    def test_refresh_stores_both_buckets(self):
        self.assertEqual(PlayerAvailability.objects.count(), 2)

    def test_an_out_player_is_reported_out_with_a_reason(self):
        verdict = player_availability_service.verdict_for(
            player_name="Haller, Sebastian (Alpha)", team_name="Alpha"
        )

        self.assertTrue(verdict.is_out)
        self.assertFalse(verdict.playable)
        self.assertEqual(verdict.reason, "Knee Injury")

    def test_a_doubtful_player_is_still_playable(self):
        verdict = player_availability_service.verdict_for(player_name="Linssen, Bryan (Alpha)")

        self.assertTrue(verdict.is_doubtful)
        self.assertTrue(verdict.playable)

    def test_a_player_absent_from_the_feed_is_available(self):
        verdict = player_availability_service.verdict_for(player_name="Someone Else")

        self.assertTrue(verdict.resolved)
        self.assertEqual(verdict.status, "")
        self.assertTrue(verdict.playable)

    def test_an_unreadable_name_is_unresolved_rather_than_assumed_fit(self):
        verdict = player_availability_service.verdict_for(player_name="")

        self.assertFalse(verdict.resolved)
        self.assertFalse(verdict.playable)

    def test_a_failed_lookup_does_not_block_pricing(self):
        # Availability is a safety check on top of pricing. An infrastructure fault must
        # not silently kill every player prop.
        from unittest import mock

        with mock.patch(
            "betpreneur.modules.scoring.models.PlayerAvailability.objects", side_effect=RuntimeError("db down")
        ):
            verdict = player_availability_service.verdict_for(player_name="Anyone")

        self.assertFalse(verdict.is_out)

    def test_refresh_replaces_the_previous_list(self):
        player_availability_service.refresh(payload=_payload(
            to_miss={"id": "9", "name": "New Player", "status": "Suspended"}
        ))

        self.assertEqual(PlayerAvailability.objects.count(), 1)


class PlayerMarketGateTests(TestCase):
    def setUp(self):
        player_availability_service.refresh(payload=_payload(
            to_miss={"id": "1", "name": "S. Haller", "status": "Knee Injury"}
        ))

    def _evaluate(self, market):
        return statpal_market_advisory.evaluate_market(describe_market(market), fixture={})

    def test_an_injured_player_prop_is_unavailable_not_scored_down(self):
        result = self._evaluate("Haller, Sebastian (Alpha) To Score")

        self.assertFalse(result["available"])
        self.assertIsNone(result["score"])
        self.assertEqual(result["basis"], "player_unavailable")
        self.assertEqual(result["assessment_type"], "none")

    def test_the_reason_is_surfaced_to_the_user(self):
        result = self._evaluate("Haller, Sebastian (Alpha) To Score")

        self.assertIn("Knee Injury", result["message"])
        self.assertIn("player_out_injured_or_suspended", result["warnings"])

    def test_an_available_player_is_not_blocked_by_the_gate(self):
        result = self._evaluate("Someone Else To Score")

        self.assertNotEqual(result["basis"], "player_unavailable")

    def test_team_markets_are_unaffected_by_the_player_gate(self):
        result = statpal_market_advisory.evaluate_market(describe_market("Over 2.5"), fixture={})

        self.assertNotEqual(result["basis"], "player_unavailable")
