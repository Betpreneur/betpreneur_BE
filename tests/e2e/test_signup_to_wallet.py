"""Signup → verify → wallet, across four modules.

Every module test mocks its neighbours. This one does not: it drives the real
HTTP stack and asserts that the registrations wiring identity to billing
actually fire. If someone deletes slips/handlers.py or billing/handlers.py, the
unit tests still pass and this fails.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from betpreneur.modules.billing.api import token_wallet_service


class SignupToWalletTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _signup(self, email="e2e@example.com"):
        response = self.client.post(
            reverse("signup"),
            {"username": "e2euser", "email": email, "password": "e2e-password-123"},
            format="json",
        )
        self.assertIn(response.status_code, (200, 201), response.content)
        return get_user_model().objects.get(email=email)

    def test_a_verified_account_is_granted_its_signup_tokens(self):
        user = self._signup()
        self.assertFalse(user.is_email_verified)

        response = self.client.post(
            reverse("verify-email"),
            {"email": user.email, "code": user.email_verification_code},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        # The grant is contributed by billing through identity's extension
        # point. Its presence proves that registration ran.
        self.assertIn("tokens", body)
        self.assertIsNotNone(body["tokens"], "billing did not contribute the grant")

        user.refresh_from_db()
        self.assertTrue(user.is_email_verified)
        wallet = token_wallet_service.get_or_create_wallet(user)
        self.assertGreater(
            wallet.free_tokens, 0, "a verified account should have a starting balance"
        )

    def test_the_wallet_endpoint_reports_the_same_balance(self):
        user = self._signup(email="e2e2@example.com")
        self.client.post(
            reverse("verify-email"),
            {"email": user.email, "code": user.email_verification_code},
            format="json",
        )
        self.client.force_authenticate(user=user)

        response = self.client.get("/api/algo/tokens/")

        self.assertEqual(response.status_code, 200, response.content)
        wallet = token_wallet_service.get_or_create_wallet(user)
        self.assertEqual(response.json()["wallet"]["free_tokens"], wallet.free_tokens)
