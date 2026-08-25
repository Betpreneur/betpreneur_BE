from decimal import Decimal

from django.test import SimpleTestCase

from betpreneur.platform.types.money import CurrencyMismatch, Money
from betpreneur.platform.types.odds import Odds, OddsError, Probability


class ProbabilityTests(SimpleTestCase):
    def test_rejects_out_of_range(self):
        for bad in (-0.01, 1.01, 2.0):
            with self.assertRaises(OddsError):
                Probability(bad)

    def test_fair_odds_round_trip(self):
        self.assertAlmostEqual(Probability(0.25).to_fair_odds().implied.value, 0.25)

    def test_zero_has_no_fair_price(self):
        with self.assertRaises(OddsError):
            Probability(0.0).to_fair_odds()


class OddsTests(SimpleTestCase):
    def test_rejects_prices_at_or_below_evens(self):
        for bad in ("1.0", "0.5", "-2"):
            with self.assertRaises(OddsError):
                Odds(Decimal(bad))

    def test_coerces_str_and_float(self):
        self.assertEqual(Odds("2.50").value, Decimal("2.50"))
        self.assertEqual(Odds(2.5).value, Decimal("2.5"))

    def test_parse_returns_none_for_unusable(self):
        for bad in (None, "", "abc", "1.0", 0):
            self.assertIsNone(Odds.parse(bad), msg=bad)
        self.assertEqual(Odds.parse("3.4").value, Decimal("3.4"))

    def test_implied_probability(self):
        self.assertAlmostEqual(Odds("4.00").implied.value, 0.25)

    def test_edge_is_positive_when_price_beats_estimate(self):
        # true 50% priced at 2.50 → +25% per unit staked
        self.assertAlmostEqual(Odds("2.50").edge_over(Probability(0.5)), 0.25)
        # true 50% priced at 1.80 → negative
        self.assertLess(Odds("1.80").edge_over(Probability(0.5)), 0)


class MoneyTests(SimpleTestCase):
    def test_from_major_converts_to_minor_units(self):
        self.assertEqual(Money.from_major("990.50").minor, 99050)
        self.assertEqual(Money.from_major(990).minor, 99000)

    def test_major_round_trips(self):
        self.assertEqual(Money(99050).major, Decimal("990.50"))

    def test_arithmetic_requires_same_currency(self):
        self.assertEqual((Money(100) + Money(250)).minor, 350)
        with self.assertRaises(CurrencyMismatch):
            Money(100, "NGN") + Money(100, "USD")

    def test_rejects_float_minor_units(self):
        with self.assertRaises(TypeError):
            Money(10.5)
