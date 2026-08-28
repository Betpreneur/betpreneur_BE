"""Stage 19: explanations must be model-aware, never generically confident."""
import unittest

from betpreneur.modules.prediction.contracts import MarketProbability, ValueAssessment
from betpreneur.modules.prediction.explanation import (
    Fact,
    FactKind,
    Priority,
    build_explanation,
    classify,
    explain_market,
    explain_value,
)


def _fact(kind, text, priority=Priority.NORMAL):
    return Fact(kind=kind, text=text, source="test", priority=priority)


class OrderingTests(unittest.TestCase):
    def test_facts_read_projection_then_detail_then_history(self):
        built = build_explanation("Over 2.5", [
            _fact(FactKind.HISTORY, "Landed in 4 of 6 tracked comparable games."),
            _fact(FactKind.COMPONENT, "Home average: 1.80 xG."),
            _fact(FactKind.PROJECTION, "Projected total goals: 3.10."),
            _fact(FactKind.COMPARISON, "Line 2.5 is below the model projection."),
        ])
        self.assertEqual(
            [f.kind for f in built.facts],
            [FactKind.PROJECTION, FactKind.COMPONENT, FactKind.COMPARISON, FactKind.HISTORY],
        )

    def test_priority_breaks_ties_within_a_kind(self):
        built = build_explanation("Over 2.5", [
            _fact(FactKind.PROJECTION, "Projected total goals: 3.10."),
            _fact(FactKind.COMPONENT, "Quiet detail.", Priority.LOW),
            _fact(FactKind.COMPONENT, "Loud detail.", Priority.HIGH),
        ])
        self.assertEqual(built.lines()[1], "Loud detail.")

    def test_identical_facts_are_not_repeated(self):
        built = build_explanation("Over 2.5", [
            _fact(FactKind.PROJECTION, "Projected total goals: 3.10."),
            _fact(FactKind.PROJECTION, "projected total goals:  3.10."),
        ])
        self.assertEqual(len(built.facts), 1)


class NoGenericReasonTests(unittest.TestCase):
    """The rule Stage 19 exists to enforce."""

    def test_an_explanation_with_model_facts_has_a_basis(self):
        built = build_explanation("Over 2.5", [
            _fact(FactKind.PROJECTION, "Projected total goals: 3.10."),
        ])
        self.assertTrue(built.has_model_basis)
        self.assertFalse(built.is_generic_only)
        self.assertEqual(built.limitations, ())

    def test_history_alone_is_not_a_model_basis(self):
        """A hit rate is not a reason — that is the 'rates at 70%' failure."""
        built = build_explanation("Over 1.5", [
            _fact(FactKind.HISTORY, "Landed in 7 of 10 tracked comparable games."),
        ])
        self.assertFalse(built.has_model_basis)
        self.assertTrue(built.is_generic_only)

    def test_an_explanation_with_no_model_facts_states_why(self):
        built = build_explanation("Over 2.5", [], no_model_reason="No scoreline distribution was available")
        self.assertEqual(len(built.limitations), 1)
        self.assertEqual(
            built.limitations[0].text, "No scoreline distribution was available."
        )

    def test_an_explanation_is_never_silent(self):
        self.assertTrue(build_explanation("Over 2.5", []).facts)

    def test_trimming_never_drops_the_limitation(self):
        built = build_explanation(
            "Over 2.5",
            [_fact(FactKind.HISTORY, f"History fact {i}.") for i in range(5)],
            limit=2,
        )
        self.assertEqual(len(built.facts), 2)
        self.assertTrue(built.limitations, "a trimmed explanation must keep its caveat")


