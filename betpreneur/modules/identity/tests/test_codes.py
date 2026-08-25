"""Verification codes. Pure domain — no Django needed."""
import unittest

from betpreneur.modules.identity.domain.codes import CODE_LENGTH, generate_code


class GenerateCodeTests(unittest.TestCase):
    def test_is_six_numeric_digits_by_default(self):
        for _ in range(50):
            code = generate_code()
            self.assertEqual(len(code), CODE_LENGTH)
            self.assertTrue(code.isdigit(), code)

    def test_length_is_configurable(self):
        self.assertEqual(len(generate_code(8)), 8)

    def test_codes_vary(self):
        # 6 digits gives a million outcomes; 200 draws colliding into fewer
        # than 150 distinct values would mean the generator is broken.
        self.assertGreater(len({generate_code() for _ in range(200)}), 150)
