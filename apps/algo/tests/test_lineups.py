"""
Team sheet gating.

The line that matters: a **confirmed** omission is a fact and kills the bet; a
**projected** omission is a guess and must not. Treating the two alike would destroy
good props on the strength of a prediction.
"""

from django.test import SimpleTestCase, TestCase

from apps.algo.market_taxonomy import describe_market
from apps.algo.models import FixtureLineup
from apps.algo.scoring.lineups import (
    BENCH,
    OMITTED,
    STARTING,
    UNKNOWN,
    lineup_service,
    parse_lineups_payload,
)
from apps.algo.statpal_advisory import statpal_market_advisory


def _payload(confidence=100):
    return {
        "home": {
            "team_id": "h1", "team_name": "Alpha", "team_formation": "4-3-3",
            "confidence": confidence,
            "starting_xi": [{"id": "1", "name": "Jeremías Ledesma", "position": "goalkeeper"},
                            {"id": "2", "name": "Elías Verón", "position": "defender"}],
            "bench": [{"id": "3", "name": "Axel Werner", "position": "goalkeeper"}],
        },
        "away": {
            "team_id": "a1", "team_name": "Beta", "team_formation": "3-4-2-1",
            "confidence": confidence,
            "starting_xi": [{"id": "4", "name": "Lucas Acosta", "position": "goalkeeper"}],
            "bench": [{"id": "5", "name": "Ignacio Chicco", "position": "goalkeeper"}],
        },
    }


class PayloadParsingTests(SimpleTestCase):
    def test_both_sides_are_parsed(self):
        rows = parse_lineups_payload(_payload(), match_id="m1")

        self.assertEqual({row["side"] for row in rows}, {"home", "away"})

    def test_confidence_and_formation_are_captured(self):
        rows = parse_lineups_payload(_payload(80), match_id="m1")

        self.assertEqual(rows[0]["confidence"], 80)
        self.assertEqual(rows[0]["formation"], "4-3-3")

    def test_an_empty_payload_yields_nothing(self):
        self.assertEqual(parse_lineups_payload({}, match_id="m1"), [])


class LineupVerdictTests(TestCase):
    def setUp(self):
        lineup_service.refresh(match_id="m1", payload=_payload(confidence=100))

    def test_both_sides_are_stored(self):
        self.assertEqual(FixtureLineup.objects.filter(match_id="m1").count(), 2)

    def test_a_starter_is_reported_starting(self):
        verdict = lineup_service.verdict_for(match_id="m1", player_name="Ledesma, Jeremías (Alpha)")

        self.assertEqual(verdict.status, STARTING)
        self.assertTrue(verdict.confirmed)
        self.assertFalse(verdict.blocks_pricing)

    def test_a_substitute_is_reported_on_the_bench(self):
        verdict = lineup_service.verdict_for(match_id="m1", player_name="Werner, Axel (Alpha)")

        self.assertEqual(verdict.status, BENCH)
        self.assertTrue(verdict.rotation_risk)
        self.assertFalse(verdict.blocks_pricing)

    def test_a_confirmed_omission_blocks_pricing(self):
        verdict = lineup_service.verdict_for(match_id="m1", player_name="Nobody Here")

        self.assertEqual(verdict.status, OMITTED)
        self.assertTrue(verdict.blocks_pricing)

    def test_an_unknown_fixture_is_not_a_verdict(self):
        verdict = lineup_service.verdict_for(match_id="does-not-exist", player_name="Anyone")

        self.assertEqual(verdict.status, UNKNOWN)
        self.assertFalse(verdict.blocks_pricing)

    def test_refresh_updates_rather_than_duplicates(self):
        lineup_service.refresh(match_id="m1", payload=_payload(confidence=100))

        self.assertEqual(FixtureLineup.objects.filter(match_id="m1").count(), 2)


class ProjectedLineupTests(TestCase):
    def setUp(self):
        lineup_service.refresh(match_id="m2", payload=_payload(confidence=70))

    def test_a_projected_omission_does_not_block_pricing(self):
        verdict = lineup_service.verdict_for(match_id="m2", player_name="Nobody Here")

        self.assertEqual(verdict.status, OMITTED)
        self.assertFalse(verdict.confirmed)
        self.assertFalse(verdict.blocks_pricing)

    def test_a_projected_starter_is_still_reported(self):
        verdict = lineup_service.verdict_for(match_id="m2", player_name="Verón, Elías (Alpha)")

        self.assertEqual(verdict.status, STARTING)
        self.assertFalse(verdict.confirmed)


class PlayerMarketLineupGateTests(TestCase):
    def _evaluate(self, market, match_id):
        return statpal_market_advisory.evaluate_market(
            describe_market(market), fixture={"statpal_provider_match_id": match_id}
        )

    def test_a_player_missing_from_a_confirmed_sheet_is_unavailable(self):
        lineup_service.refresh(match_id="m1", payload=_payload(confidence=100))

        result = self._evaluate("Nobody Here To Score", "m1")

        self.assertFalse(result["available"])
        self.assertEqual(result["basis"], "player_not_in_confirmed_lineup")
        self.assertEqual(result["assessment_type"], "none")

    def test_a_player_missing_from_a_projected_sheet_is_still_priced(self):
        lineup_service.refresh(match_id="m2", payload=_payload(confidence=70))

        result = self._evaluate("Nobody Here To Score", "m2")

        self.assertNotEqual(result["basis"], "player_not_in_confirmed_lineup")

    def test_a_named_starter_is_not_blocked(self):
        lineup_service.refresh(match_id="m1", payload=_payload(confidence=100))

        result = self._evaluate("Ledesma, Jeremías (Alpha) To Score", "m1")

        self.assertNotEqual(result["basis"], "player_not_in_confirmed_lineup")

    def test_team_markets_are_unaffected_by_the_lineup_gate(self):
        lineup_service.refresh(match_id="m1", payload=_payload(confidence=100))

        result = statpal_market_advisory.evaluate_market(
            describe_market("Over 2.5"), fixture={"statpal_provider_match_id": "m1"}
        )

        self.assertNotEqual(result["basis"], "player_not_in_confirmed_lineup")
