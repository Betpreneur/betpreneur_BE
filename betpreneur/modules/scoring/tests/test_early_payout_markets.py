from django.test import SimpleTestCase

from betpreneur.modules.markets.api import describe_market
from betpreneur.modules.scoring.domain import derive
from betpreneur.modules.scoring.domain.dixon_coles import build_score_matrix
from betpreneur.modules.scoring.evaluators import score_matrix_evaluator


class EarlyPayoutMarketTests(SimpleTestCase):
    def test_home_win_1up_is_not_the_same_probability_as_plain_home_win(self):
        matrix = build_score_matrix(1.7, 1.2)

        plain = derive.home_win(matrix)
        one_up = derive.result_early_payout(matrix, "home", 1)
        two_up = derive.result_early_payout(matrix, "home", 2)

        self.assertGreater(one_up, plain)
        self.assertGreater(two_up, plain)
        self.assertGreater(one_up, two_up)

    def test_away_win_2up_uses_selected_team_lead(self):
        matrix = build_score_matrix(0.8, 1.9)

        plain = derive.away_win(matrix)
        two_up = derive.result_early_payout(matrix, "away", 2)

        self.assertGreater(two_up, plain)

    def test_double_chance_1up_improves_base_double_chance_when_final_result_loses(self):
        matrix = build_score_matrix(1.5, 1.5)

        plain = derive.double_chance(matrix, "home_or_draw")
        one_up = derive.double_chance_early_payout(matrix, "home_or_draw", 1)

        self.assertGreater(one_up, plain)

    def test_score_matrix_evaluator_routes_1up_through_early_payout_probability(self):
        descriptor = describe_market("Home Win 1UP")
        matrix = build_score_matrix(1.7, 1.2)

        probability, push = score_matrix_evaluator.outcome_probability(descriptor, matrix)

        self.assertEqual(push, 0.0)
        self.assertAlmostEqual(probability, derive.result_early_payout(matrix, "home", 1))