class FromContractsTests(unittest.TestCase):
    def test_a_market_probability_becomes_ordered_facts(self):
        probability = MarketProbability(
            fixture_id="f1",
            market="Over 2.5",
            raw_probability=0.71,
            calibrated_probability=0.66,
            model="poisson_goals",
            data_quality="strong",
            explanation_facts=(
                "Home average: 1.80 xG.",
                "Projected total goals: 3.10.",
                "Over 2.5 landed in 4 of 6 tracked comparable games.",
            ),
        )
        built = explain_market(probability)
        self.assertTrue(built.has_model_basis)
        self.assertEqual(built.lines()[0], "Projected total goals: 3.10.")
        self.assertEqual(built.of_kind(FactKind.HISTORY)[0].source, "poisson_goals")

    def test_a_missing_matrix_is_explained_not_hidden(self):
        probability = MarketProbability(
            fixture_id="f1",
            market="Over 2.5",
            model="poisson_goals",
            data_quality="unavailable",
            warnings=("scoreline_matrix_missing",),
        )
        built = explain_market(probability)
        self.assertTrue(built.is_generic_only)
        self.assertEqual(
            built.limitations[0].text,
            "No scoreline distribution was available for this fixture.",
        )

    def test_value_facts_are_price_facts_and_claim_no_model_basis(self):
        value = ValueAssessment(
            fixture_id="f1",
            market="Over 2.5",
            calibrated_probability=0.66,
            available_odds=1.62,
            explanation_facts=("Model fair odds: 1.48.", "Available odds: 1.62."),
        )
        built = explain_value(value)
        self.assertEqual({f.kind for f in built.facts}, {FactKind.PRICE})
        self.assertFalse(built.has_model_basis, "price is not evidence about football")


class ClassifyTests(unittest.TestCase):
    def test_existing_writer_strings_are_typed_correctly(self):
        cases = [
            ("Projected total goals: 3.10.", FactKind.PROJECTION),
            ("Home win probability: 52%.", FactKind.PROJECTION),
            ("Home average: 1.80 xG.", FactKind.COMPONENT),
            ("Away team concedes 5.20 corners.", FactKind.COMPONENT),
            ("Line 7.5 is below the model projection of 10.40 corners.", FactKind.COMPARISON),
            ("Home team profile: Over 2.5 landed in 4 of 6 tracked comparable games.", FactKind.HISTORY),
            ("Model fair odds: 1.48.", FactKind.PRICE),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(classify(text).kind, expected)


class SerialisationTests(unittest.TestCase):
    def test_an_explanation_round_trips_to_a_payload(self):
        built = build_explanation("Over 2.5", [
            Fact(
                kind=FactKind.PROJECTION,
                text="Projected total goals: 3.10.",
                source="poisson_goals",
                values={"projected_total": 3.10},
            )
        ])
        payload = built.to_dict()
        self.assertTrue(payload["has_model_basis"])
        self.assertEqual(payload["facts"][0]["values"], {"projected_total": 3.10})


class RealEngineInvariantTests(unittest.TestCase):
    """The invariant has to hold for markets the engine really produces, not
    just for contracts assembled in a test."""

    def _prediction(self):
        from betpreneur.modules.prediction.tests.test_market_probabilities import (
            MarketProbabilityEngineTests,
        )

        # Reuse the engine suite's fixture rather than duplicating a second
        # hand-built prediction that could drift from it.
        return MarketProbabilityEngineTests("run")._prediction()

    def test_every_evaluated_market_is_explained_or_explains_itself(self):
        from betpreneur.modules.prediction.api import evaluate_market_probability

        prediction = self._prediction()
        markets = [
            "Home Win", "Draw", "Away Win", "DC: 1X", "DNB Home",
            "Over 1.5", "Over 2.5", "Under 3.5", "GG / BTTS Yes",
            "Corners Over 8.5", "Cards Over 3.5",
        ]
        for market in markets:
            with self.subTest(market=market):
                built = explain_market(evaluate_market_probability(prediction, market))
                self.assertTrue(built.facts, "an explanation is never silent")
                self.assertTrue(
                    built.has_model_basis or built.limitations,
                    "a market with no model basis must say why",
                )

    def test_a_market_the_engine_cannot_model_says_so(self):
        from betpreneur.modules.prediction.api import evaluate_market_probability

        prediction = self._prediction()
        built = explain_market(
            evaluate_market_probability(prediction, "Player To Score Anytime")
        )
        if not built.has_model_basis:
            self.assertTrue(
                built.limitations, "an unmodelled market must carry a stated limitation"
            )
