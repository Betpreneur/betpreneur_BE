"""
Same-fixture correlation (ADR-002).

Two legs on one match reinforce or exclude each other, so multiplying their marginals
misstates the ticket. These tests pin the direction and the honesty of the fallback.
"""

from django.test import SimpleTestCase, TestCase

from betpreneur.modules.pricing.services.ticket_risk import Calibration, TicketRiskService
from betpreneur.modules.scoring.api import (
    build_score_matrix,
    combine,
    group_factor,
    predicate_for,
    score_model_service,
)

PRIOR = Calibration(basis="prior", sample_size=0, bands={})


class PredicateTests(SimpleTestCase):
    def test_result_predicates_partition_the_grid(self):
        home = predicate_for("match_result", "home")
        draw = predicate_for("match_result", "draw")
        away = predicate_for("match_result", "away")

        for h in range(4):
            for a in range(4):
                self.assertEqual(sum([home(h, a), draw(h, a), away(h, a)]), 1)

    def test_double_chance_predicate_covers_two_results(self):
        one_x = predicate_for("double_chance", "home_or_draw")

        self.assertTrue(one_x(2, 1))
        self.assertTrue(one_x(1, 1))
        self.assertFalse(one_x(0, 1))

    def test_half_lines_are_representable(self):
        over = predicate_for("total_goals", "over", line=2.5)

        self.assertTrue(over(2, 1))
        self.assertFalse(over(1, 1))

    def test_whole_lines_are_not_representable_because_they_push(self):
        self.assertIsNone(predicate_for("total_goals", "over", line=2))

    def test_unmodelled_family_has_no_predicate(self):
        self.assertIsNone(predicate_for("correct_score", "2-1"))

    def test_team_total_uses_the_right_side(self):
        home_over = predicate_for("team_total_goals", "over", line=1.5, team="home")

        self.assertTrue(home_over(2, 0))
        self.assertFalse(home_over(0, 2))


class GroupFactorTests(SimpleTestCase):
    def setUp(self):
        self.matrix = build_score_matrix(1.6, 1.1)

    def test_positively_related_legs_produce_a_factor_above_one(self):
        # Home Win and Over 2.5 win on overlapping scorelines.
        factor, covered = group_factor(
            self.matrix,
            [("match_result", "home", None, ""), ("total_goals", "over", 2.5, "")],
        )

        self.assertEqual(covered, 2)
        self.assertGreater(factor, 1.0)

    def test_conflicting_legs_produce_a_factor_below_one(self):
        # Home Win and Away Win cannot both happen.
        factor, covered = group_factor(
            self.matrix,
            [("match_result", "home", None, ""), ("match_result", "away", None, "")],
        )

        self.assertEqual(covered, 2)
        self.assertLess(factor, 1.0)

    def test_a_single_representable_leg_leaves_the_factor_untouched(self):
        factor, covered = group_factor(
            self.matrix,
            [("match_result", "home", None, ""), ("correct_score", "2-1", None, "")],
        )

        self.assertEqual(factor, 1.0)
        self.assertEqual(covered, 0)

    def test_implied_pair_is_clamped_rather_than_exploding(self):
        # Home Win implies Home or Draw, so the raw ratio is large.
        factor, _ = group_factor(
            self.matrix,
            [("match_result", "home", None, ""), ("double_chance", "home_or_draw", None, "")],
        )

        self.assertLessEqual(factor, 5.0)
        self.assertGreater(factor, 1.0)

    def test_combine_reports_what_it_could_not_adjust(self):
        result = combine([
            (self.matrix, [("match_result", "home", None, ""), ("total_goals", "over", 2.5, "")]),
            (self.matrix, [("correct_score", "2-1", None, ""), ("correct_score", "1-0", None, "")]),
        ])

        self.assertTrue(result.applied)
        self.assertEqual(result.correlated_groups, 1)
        self.assertEqual(result.adjusted_legs, 2)
        self.assertEqual(result.skipped_legs, 2)

    def test_cross_fixture_correlation_is_declared_out_of_scope(self):
        result = combine([])

        self.assertEqual(result.to_dict()["cross_fixture_correlation"], "not_modelled")


def _standings():
    def team(name, hs, ha, hg, aws, awa, ag):
        return {
            "id": name.lower(), "name": name,
            "home": {"goals_scored": hs, "goals_allowed": ha, "games_played": hg},
            "away": {"goals_scored": aws, "goals_allowed": awa, "games_played": ag},
        }
    return {"standings": {"tournament": [{"team": [
        team("Alpha", 18, 8, 10, 12, 11, 10),
        team("Beta", 12, 12, 10, 9, 14, 10),
    ]}]}}


def _leg(score, *, match_id, family, side, line=None, team=""):
    return {
        "match": match_id,
        "status": "analysed",
        "verdict": "keep",
        "market_taxonomy": {
            "family": family, "side": side, "line": line, "team": team, "recognized": True,
        },
        "canonical_market": {"resolution": "mapped", "period": "full_match", "subject": "match"},
        "matched_fixture": {
            "match_id": match_id, "league_id": "77",
            "home_team": "Alpha", "away_team": "Beta",
        },
        "selected_market": {
            "advisory_score": score,
            "market_capability": {"confidence_cap": 88, "data_quality": "strong"},
            "statpal_advisory": {"assessment_type": "quantitative_model"},
        },
    }


class TicketCorrelationTests(TestCase):
    def setUp(self):
        score_model_service.fit_league(
            league_id="77", league_name="Test League", standings_payload=_standings()
        )

    def test_same_fixture_legs_raise_the_estimate_above_the_naive_product(self):
        legs = [
            _leg(70, match_id="m1", family="match_result", side="home"),
            _leg(70, match_id="m1", family="total_goals", side="over", line=2.5),
        ]

        ticket = TicketRiskService().assess(legs, calibration=PRIOR)
        naive = (ticket.legs[0].probability or 0) * (ticket.legs[1].probability or 0) * 100

        self.assertGreater(ticket.success_percent, naive)
        self.assertTrue(ticket.correlation["applied"])
        self.assertGreater(ticket.correlation["factor"], 1.0)

    def test_legs_on_different_fixtures_are_left_independent(self):
        legs = [
            _leg(70, match_id="m1", family="match_result", side="home"),
            _leg(70, match_id="m2", family="total_goals", side="over", line=2.5),
        ]

        ticket = TicketRiskService().assess(legs, calibration=PRIOR)

        self.assertFalse(ticket.correlation["applied"])
        self.assertEqual(ticket.correlation["factor"], 1.0)

    def test_unrepresentable_pairs_fall_back_to_independence_and_say_so(self):
        legs = [
            _leg(70, match_id="m1", family="correct_score", side="2-1"),
            _leg(70, match_id="m1", family="correct_score", side="1-0"),
        ]

        ticket = TicketRiskService().assess(legs, calibration=PRIOR)

        self.assertFalse(ticket.correlation["applied"])
        self.assertGreaterEqual(ticket.correlation["legs_assumed_independent"], 0)

    def test_correlation_never_pushes_the_estimate_above_one(self):
        legs = [
            _leg(92, match_id="m1", family="match_result", side="home"),
            _leg(92, match_id="m1", family="double_chance", side="home_or_draw"),
        ]

        ticket = TicketRiskService().assess(legs, calibration=PRIOR)

        self.assertLessEqual(ticket.success_percent, 100.0)
