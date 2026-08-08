from django.test import TestCase

from apps.algo.views import _slip_intelligence


def _analysed_leg(
    score, *, match, replacement=None, data_quality="strong", cap=88,
    family="total_goals", assessment="quantitative_model",
):
    item = {
        "match": match,
        "submitted_market": "Home Win",
        "provider_market_text": "Home Win",
        "market_taxonomy": {
            "canonical": "Home Win",
            "family": family,
            "recognized": True,
            "core_supported": True,
        },
        "canonical_market": {"resolution": "mapped", "period": "full_match", "subject": "match"},
        "status": "analysed",
        "verdict": "keep",
        "message": "",
        "matched_fixture": {"match_id": match, "match_date": "2026-08-08", "fixture": match},
        "provider_payload": {"odds": 1.9},
        "selected_market": {
            "market": "Home Win",
            "advisory_score": score,
            "advisory_status": "strong" if score >= 78 else "caution",
            "confidence": score,
            "final_confidence": score,
            "odds": 1.9,
            "market_capability": {"confidence_cap": cap, "data_quality": data_quality},
            "statpal_advisory": {"assessment_type": assessment},
        },
    }
    if replacement is not None:
        item["verdict"] = "replace"
        item["replacement_market"] = {"market": "Double Chance", "advisory_score": replacement, "odds": 1.3}
    return item


class ContractV2Tests(TestCase):
    def setUp(self):
        self.legs = [
            _analysed_leg(88, match="Arsenal vs Everton"),
            _analysed_leg(72, match="Spurs vs Fulham"),
            _analysed_leg(48, match="Chelsea vs Brentford", replacement=74),
        ]
        _, self.intelligence = _slip_intelligence(self.legs)
        self.public = self.intelligence["public"]

    def test_contract_version_is_v2(self):
        self.assertEqual(self.public["contract_version"], "match_checker_public_v2")

    def test_ticket_reports_a_multiplicative_success_estimate(self):
        estimate = self.public["ticket"]["estimated_success_percent"]

        self.assertIsNotNone(estimate)
        # Three legs, none certain: the ticket is far less likely than its best leg.
        self.assertLess(estimate, 50)

    def test_ticket_reports_risk_tier_counts(self):
        tiers = self.public["ticket"]["risk_tiers"]

        self.assertEqual(sum(tiers.values()), 3)

    def test_ticket_killers_identify_the_weakest_leg(self):
        killers = self.public["ticket_killers"]["selections"]

        self.assertTrue(killers)
        self.assertEqual(killers[0]["match"], "Chelsea vs Brentford")
        self.assertIn("risk", self.public["ticket_killers"]["message"])

    def test_each_selection_carries_a_risk_tier_and_share(self):
        for card in self.public["selections"]:
            tier = card["risk_tier"]
            self.assertIn(tier["code"], {"very_strong", "strong", "borderline", "risky", "avoid", "unknown"})
            self.assertIsNotNone(tier["risk_share_percent"])
            self.assertIsNotNone(tier["estimated_success_percent"])

    def test_repairable_leg_exposes_its_ticket_lift(self):
        repairs = [card["repair"] for card in self.public["selections"] if card["repair"]["available"]]

        self.assertTrue(repairs)
        self.assertGreater(repairs[0]["ticket_lift_points"], 0)

    def test_calibration_basis_is_disclosed(self):
        calibration = self.public["calibration"]

        self.assertEqual(calibration["basis"], "prior")
        self.assertEqual(calibration["sample_size"], 0)
        self.assertIn("not guarantees", calibration["disclaimer"])
        self.assertIn("conservative prior", calibration["disclaimer"])

    def test_health_reflects_leg_quality_not_the_raw_accumulator_odds(self):
        health = self.public["ticket_health"]

        # The product of three legs is small, but leg quality is mid-range.
        self.assertGreater(health["score"], self.public["ticket"]["estimated_success_percent"])
        self.assertEqual(health["risk_level"], self.intelligence["risk_level"])


class ContractV2UnanalysedTests(TestCase):
    def test_unassessable_ticket_reports_no_estimate_and_no_killers(self):
        legs = [
            _analysed_leg(90, match="A vs B", data_quality="poor", cap=30),
            _analysed_leg(90, match="C vs D", data_quality="poor", cap=30),
        ]

        _, intelligence = _slip_intelligence(legs)
        public = intelligence["public"]

        self.assertIsNone(public["ticket"]["estimated_success_percent"])
        self.assertIsNone(public["ticket_health"]["score"])
        self.assertEqual(public["ticket_health"]["risk_level"], "unknown")
        self.assertEqual(public["ticket_killers"]["selections"], [])
        self.assertIn("no risk ranking", public["ticket_killers"]["message"])

    def test_unassessed_legs_are_labelled_not_assessed_rather_than_avoid(self):
        legs = [_analysed_leg(90, match="A vs B", data_quality="poor", cap=30)]

        _, intelligence = _slip_intelligence(legs)
        card = intelligence["public"]["selections"][0]

        self.assertEqual(card["risk_tier"]["code"], "unknown")
        self.assertEqual(card["risk_tier"]["label"], "Not assessed")
        self.assertIsNone(card["risk_tier"]["estimated_success_percent"])
