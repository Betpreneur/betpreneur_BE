from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from betpreneur.modules.billing.api import (
    TokenReservation,
    TokenTransaction,
    TokenWallet,
    TokenWalletService,
)
from betpreneur.modules.slips.interface.views import (
    _consume_slip_review_token_reservation,
    process_slip_review_import,
)
from betpreneur.modules.slips.models import SlipReview


class SlipReviewTokenBillingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="slip-token-user",
            email="slip-token-user@example.com",
            password="test-pass",
        )

    def _review(self):
        return SlipReview.objects.create(
            user=self.user,
            source=SlipReview.Source.SPORTYBET,
            status=SlipReview.Status.QUEUED,
            title="SportyBet review",
            submitted_payload={"code": "ABC123"},
            summary={},
        )

    def _imported_payload(self, count=3):
        return {
            "share_code": "ABC123",
            "selection_count": count,
            "selections": [
                {"match": f"Home {index} vs Away {index}", "market": "Over 1.5", "odds": "1.30"}
                for index in range(1, count + 1)
            ],
        }

    @override_settings(SLIP_REVIEW_TOKEN_COST_PER_GAME=1)
    @patch("betpreneur.modules.slips.interface.views.SportyBetShareImporter.import_share")
    def test_import_fails_cleanly_when_wallet_cannot_cover_selection_count(self, import_mock):
        import_mock.return_value = self._imported_payload(count=3)
        TokenWallet.objects.create(user=self.user, free_tokens=2, paid_tokens=0)
        review = self._review()

        result = process_slip_review_import(review.id)

        review.refresh_from_db()
        wallet = TokenWallet.objects.get(user=self.user)
        self.assertEqual(review.status, SlipReview.Status.FAILED)
        self.assertEqual(review.summary["error_code"], "insufficient_tokens")
        self.assertEqual(result["error_code"], "insufficient_tokens")
        self.assertEqual(result["error_payload"]["required_tokens"], 3)
        self.assertEqual(result["error_payload"]["available_tokens"], 2)
        self.assertEqual(wallet.free_tokens, 2)
        self.assertEqual(TokenReservation.objects.count(), 0)
        self.assertEqual(TokenTransaction.objects.count(), 0)

    @override_settings(SLIP_REVIEW_TOKEN_COST_PER_GAME=1, TOKEN_RESERVATION_TTL_MINUTES=30)
    @patch("betpreneur.modules.slips.interface.views.plan_slip_hydration")
    @patch("celery.chord")
    @patch("betpreneur.modules.slips.interface.views.SportyBetShareImporter.import_share")
    def test_import_reserves_tokens_before_queueing_leg_fanout(self, import_mock, chord_mock, plan_mock):
        import_mock.return_value = self._imported_payload(count=2)
        plan_mock.return_value = {
            "legs": 2,
            "distinct_fixtures": 2,
            "fixtures_needing_snapshots": 2,
            "fixtures_served_by_model": 0,
            "estimated_snapshot_calls": 4,
        }
        chord_mock.return_value.return_value = SimpleNamespace(id="fanout-task")
        TokenWallet.objects.create(user=self.user, free_tokens=5, paid_tokens=0)
        review = self._review()

        result = process_slip_review_import(review.id)

        review.refresh_from_db()
        wallet = TokenWallet.objects.get(user=self.user)
        reservation = TokenReservation.objects.get(user=self.user)
        tx = TokenTransaction.objects.get()
        self.assertEqual(review.status, SlipReview.Status.ANALYSING)
        self.assertEqual(review.submitted_payload["token_reservation_id"], reservation.id)
        self.assertEqual(review.submitted_payload["token_cost"], 2)
        self.assertEqual(review.summary["billing"]["status"], "reserved")
        self.assertEqual(review.summary["billing"]["token_cost"], 2)
        self.assertEqual(wallet.free_tokens, 3)
        self.assertEqual(reservation.amount, 2)
        self.assertEqual(reservation.status, TokenReservation.Status.RESERVED)
        self.assertEqual(tx.amount, -2)
        self.assertEqual(tx.reason, TokenTransaction.Reason.SLIP_REVIEW_RESERVE)
        self.assertEqual(result["fanout_task_id"], "fanout-task")

    def test_fail_slip_review_releases_reserved_tokens(self):
        from betpreneur.modules.slips.interface.views import fail_slip_review_import

        wallet = TokenWallet.objects.create(user=self.user, free_tokens=5, paid_tokens=0)
        review = self._review()
        reservation = TokenWalletService().reserve_tokens(
            self.user,
            4,
            reference_type="slip_review",
            reference_id=str(review.id),
        ).reservation
        review.submitted_payload = {"token_reservation_id": reservation.id, "selection_count": 4}
        review.save(update_fields=["submitted_payload", "updated_at"])

        fail_slip_review_import(review.id, "Slip review failed.", error_code="failed")

        wallet.refresh_from_db()
        reservation.refresh_from_db()
        self.assertEqual(wallet.free_tokens, 5)
        self.assertEqual(reservation.status, TokenReservation.Status.RELEASED)
        self.assertEqual(TokenTransaction.objects.filter(reason=TokenTransaction.Reason.SLIP_REVIEW_RELEASE).count(), 1)

    def test_completed_slip_review_consumes_reserved_tokens_without_second_debit(self):
        wallet = TokenWallet.objects.create(user=self.user, free_tokens=5, paid_tokens=0)
        review = self._review()
        reservation = TokenWalletService().reserve_tokens(
            self.user,
            3,
            reference_type="slip_review",
            reference_id=str(review.id),
        ).reservation
        review.submitted_payload = {"token_reservation_id": reservation.id, "selection_count": 3}
        review.save(update_fields=["submitted_payload", "updated_at"])

        result = _consume_slip_review_token_reservation(review)

        wallet.refresh_from_db()
        reservation.refresh_from_db()
        self.assertEqual(wallet.free_tokens, 2)
        self.assertEqual(reservation.status, TokenReservation.Status.CONSUMED)
        self.assertEqual(result.transaction.reason, TokenTransaction.Reason.SLIP_REVIEW_CONSUME)
        self.assertEqual(result.transaction.amount, 0)
        self.assertEqual(TokenTransaction.objects.filter(reason=TokenTransaction.Reason.SLIP_REVIEW_RESERVE).count(), 1)

    def test_slip_review_does_not_charge_games_that_need_review(self):
        wallet = TokenWallet.objects.create(user=self.user, free_tokens=5, paid_tokens=0)
        review = self._review()
        reservation = TokenWalletService().reserve_tokens(
            self.user,
            3,
            reference_type="slip_review",
            reference_id=str(review.id),
        ).reservation
        review.submitted_payload = {"token_reservation_id": reservation.id, "selection_count": 3}
        review.summary = {"analysed_count": 1, "not_assessed_count": 2}
        review.save(update_fields=["submitted_payload", "summary", "updated_at"])

        result = _consume_slip_review_token_reservation(review)

        wallet.refresh_from_db()
        reservation.refresh_from_db()
        self.assertEqual(wallet.free_tokens, 4)
        self.assertEqual(reservation.status, TokenReservation.Status.CONSUMED)
        self.assertEqual(review.summary["billing"]["token_cost"], 3)
        self.assertEqual(review.summary["billing"]["billable_games"], 1)
        self.assertEqual(review.summary["billing"]["charged_tokens"], 1)
        self.assertEqual(review.summary["billing"]["refunded_tokens"], 2)
        self.assertEqual(review.summary["billing"]["non_billable_games"], 2)
        self.assertEqual(result.transaction.metadata["charged_tokens"], 1)
