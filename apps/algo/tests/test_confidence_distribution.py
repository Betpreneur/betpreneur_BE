"""
Regression guard for the "every review says 64%" defect.

Data quality used to be applied as `min(cap, probability)`, which truncated the
modelled estimate at the data-quality ceiling. Because a handful of capability
tiers cover most fixtures, that collapsed genuinely different probabilities onto
a handful of numbers -- with a cap of 62, nineteen markets in twenty came back as
exactly 62. The model was fine; the presentation layer was flattening it.

These tests assert the two quantities stay separate: the probability is reported
as modelled, and thin evidence is expressed by holding the *claim* back.
"""

from collections import Counter

from django.test import SimpleTestCase

from apps.algo.views import _submitted_market_payload, _with_market_capability

# A spread of modelled probabilities of the kind the score matrix actually emits.
PROBABILITIES = [
    41.2, 44.8, 48.3, 51.7, 54.1, 57.6, 59.9, 62.4, 64.0, 66.8,
    68.2, 70.5, 72.9, 74.3, 76.1, 78.8, 81.0, 83.6, 86.2, 90.4,
]


def _capability(cap, quality="limited"):
    return {
        "support_level": "medium",
        "data_quality": quality,
        "confidence_cap": cap,
        "warnings": [],
    }


class ProbabilityDistributionTests(SimpleTestCase):
    def _scores(self, cap):
        return [
            _with_market_capability(
                {
                    "market": "Over 2.5",
                    "advisory_score": probability,
                    "advisory_status": "strong",
                    "advisory_warnings": [],
                    "advisory_evidence": {},
                },
                _capability(cap),
            )["advisory_score"]
            for probability in PROBABILITIES
        ]

    def test_data_quality_ceiling_does_not_collapse_distinct_probabilities(self):
        """No pile-up on the cap value -- the old behaviour put 60-95% there."""
        for cap in (62, 70, 75):
            with self.subTest(cap=cap):
                scores = self._scores(cap)
                landed_on_cap = sum(1 for score in scores if score == cap)
                self.assertEqual(
                    landed_on_cap,
                    0,
                    f"{landed_on_cap}/{len(scores)} probabilities were truncated to the cap {cap}",
                )

    def test_distinct_inputs_stay_distinct(self):
        """Two fixtures the model separates must not be reported as the same number."""
        for cap in (62, 70, 75):
            with self.subTest(cap=cap):
                scores = self._scores(cap)
                self.assertEqual(len(set(scores)), len(PROBABILITIES))

    def test_no_single_value_dominates_the_output(self):
        """The concrete symptom: ~80% of reviews reporting one number."""
        for cap in (62, 70, 75):
            with self.subTest(cap=cap):
                most_common, count = Counter(self._scores(cap)).most_common(1)[0]
                share = count / len(PROBABILITIES)
                self.assertLess(
                    share,
                    0.25,
                    f"{share:.0%} of markets reported {most_common} with cap {cap}",
                )


class ClaimHoldbackTests(SimpleTestCase):
    """Keeping the probability honest must not make the *claim* reckless."""

    def test_thin_evidence_still_holds_the_status_back(self):
        market = _with_market_capability(
            {
                "market": "Over 2.5",
                "advisory_score": 88.0,
                "advisory_status": "strong",
                "advisory_warnings": [],
                "advisory_evidence": {},
            },
            _capability(58),
        )
        self.assertEqual(market["advisory_score"], 88.0)
        self.assertEqual(market["data_confidence"], 58.0)
        # 88% would be "strong"; 58 points of evidence only earns "caution".
        self.assertEqual(market["advisory_status"], "caution")
        self.assertTrue(market["advisory_evidence"]["claim_limited_by_data_quality"])

    def test_strong_evidence_lets_a_strong_estimate_stand(self):
        market = _with_market_capability(
            {
                "market": "Over 2.5",
                "advisory_score": 88.0,
                "advisory_status": "strong",
                "advisory_warnings": [],
                "advisory_evidence": {},
            },
            _capability(92, quality="full"),
        )
        self.assertEqual(market["advisory_status"], "strong")
        self.assertNotIn("claim_limited_by_data_quality", market["advisory_evidence"])

    def test_submitted_and_direct_paths_agree(self):
        """The two paths compute the same claim -- they drifted once already."""
        for probability in PROBABILITIES:
            for cap in (58, 75, 92):
                with self.subTest(probability=probability, cap=cap):
                    direct = _with_market_capability(
                        {
                            "market": "Over 2.5",
                            "advisory_score": probability,
                            "advisory_status": "strong",
                            "advisory_warnings": [],
                            "advisory_evidence": {},
                            "statpal_advisory": {},
                        },
                        _capability(cap),
                    )
                    submitted = _submitted_market_payload(
                        requested_market="Over 2.5",
                        market_taxonomy={"family": "total_goals"},
                        statpal_advisory={
                            "available": True,
                            "score": probability,
                            "status": "strong",
                            "basis": "statpal_goal_market_model",
                            "warnings": [],
                            "evidence": {},
                        },
                        market_capability=_capability(cap),
                    )
                    self.assertEqual(
                        submitted["advisory_score"], direct["advisory_score"]
                    )
                    self.assertEqual(
                        submitted["data_confidence"], direct["data_confidence"]
                    )
                    self.assertEqual(
                        submitted["advisory_status"], direct["advisory_status"]
                    )
