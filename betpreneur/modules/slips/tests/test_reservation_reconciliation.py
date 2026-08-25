from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

# Reconciling a stale reservation asks slips whether the review was
# delivered, so this exercises the delivery resolver slips registers.
from betpreneur.modules.billing.api import (
    TokenReservation,
    TokenTransaction,
    TokenWallet,
    TokenWalletService,
)
from betpreneur.modules.slips.interface.views import _consume_slip_review_token_reservation
from betpreneur.modules.slips.models import SlipReview


class StaleReservationReconciliationTests(TestCase):
    """
    A reservation left open after a *successful* review must not be refunded.

    `_consume_slip_review_token_reservation` is best-effort -- it will not fail a
    delivered review over a billing error. That leaves the escrow open, and the
    sweeper used to refund any stale `reserved` row on sight, handing back the
    tokens for a review the user had already received.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="stale-reservation-user",
            email="stale@example.com",
            password="test-pass",
        )
        self.service = TokenWalletService()
        TokenWallet.objects.create(user=self.user, free_tokens=10, paid_tokens=0)

    def _review(self, status):
        return SlipReview.objects.create(
            user=self.user,
            source=SlipReview.Source.SPORTYBET,
            status=status,
            submitted_payload={"code": "ABC123", "selection_count": 3},
            summary={},
        )

    def _stale_reservation(self, review, amount=3):
        result = self.service.reserve_tokens(
            self.user,
            amount,
            reference_type="slip_review",
            reference_id=str(review.id),
        )
        reservation = result.reservation
        TokenReservation.objects.filter(pk=reservation.pk).update(
            expires_at=timezone.now() - timedelta(minutes=5)
        )
        return reservation

    def test_delivered_review_is_recognised_not_refunded(self):
        review = self._review(SlipReview.Status.COMPLETED)
        reservation = self._stale_reservation(review)

        report = self.service.expire_stale_reservations()

        reservation.refresh_from_db()
        wallet = TokenWallet.objects.get(user=self.user)
        self.assertEqual(reservation.status, TokenReservation.Status.CONSUMED)
        self.assertEqual(report["consumed"], 1)
        self.assertEqual(report["expired"], 0)
        # The user received the review, so the tokens stay spent.
        self.assertEqual(wallet.total_tokens, 7)

    def test_partial_review_counts_as_delivered(self):
        review = self._review(SlipReview.Status.PARTIAL)
        reservation = self._stale_reservation(review)

        self.service.expire_stale_reservations()

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, TokenReservation.Status.CONSUMED)
        self.assertEqual(TokenWallet.objects.get(user=self.user).total_tokens, 7)

    def test_stale_delivered_review_only_recognises_analysed_legs(self):
        review = self._review(SlipReview.Status.PARTIAL)
        review.summary = {"analysed_count": 1, "not_assessed_count": 2}
        review.save(update_fields=["summary"])
        reservation = self._stale_reservation(review)

        report = self.service.expire_stale_reservations()

        reservation.refresh_from_db()
        wallet = TokenWallet.objects.get(user=self.user)
        self.assertEqual(reservation.status, TokenReservation.Status.CONSUMED)
        self.assertEqual(report["consumed"], 1)
        self.assertEqual(report["tokens_recognised"], 1)
        self.assertEqual(wallet.total_tokens, 9)
        release_tx = TokenTransaction.objects.get(reason=TokenTransaction.Reason.SLIP_REVIEW_RELEASE)
        self.assertEqual(release_tx.amount, 2)

    def test_failed_review_is_still_refunded(self):
        review = self._review(SlipReview.Status.FAILED)
        reservation = self._stale_reservation(review)

        report = self.service.expire_stale_reservations()

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, TokenReservation.Status.EXPIRED)
        self.assertEqual(report["expired"], 1)
        self.assertEqual(TokenWallet.objects.get(user=self.user).total_tokens, 10)

    def test_unanalysed_review_is_refunded(self):
        """Nothing was analysed, so nothing was delivered."""
        review = self._review(SlipReview.Status.UNANALYSED)
        reservation = self._stale_reservation(review)

        self.service.expire_stale_reservations()

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, TokenReservation.Status.EXPIRED)
        self.assertEqual(TokenWallet.objects.get(user=self.user).total_tokens, 10)

    def test_in_flight_review_is_deferred_not_refunded(self):
        """Refunding a running review would let it finish and be delivered free."""
        review = self._review(SlipReview.Status.ANALYSING)
        reservation = self._stale_reservation(review)

        report = self.service.expire_stale_reservations()

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, TokenReservation.Status.RESERVED)
        self.assertEqual(report["deferred"], 1)
        self.assertEqual(report["expired"], 0)
        self.assertEqual(TokenWallet.objects.get(user=self.user).total_tokens, 7)

    def test_reservation_for_a_missing_review_is_refunded(self):
        review = self._review(SlipReview.Status.COMPLETED)
        reservation = self._stale_reservation(review)
        review.delete()

        self.service.expire_stale_reservations()

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, TokenReservation.Status.EXPIRED)
        self.assertEqual(TokenWallet.objects.get(user=self.user).total_tokens, 10)

    def test_non_slip_review_reservations_still_expire(self):
        result = self.service.reserve_tokens(
            self.user, 3, reference_type="other_feature", reference_id="1"
        )
        TokenReservation.objects.filter(pk=result.reservation.pk).update(
            expires_at=timezone.now() - timedelta(minutes=5)
        )

        report = self.service.expire_stale_reservations()

        self.assertEqual(report["expired"], 1)
        self.assertEqual(TokenWallet.objects.get(user=self.user).total_tokens, 10)

    def test_a_failed_consume_leaves_the_escrow_open_for_reconciliation(self):
        """The end-to-end leak: consume blows up, sweeper must not refund."""
        review = self._review(SlipReview.Status.COMPLETED)
        reservation = self._stale_reservation(review)
        review.submitted_payload = {
            **review.submitted_payload,
            "token_reservation_id": reservation.id,
            "selection_count": 3,
        }
        review.save(update_fields=["submitted_payload"])

        with patch(
            "betpreneur.modules.billing.api.token_wallet_service.consume_reservation_amount",
            side_effect=RuntimeError("database went away"),
        ):
            self.assertIsNone(_consume_slip_review_token_reservation(review))

        # Billing records the open escrow rather than silently reporting success.
        self.assertEqual(review.summary["billing"]["status"], "consume_failed")
        self.assertTrue(review.summary["billing"]["reconciliation_pending"])

        self.service.expire_stale_reservations()

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, TokenReservation.Status.CONSUMED)
        self.assertEqual(TokenWallet.objects.get(user=self.user).total_tokens, 7)
