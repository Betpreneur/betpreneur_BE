from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.algo.models import SlipReview, TokenPurchase, TokenReservation, TokenTransaction, TokenWallet
from apps.algo.payfonte import PayfonteError
from apps.algo.tasks import expire_token_reservations
from apps.algo.tokens import InsufficientTokens, TokenWalletService, token_package_by_id, token_package_catalogue
from apps.algo.views import _consume_slip_review_token_reservation, process_slip_review_import


class TokenWalletServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="token-user",
            email="token-user@example.com",
            password="test-pass",
        )
        self.service = TokenWalletService()

    def test_get_or_create_wallet_creates_empty_wallet(self):
        wallet = self.service.get_or_create_wallet(self.user)

        self.assertEqual(wallet.free_tokens, 0)
        self.assertEqual(wallet.paid_tokens, 0)
        self.assertEqual(self.service.available_balance(self.user)["total_tokens"], 0)

    @override_settings(
        TOKEN_PURCHASE_PACKAGES="240:990,480:1980,720:2970,960:3960,1200:4950",
        TOKEN_PURCHASE_CURRENCY="NGN",
    )
    def test_token_package_catalogue_exposes_stable_payment_ids(self):
        packages = token_package_catalogue()

        self.assertEqual([package["tokens"] for package in packages], [240, 480, 720, 960, 1200])
        self.assertEqual([package["amount"] for package in packages], [990, 1980, 2970, 3960, 4950])
        self.assertEqual(packages[0]["id"], "tokens_240_ngn_990")
        self.assertEqual(packages[0]["amount_kobo"], 99000)
        self.assertEqual(packages[0]["currency"], "NGN")
        self.assertEqual(token_package_by_id("tokens_720_ngn_2970")["tokens"], 720)
        self.assertIsNone(token_package_by_id("unknown"))

    @override_settings(TOKEN_PURCHASE_PACKAGES="240:990", TOKEN_PURCHASE_CURRENCY="NGN")
    def test_create_token_purchase_uses_package_catalogue(self):
        purchase = self.service.create_token_purchase(
            self.user,
            package_id="tokens_240_ngn_990",
            metadata={"source": "test"},
        )

        self.assertEqual(purchase.status, TokenPurchase.Status.PENDING)
        self.assertEqual(purchase.tokens, 240)
        self.assertEqual(purchase.amount, 990)
        self.assertEqual(purchase.amount_kobo, 99000)
        self.assertEqual(purchase.currency, "NGN")
        self.assertEqual(purchase.metadata, {"source": "test"})

    @override_settings(TOKEN_PURCHASE_PACKAGES="240:990", TOKEN_PURCHASE_CURRENCY="NGN")
    def test_complete_token_purchase_credits_paid_tokens_once(self):
        purchase = self.service.create_token_purchase(self.user, package_id="tokens_240_ngn_990")

        result = self.service.complete_token_purchase(
            purchase,
            provider="manual",
            provider_reference="pay-ref-1",
            metadata={"webhook": True},
        )
        second = self.service.complete_token_purchase(purchase.id, provider_reference="pay-ref-1")

        wallet = TokenWallet.objects.get(user=self.user)
        purchase.refresh_from_db()
        self.assertEqual(wallet.free_tokens, 0)
        self.assertEqual(wallet.paid_tokens, 240)
        self.assertEqual(purchase.status, TokenPurchase.Status.PAID)
        self.assertEqual(purchase.provider, "manual")
        self.assertEqual(purchase.provider_reference, "pay-ref-1")
        self.assertIsNotNone(purchase.credited_transaction)
        self.assertFalse(result.idempotent)
        self.assertTrue(second.idempotent)
        self.assertEqual(TokenTransaction.objects.count(), 1)

        tx = TokenTransaction.objects.get()
        self.assertEqual(tx.amount, 240)
        self.assertEqual(tx.free_tokens_delta, 0)
        self.assertEqual(tx.paid_tokens_delta, 240)
        self.assertEqual(tx.token_bucket, TokenTransaction.TokenBucket.PAID)
        self.assertEqual(tx.reason, TokenTransaction.Reason.TOKEN_PURCHASE_CREDIT)
        self.assertEqual(tx.reference_type, "token_purchase")
        self.assertEqual(tx.reference_id, str(purchase.id))
        self.assertEqual(tx.balance_after, {"free_tokens": 0, "paid_tokens": 240, "total_tokens": 240})

    @override_settings(TOKEN_PURCHASE_PACKAGES="240:990", TOKEN_PURCHASE_CURRENCY="NGN")
    def test_complete_token_purchase_rejects_failed_purchase(self):
        purchase = self.service.create_token_purchase(self.user, package_id="tokens_240_ngn_990")
        self.service.fail_token_purchase(purchase, metadata={"reason": "declined"})

        with self.assertRaises(ValueError):
            self.service.complete_token_purchase(purchase.id)

        self.assertFalse(TokenWallet.objects.filter(user=self.user, paid_tokens__gt=0).exists())
        self.assertEqual(TokenTransaction.objects.count(), 0)

    def test_charge_tokens_spends_free_tokens_first_and_writes_transaction(self):
        wallet = TokenWallet.objects.create(user=self.user, free_tokens=8, paid_tokens=10)

        result = self.service.charge_tokens(
            self.user,
            12,
            reason=TokenTransaction.Reason.SMART_RANDOMIZE_CHARGE,
            reference_type="slip_review",
            reference_id="42",
            metadata={"action": "randomize"},
        )

        wallet.refresh_from_db()
        self.assertEqual(wallet.free_tokens, 0)
        self.assertEqual(wallet.paid_tokens, 6)
        self.assertEqual(result.free_tokens_used, 8)
        self.assertEqual(result.paid_tokens_used, 4)
        self.assertEqual(result.balance_after, {"free_tokens": 0, "paid_tokens": 6, "total_tokens": 6})

        tx = TokenTransaction.objects.get()
        self.assertEqual(tx.amount, -12)
        self.assertEqual(tx.free_tokens_delta, -8)
        self.assertEqual(tx.paid_tokens_delta, -4)
        self.assertEqual(tx.token_bucket, TokenTransaction.TokenBucket.MIXED)
        self.assertEqual(tx.reason, TokenTransaction.Reason.SMART_RANDOMIZE_CHARGE)
        self.assertEqual(tx.reference_type, "slip_review")
        self.assertEqual(tx.reference_id, "42")
        self.assertEqual(tx.balance_after, {"free_tokens": 0, "paid_tokens": 6, "total_tokens": 6})

    def test_charge_tokens_raises_without_mutating_wallet_when_balance_is_low(self):
        wallet = TokenWallet.objects.create(user=self.user, free_tokens=3, paid_tokens=1)

        with self.assertRaises(InsufficientTokens) as caught:
            self.service.charge_tokens(
                self.user,
                5,
                reason=TokenTransaction.Reason.SMART_RANDOMIZE_CHARGE,
            )

        wallet.refresh_from_db()
        self.assertEqual(wallet.free_tokens, 3)
        self.assertEqual(wallet.paid_tokens, 1)
        self.assertEqual(TokenTransaction.objects.count(), 0)
        self.assertEqual(
            caught.exception.to_dict(),
            {
                "code": "insufficient_tokens",
                "required_tokens": 5,
                "available_tokens": 4,
                "shortfall": 1,
                "wallet": {"free_tokens": 3, "paid_tokens": 1, "total_tokens": 4},
            },
        )

    def test_adjust_tokens_updates_exact_buckets_and_writes_admin_transaction(self):
        wallet = TokenWallet.objects.create(user=self.user, free_tokens=5, paid_tokens=2)

        result = self.service.adjust_tokens(
            self.user,
            free_tokens_delta=-2,
            paid_tokens_delta=10,
            reference_id="support-ticket-1",
            metadata={"note": "Support correction"},
        )

        wallet.refresh_from_db()
        self.assertEqual(wallet.free_tokens, 3)
        self.assertEqual(wallet.paid_tokens, 12)
        tx = result.transaction
        self.assertEqual(tx.amount, 8)
        self.assertEqual(tx.free_tokens_delta, -2)
        self.assertEqual(tx.paid_tokens_delta, 10)
        self.assertEqual(tx.token_bucket, TokenTransaction.TokenBucket.MIXED)
        self.assertEqual(tx.reason, TokenTransaction.Reason.ADMIN_ADJUSTMENT)
        self.assertEqual(tx.reference_type, "admin_adjustment")
        self.assertEqual(tx.reference_id, "support-ticket-1")

    def test_adjust_tokens_rejects_bucket_negative_mutation(self):
        wallet = TokenWallet.objects.create(user=self.user, free_tokens=1, paid_tokens=0)

        with self.assertRaises(ValueError):
            self.service.adjust_tokens(self.user, free_tokens_delta=-2)

        wallet.refresh_from_db()
        self.assertEqual(wallet.free_tokens, 1)
        self.assertEqual(wallet.paid_tokens, 0)
        self.assertEqual(TokenTransaction.objects.count(), 0)

    @override_settings(TOKEN_RESERVATION_TTL_MINUTES=30)
    def test_reserve_tokens_holds_free_tokens_first_and_writes_transaction(self):
        wallet = TokenWallet.objects.create(user=self.user, free_tokens=6, paid_tokens=10)
        before = timezone.now()

        result = self.service.reserve_tokens(
            self.user,
            9,
            reference_type="slip_review",
            reference_id="99",
            metadata={"games": 9},
        )

        wallet.refresh_from_db()
        self.assertEqual(wallet.free_tokens, 0)
        self.assertEqual(wallet.paid_tokens, 7)
        self.assertEqual(result.free_tokens_used, 6)
        self.assertEqual(result.paid_tokens_used, 3)

        reservation = result.reservation
        self.assertIsNotNone(reservation)
        self.assertEqual(reservation.amount, 9)
        self.assertEqual(reservation.free_tokens_reserved, 6)
        self.assertEqual(reservation.paid_tokens_reserved, 3)
        self.assertEqual(reservation.status, TokenReservation.Status.RESERVED)
        self.assertEqual(reservation.reference_type, "slip_review")
        self.assertEqual(reservation.reference_id, "99")
        self.assertGreaterEqual(reservation.expires_at, before + timedelta(minutes=29))

        tx = TokenTransaction.objects.get()
        self.assertEqual(tx.amount, -9)
        self.assertEqual(tx.free_tokens_delta, -6)
        self.assertEqual(tx.paid_tokens_delta, -3)
        self.assertEqual(tx.reason, TokenTransaction.Reason.SLIP_REVIEW_RESERVE)
        self.assertEqual(tx.metadata["reservation_id"], reservation.id)

    def test_consume_reservation_marks_consumed_without_debiting_again(self):
        wallet = TokenWallet.objects.create(user=self.user, free_tokens=10, paid_tokens=5)
        reserved = self.service.reserve_tokens(self.user, 7, reference_type="slip_review", reference_id="5")
        wallet.refresh_from_db()
        self.assertEqual(wallet.free_tokens, 3)
        self.assertEqual(wallet.paid_tokens, 5)

        result = self.service.consume_reservation(reserved.reservation)

        wallet.refresh_from_db()
        result.reservation.refresh_from_db()
        self.assertEqual(wallet.free_tokens, 3)
        self.assertEqual(wallet.paid_tokens, 5)
        self.assertEqual(result.reservation.status, TokenReservation.Status.CONSUMED)
        self.assertIsNotNone(result.reservation.consumed_at)
        self.assertEqual(TokenTransaction.objects.count(), 2)
        self.assertEqual(result.transaction.amount, 0)
        self.assertEqual(result.transaction.reason, TokenTransaction.Reason.SLIP_REVIEW_CONSUME)

    def test_consume_reservation_amount_refunds_unbilled_tokens(self):
        wallet = TokenWallet.objects.create(user=self.user, free_tokens=6, paid_tokens=10)
        reserved = self.service.reserve_tokens(self.user, 9, reference_type="slip_review", reference_id="partial")
        wallet.refresh_from_db()
        self.assertEqual(wallet.free_tokens, 0)
        self.assertEqual(wallet.paid_tokens, 7)

        result = self.service.consume_reservation_amount(reserved.reservation, 7)

        wallet.refresh_from_db()
        result.reservation.refresh_from_db()
        self.assertEqual(wallet.free_tokens, 0)
        self.assertEqual(wallet.paid_tokens, 9)
        self.assertEqual(result.reservation.status, TokenReservation.Status.CONSUMED)
        release_tx = TokenTransaction.objects.get(reason=TokenTransaction.Reason.SLIP_REVIEW_RELEASE)
        consume_tx = TokenTransaction.objects.get(reason=TokenTransaction.Reason.SLIP_REVIEW_CONSUME)
        self.assertEqual(release_tx.amount, 2)
        self.assertEqual(release_tx.metadata["reason"], "non_billable_review_legs")
        self.assertEqual(consume_tx.metadata["charged_tokens"], 7)
        self.assertEqual(consume_tx.metadata["refunded_tokens"], 2)

    def test_release_reservation_restores_tokens_and_writes_transaction(self):
        wallet = TokenWallet.objects.create(user=self.user, free_tokens=2, paid_tokens=10)
        reserved = self.service.reserve_tokens(self.user, 6, reference_type="slip_review", reference_id="6")
        wallet.refresh_from_db()
        self.assertEqual(wallet.free_tokens, 0)
        self.assertEqual(wallet.paid_tokens, 6)

        result = self.service.release_reservation(reserved.reservation)

        wallet.refresh_from_db()
        result.reservation.refresh_from_db()
        self.assertEqual(wallet.free_tokens, 2)
        self.assertEqual(wallet.paid_tokens, 10)
        self.assertEqual(result.reservation.status, TokenReservation.Status.RELEASED)
        self.assertIsNotNone(result.reservation.released_at)
        self.assertEqual(TokenTransaction.objects.count(), 2)
        self.assertEqual(result.transaction.amount, 6)
        self.assertEqual(result.transaction.free_tokens_delta, 2)
        self.assertEqual(result.transaction.paid_tokens_delta, 4)
        self.assertEqual(result.transaction.reason, TokenTransaction.Reason.SLIP_REVIEW_RELEASE)

    def test_expire_reservation_restores_tokens_and_marks_expired(self):
        wallet = TokenWallet.objects.create(user=self.user, free_tokens=3, paid_tokens=10)
        reserved = self.service.reserve_tokens(self.user, 8, reference_type="slip_review", reference_id="88")
        wallet.refresh_from_db()
        self.assertEqual(wallet.free_tokens, 0)
        self.assertEqual(wallet.paid_tokens, 5)

        result = self.service.expire_reservation(reserved.reservation)

        wallet.refresh_from_db()
        result.reservation.refresh_from_db()
        self.assertEqual(wallet.free_tokens, 3)
        self.assertEqual(wallet.paid_tokens, 10)
        self.assertEqual(result.reservation.status, TokenReservation.Status.EXPIRED)
        self.assertIsNotNone(result.reservation.released_at)
        self.assertEqual(result.transaction.amount, 8)
        self.assertEqual(result.transaction.reason, TokenTransaction.Reason.TOKEN_RESERVATION_EXPIRE)
        self.assertTrue(result.transaction.metadata["expired"])

    def test_consume_and_release_are_idempotent_for_final_matching_status(self):
        TokenWallet.objects.create(user=self.user, free_tokens=5, paid_tokens=0)
        reserved = self.service.reserve_tokens(self.user, 5).reservation

        first = self.service.consume_reservation(reserved)
        second = self.service.consume_reservation(reserved)

        self.assertEqual(first.reservation.status, TokenReservation.Status.CONSUMED)
        self.assertEqual(second.reservation.status, TokenReservation.Status.CONSUMED)
        self.assertEqual(TokenTransaction.objects.count(), 2)

    @override_settings(TOKEN_FREE_DAILY_CAP=50, TOKEN_FREE_REFILL_THRESHOLD=10)
    def test_refill_free_tokens_tops_up_low_free_bucket_only(self):
        run_date = date(2026, 8, 19)
        wallet = TokenWallet.objects.create(user=self.user, free_tokens=7, paid_tokens=12)

        result = self.service.refill_free_tokens(self.user, run_date=run_date)

        wallet.refresh_from_db()
        self.assertTrue(result.refilled)
        self.assertEqual(result.tokens_added, 43)
        self.assertEqual(wallet.free_tokens, 50)
        self.assertEqual(wallet.paid_tokens, 12)
        self.assertEqual(wallet.last_free_refill_date, run_date)

        tx = TokenTransaction.objects.get()
        self.assertEqual(tx.amount, 43)
        self.assertEqual(tx.free_tokens_delta, 43)
        self.assertEqual(tx.paid_tokens_delta, 0)
        self.assertEqual(tx.token_bucket, TokenTransaction.TokenBucket.FREE)
        self.assertEqual(tx.reason, TokenTransaction.Reason.DAILY_FREE_REFILL)
        self.assertEqual(tx.reference_id, "2026-08-19")

    @override_settings(TOKEN_FREE_DAILY_CAP=50, TOKEN_FREE_REFILL_THRESHOLD=10)
    def test_refill_free_tokens_skips_when_free_bucket_is_above_threshold(self):
        wallet = TokenWallet.objects.create(user=self.user, free_tokens=30, paid_tokens=5)

        result = self.service.refill_free_tokens(self.user, run_date=date(2026, 8, 19))

        wallet.refresh_from_db()
        self.assertFalse(result.refilled)
        self.assertEqual(result.skipped_reason, "free_balance_above_threshold")
        self.assertEqual(wallet.free_tokens, 30)
        self.assertEqual(wallet.paid_tokens, 5)
        self.assertIsNone(wallet.last_free_refill_date)
        self.assertEqual(TokenTransaction.objects.count(), 0)

    @override_settings(TOKEN_FREE_DAILY_CAP=50, TOKEN_FREE_REFILL_THRESHOLD=10)
    def test_refill_free_tokens_skips_when_user_already_refilled_today(self):
        run_date = date(2026, 8, 19)
        wallet = TokenWallet.objects.create(
            user=self.user,
            free_tokens=2,
            paid_tokens=0,
            last_free_refill_date=run_date,
        )

        result = self.service.refill_free_tokens(self.user, run_date=run_date)

        wallet.refresh_from_db()
        self.assertFalse(result.refilled)
        self.assertEqual(result.skipped_reason, "already_refilled_today")
        self.assertEqual(wallet.free_tokens, 2)
        self.assertEqual(TokenTransaction.objects.count(), 0)

    @override_settings(TOKEN_FREE_DAILY_CAP=50, TOKEN_FREE_REFILL_THRESHOLD=10)
    def test_refill_daily_free_tokens_covers_existing_and_missing_wallets(self):
        user_model = get_user_model()
        low_user = user_model.objects.create_user(
            username="low-user",
            email="low-user@example.com",
            password="test-pass",
        )
        high_user = user_model.objects.create_user(
            username="high-user",
            email="high-user@example.com",
            password="test-pass",
        )
        missing_wallet_user = user_model.objects.create_user(
            username="missing-wallet",
            email="missing-wallet@example.com",
            password="test-pass",
        )
        TokenWallet.objects.create(user=self.user, free_tokens=30, paid_tokens=0)
        TokenWallet.objects.create(user=low_user, free_tokens=5, paid_tokens=0)
        TokenWallet.objects.create(user=high_user, free_tokens=25, paid_tokens=0)

        result = self.service.refill_daily_free_tokens(run_date=date(2026, 8, 19))

        self.assertEqual(result["considered"], 4)
        self.assertEqual(result["refilled"], 2)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["tokens_added"], 95)
        self.assertEqual(TokenWallet.objects.get(user=low_user).free_tokens, 50)
        self.assertEqual(TokenWallet.objects.get(user=missing_wallet_user).free_tokens, 50)
        self.assertEqual(TokenWallet.objects.get(user=high_user).free_tokens, 25)
        self.assertEqual(TokenTransaction.objects.count(), 2)

    def test_expire_stale_reservations_only_expires_elapsed_reservations(self):
        wallet = TokenWallet.objects.create(user=self.user, free_tokens=10, paid_tokens=0)
        expired = self.service.reserve_tokens(self.user, 4, reference_id="expired").reservation
        fresh = self.service.reserve_tokens(self.user, 3, reference_id="fresh").reservation
        now = timezone.now()
        TokenReservation.objects.filter(id=expired.id).update(expires_at=now - timedelta(minutes=1))
        TokenReservation.objects.filter(id=fresh.id).update(expires_at=now + timedelta(minutes=1))

        result = self.service.expire_stale_reservations(now=now)

        wallet.refresh_from_db()
        expired.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(result["considered"], 1)
        self.assertEqual(result["expired"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["tokens_released"], 4)
        self.assertEqual(wallet.free_tokens, 7)
        self.assertEqual(expired.status, TokenReservation.Status.EXPIRED)
        self.assertEqual(fresh.status, TokenReservation.Status.RESERVED)
        self.assertEqual(
            TokenTransaction.objects.filter(reason=TokenTransaction.Reason.TOKEN_RESERVATION_EXPIRE).count(),
            1,
        )

    def test_expire_token_reservations_task_runs_stale_reservation_sweep(self):
        wallet = TokenWallet.objects.create(user=self.user, free_tokens=6, paid_tokens=0)
        expired = self.service.reserve_tokens(self.user, 6, reference_id="task-expired").reservation
        TokenReservation.objects.filter(id=expired.id).update(expires_at=timezone.now() - timedelta(minutes=1))

        result = expire_token_reservations(limit=10)

        wallet.refresh_from_db()
        expired.refresh_from_db()
        self.assertEqual(result["expired"], 1)
        self.assertEqual(wallet.free_tokens, 6)
        self.assertEqual(expired.status, TokenReservation.Status.EXPIRED)


class TokenWalletApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="token-api-user",
            email="token-api-user@example.com",
            password="test-pass",
        )
        self.client = APIClient()

    def test_token_wallet_endpoint_requires_authentication(self):
        response = self.client.get("/api/algo/tokens/")

        self.assertEqual(response.status_code, 401)

    @override_settings(
        TOKEN_FREE_DAILY_CAP=50,
        TOKEN_FREE_REFILL_THRESHOLD=10,
        TOKEN_FREE_REFILL_HOUR=0,
        TOKEN_FREE_REFILL_MINUTE=15,
        SLIP_REVIEW_TOKEN_COST_PER_GAME=1,
        SLIP_REVIEW_RANDOMIZE_TOKEN_COST=5,
    )
    def test_token_wallet_endpoint_creates_wallet_and_returns_balance_only(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.get("/api/algo/tokens/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            payload["wallet"],
            {
                "free_tokens": 0,
                "paid_tokens": 0,
                "total_tokens": 0,
            },
        )
        self.assertNotIn("pricing", payload)
        self.assertNotIn("refill_policy", payload)
        self.assertNotIn("recent_transactions", payload)
        self.assertTrue(TokenWallet.objects.filter(user=self.user).exists())

    def test_token_wallet_endpoint_ignores_transaction_history(self):
        wallet = TokenWallet.objects.create(user=self.user, free_tokens=20, paid_tokens=4)
        TokenTransaction.objects.create(
            user=self.user,
            wallet=wallet,
            amount=20,
            free_tokens_delta=20,
            token_bucket=TokenTransaction.TokenBucket.FREE,
            reason=TokenTransaction.Reason.DAILY_FREE_REFILL,
            balance_after={"free_tokens": 20, "paid_tokens": 4, "total_tokens": 24},
        )
        TokenTransaction.objects.create(
            user=self.user,
            wallet=wallet,
            amount=-5,
            free_tokens_delta=-5,
            token_bucket=TokenTransaction.TokenBucket.FREE,
            reason=TokenTransaction.Reason.SMART_RANDOMIZE_CHARGE,
            reference_type="slip_review",
            reference_id="12",
            balance_after={"free_tokens": 15, "paid_tokens": 4, "total_tokens": 19},
        )
        self.client.force_authenticate(user=self.user)

        response = self.client.get("/api/algo/tokens/?transaction_limit=1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["wallet"]["free_tokens"], 20)
        self.assertEqual(payload["wallet"]["paid_tokens"], 4)
        self.assertEqual(payload["wallet"]["total_tokens"], 24)
        self.assertNotIn("recent_transactions", payload)

    def test_token_purchases_endpoint_returns_purchase_history_only(self):
        other_user = get_user_model().objects.create_user(
            username="other-token-api-user",
            email="other-token-api-user@example.com",
            password="test-pass",
        )
        wallet = TokenWallet.objects.create(user=self.user, free_tokens=10, paid_tokens=0)
        TokenWallet.objects.create(user=other_user, free_tokens=10, paid_tokens=0)
        TokenTransaction.objects.create(
            user=self.user,
            wallet=wallet,
            amount=10,
            free_tokens_delta=10,
            token_bucket=TokenTransaction.TokenBucket.FREE,
            reason=TokenTransaction.Reason.DAILY_FREE_REFILL,
            balance_after={"free_tokens": 10, "paid_tokens": 0, "total_tokens": 10},
        )
        purchase = TokenPurchase.objects.create(
            user=self.user,
            package_id="tokens_240_ngn_990",
            tokens=240,
            amount=990,
            amount_kobo=99000,
            currency="NGN",
            status=TokenPurchase.Status.PAID,
        )
        TokenPurchase.objects.create(
            user=other_user,
            package_id="tokens_480_ngn_1980",
            tokens=480,
            amount=1980,
            amount_kobo=198000,
            currency="NGN",
            status=TokenPurchase.Status.PAID,
        )
        self.client.force_authenticate(user=self.user)

        response = self.client.get("/api/algo/tokens/purchases/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(
            payload["purchases"][0],
            {
                "id": purchase.id,
                "date": purchase.created_at.isoformat(),
                "tokens": 240,
                "amount": 990,
                "currency": "NGN",
                "status": TokenPurchase.Status.PAID,
            },
        )

    def test_token_transactions_endpoint_is_removed(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.get("/api/algo/tokens/transactions/")

        self.assertEqual(response.status_code, 404)

    @override_settings(
        TOKEN_PURCHASE_PACKAGES="240:990,480:1980,720:2970,960:3960,1200:4950",
        TOKEN_PURCHASE_CURRENCY="NGN",
    )
    def test_token_packages_endpoint_returns_configured_catalogue(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.get("/api/algo/tokens/packages/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["currency"], "NGN")
        self.assertEqual(len(payload["packages"]), 5)
        self.assertEqual(
            payload["packages"][0],
            {
                "id": "tokens_240_ngn_990",
                "tokens": 240,
                "amount": 990,
                "amount_kobo": 99000,
                "currency": "NGN",
                "label": "240 Tokens",
            },
        )
        self.assertEqual(payload["packages"][-1]["id"], "tokens_1200_ngn_4950")
        self.assertNotIn("pricing", payload)
        self.assertNotIn("refill_policy", payload)

    @override_settings(
        TOKEN_PURCHASE_PACKAGES="240:990,480:1980",
        TOKEN_PURCHASE_CURRENCY="NGN",
        PAYFONTE_CLIENT_ID="client-id",
        PAYFONTE_CLIENT_SECRET="client-secret",
        PAYFONTE_VIRTUAL_ACCOUNT_TTL_MINUTES=30,
    )
    @patch("apps.algo.views.payfonte_client")
    def test_token_purchase_endpoint_creates_pending_purchase(self, client_mock):
        client_mock.return_value.direct_charge.return_value = {
            "reference": "PF-REF-1",
            "status": "pending",
            "amount": 198000,
            "bankTransfer": {
                "bankName": "Wema Bank",
                "accountNumber": "1234567890",
                "accountName": "BETPRENEUR TEST",
            },
        }
        self.client.force_authenticate(user=self.user)

        response = self.client.post(
            "/api/algo/tokens/purchases/",
            {"package_id": "tokens_480_ngn_1980", "metadata": {"screen": "wallet"}},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        purchase = TokenPurchase.objects.get(user=self.user)
        self.assertEqual(purchase.status, TokenPurchase.Status.PENDING)
        self.assertEqual(purchase.tokens, 480)
        self.assertEqual(purchase.amount, 1980)
        self.assertEqual(purchase.provider, "payfonte")
        self.assertEqual(purchase.provider_reference, "PF-REF-1")
        self.assertEqual(purchase.metadata["screen"], "wallet")
        self.assertEqual(purchase.metadata["payfonte"]["direct_charge"]["data"]["reference"], "PF-REF-1")
        self.assertEqual(payload["purchase"]["id"], purchase.id)
        self.assertEqual(payload["purchase"]["status"], TokenPurchase.Status.PENDING)
        self.assertEqual(payload["purchase"]["payment"]["provider_reference"], "PF-REF-1")
        self.assertEqual(payload["purchase"]["payment"]["bank_account"]["account_number"], "1234567890")
        self.assertEqual(payload["purchase"]["payment"]["validity_minutes"], 30)
        self.assertGreater(payload["purchase"]["payment"]["expires_in_seconds"], 0)
        self.assertLessEqual(payload["purchase"]["payment"]["expires_in_seconds"], 1800)
        self.assertIn("within 30 minutes", payload["purchase"]["payment"]["instructions"])
        self.assertTrue(payload["purchase"]["payment"]["expires_at"])
        self.assertEqual(payload["package"]["id"], "tokens_480_ngn_1980")
        self.assertFalse(TokenTransaction.objects.exists())
        request_payload = client_mock.return_value.direct_charge.call_args.args[0]
        self.assertEqual(request_payload["provider"], "bank-transfer-nigeria")
        self.assertEqual(request_payload["amount"], 198000)
        self.assertTrue(request_payload["reference"].startswith("BP-TOK-"))

    @override_settings(TOKEN_PURCHASE_PACKAGES="240:990", TOKEN_PURCHASE_CURRENCY="NGN")
    def test_token_purchase_endpoint_rejects_unknown_package(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.post(
            "/api/algo/tokens/purchases/",
            {"package_id": "tokens_999_ngn_999"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(TokenPurchase.objects.count(), 0)

    @override_settings(
        TOKEN_PURCHASE_PACKAGES="240:990",
        TOKEN_PURCHASE_CURRENCY="NGN",
        PAYFONTE_CLIENT_ID="client-id",
        PAYFONTE_CLIENT_SECRET="client-secret",
    )
    @patch("apps.algo.views.payfonte_client")
    def test_token_purchase_endpoint_marks_purchase_failed_when_payfonte_fails(self, client_mock):
        client_mock.return_value.direct_charge.side_effect = PayfonteError("provider unavailable")
        self.client.force_authenticate(user=self.user)

        response = self.client.post(
            "/api/algo/tokens/purchases/",
            {"package_id": "tokens_240_ngn_990"},
            format="json",
        )

        purchase = TokenPurchase.objects.get(user=self.user)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(purchase.status, TokenPurchase.Status.FAILED)
        self.assertEqual(purchase.provider, "payfonte")
        self.assertIn("provider unavailable", purchase.metadata["payfonte"]["initiation_error"])

    @override_settings(TOKEN_PURCHASE_PACKAGES="240:990", TOKEN_PURCHASE_CURRENCY="NGN")
    def test_token_purchase_endpoint_lists_current_user_purchases_only(self):
        other_user = get_user_model().objects.create_user(
            username="other-token-purchase-user",
            email="other-token-purchase-user@example.com",
            password="test-pass",
        )
        own = TokenWalletService().create_token_purchase(self.user, package_id="tokens_240_ngn_990")
        TokenWalletService().create_token_purchase(other_user, package_id="tokens_240_ngn_990")
        self.client.force_authenticate(user=self.user)

        response = self.client.get("/api/algo/tokens/purchases/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["purchases"][0]["id"], own.id)

    @override_settings(TOKEN_PURCHASE_PACKAGES="240:990", TOKEN_PURCHASE_CURRENCY="NGN")
    @patch("apps.algo.views.payfonte_client")
    def test_token_purchase_verify_credits_paid_tokens_after_payfonte_success(self, client_mock):
        service = TokenWalletService()
        purchase = service.create_token_purchase(self.user, package_id="tokens_240_ngn_990")
        service.attach_purchase_payment(
            purchase,
            provider="payfonte",
            provider_reference="PF-SUCCESS-1",
            metadata={"payfonte": {"direct_charge": {"data": {"reference": "PF-SUCCESS-1"}}}},
        )
        client_mock.return_value.verify_payment.return_value = {
            "reference": "PF-SUCCESS-1",
            "status": "success",
            "amount": 99000,
            "currency": "NGN",
        }
        self.client.force_authenticate(user=self.user)

        response = self.client.post(f"/api/algo/tokens/purchases/{purchase.id}/verify/", {}, format="json")
        second = self.client.post(f"/api/algo/tokens/purchases/{purchase.id}/verify/", {}, format="json")

        purchase.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(TokenWallet.objects.get(user=self.user).paid_tokens, 240)
        self.assertEqual(TokenTransaction.objects.count(), 1)
        self.assertEqual(purchase.status, TokenPurchase.Status.PAID)
        self.assertFalse(response.json()["idempotent"])
        self.assertTrue(second.json()["idempotent"])
        self.assertEqual(response.json()["payfonte_status"], "success")

    @override_settings(TOKEN_PURCHASE_PACKAGES="240:990", TOKEN_PURCHASE_CURRENCY="NGN")
    @patch("apps.algo.views.payfonte_client")
    def test_token_purchase_verify_keeps_purchase_pending_when_payfonte_is_pending(self, client_mock):
        service = TokenWalletService()
        purchase = service.create_token_purchase(self.user, package_id="tokens_240_ngn_990")
        service.attach_purchase_payment(purchase, provider="payfonte", provider_reference="PF-PENDING-1")
        client_mock.return_value.verify_payment.return_value = {
            "reference": "PF-PENDING-1",
            "status": "pending",
            "amount": 99000,
            "currency": "NGN",
        }
        self.client.force_authenticate(user=self.user)

        response = self.client.post(f"/api/algo/tokens/purchases/{purchase.id}/verify/", {}, format="json")

        purchase.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["payfonte_status"], "pending")
        self.assertEqual(purchase.status, TokenPurchase.Status.PENDING)
        self.assertEqual(TokenTransaction.objects.count(), 0)

    @override_settings(TOKEN_PURCHASE_PACKAGES="240:990", TOKEN_PURCHASE_CURRENCY="NGN")
    @patch("apps.algo.views.payfonte_client")
    def test_token_purchase_verify_fails_purchase_when_payfonte_fails(self, client_mock):
        service = TokenWalletService()
        purchase = service.create_token_purchase(self.user, package_id="tokens_240_ngn_990")
        service.attach_purchase_payment(purchase, provider="payfonte", provider_reference="PF-FAILED-1")
        client_mock.return_value.verify_payment.return_value = {
            "reference": "PF-FAILED-1",
            "status": "failed",
            "amount": 99000,
            "currency": "NGN",
        }
        self.client.force_authenticate(user=self.user)

        response = self.client.post(f"/api/algo/tokens/purchases/{purchase.id}/verify/", {}, format="json")

        purchase.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["payfonte_status"], "failed")
        self.assertEqual(purchase.status, TokenPurchase.Status.FAILED)
        self.assertEqual(TokenTransaction.objects.count(), 0)

    @override_settings(TOKEN_PURCHASE_PACKAGES="240:990", TOKEN_PURCHASE_CURRENCY="NGN")
    @patch("apps.algo.views.payfonte_client")
    def test_token_purchase_verify_rejects_amount_mismatch(self, client_mock):
        service = TokenWalletService()
        purchase = service.create_token_purchase(self.user, package_id="tokens_240_ngn_990")
        service.attach_purchase_payment(purchase, provider="payfonte", provider_reference="PF-BAD-AMOUNT")
        client_mock.return_value.verify_payment.return_value = {
            "reference": "PF-BAD-AMOUNT",
            "status": "success",
            "amount": 50000,
            "currency": "NGN",
        }
        self.client.force_authenticate(user=self.user)

        response = self.client.post(f"/api/algo/tokens/purchases/{purchase.id}/verify/", {}, format="json")

        purchase.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(purchase.status, TokenPurchase.Status.PENDING)
        self.assertEqual(TokenTransaction.objects.count(), 0)

    @override_settings(TOKEN_PURCHASE_PACKAGES="240:990", TOKEN_PURCHASE_CURRENCY="NGN")
    @patch("apps.algo.views.payfonte_client")
    def test_payfonte_webhook_verifies_before_crediting_tokens(self, client_mock):
        service = TokenWalletService()
        purchase = service.create_token_purchase(self.user, package_id="tokens_240_ngn_990")
        service.attach_purchase_payment(purchase, provider="payfonte", provider_reference="PF-WEBHOOK-1")
        client_mock.return_value.verify_payment.return_value = {
            "reference": "PF-WEBHOOK-1",
            "status": "success",
            "amount": 99000,
            "currency": "NGN",
        }

        response = self.client.post(
            "/api/algo/tokens/payfonte/webhook/",
            {"event": "payment.completed", "data": {"reference": "PF-WEBHOOK-1", "status": "success"}},
            format="json",
        )

        purchase.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "processed")
        self.assertEqual(purchase.status, TokenPurchase.Status.PAID)
        self.assertEqual(TokenWallet.objects.get(user=self.user).paid_tokens, 240)
        self.assertEqual(TokenTransaction.objects.count(), 1)

    @override_settings(TOKEN_PURCHASE_PACKAGES="240:990", TOKEN_PURCHASE_CURRENCY="NGN")
    @patch("apps.algo.views.payfonte_client")
    def test_payfonte_webhook_matches_external_reference_and_verifies_transaction_reference(self, client_mock):
        service = TokenWalletService()
        purchase = service.create_token_purchase(self.user, package_id="tokens_240_ngn_990")
        service.attach_purchase_payment(purchase, provider="payfonte", provider_reference="BP-TOK-6-a945b214")
        client_mock.return_value.verify_payment.return_value = {
            "reference": "DDC20260820223050CQCJL",
            "externalReference": "BP-TOK-6-a945b214",
            "status": "success",
            "amount": 99000,
            "currency": "NGN",
        }

        response = self.client.post(
            "/api/algo/tokens/payfonte/webhook/",
            {
                "event": "payment.completed",
                "reference": "DDC20260820223050CQCJL",
                "data": {
                    "reference": "DDC20260820223050CQCJL",
                    "paymentReference": "DDC20260820223050CQCJL",
                    "externalReference": "BP-TOK-6-a945b214",
                    "status": "success",
                    "amount": 99000,
                    "currency": "NGN",
                },
            },
            format="json",
        )

        purchase.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "processed")
        self.assertEqual(purchase.status, TokenPurchase.Status.PAID)
        client_mock.return_value.verify_payment.assert_called_once_with("DDC20260820223050CQCJL")

    @override_settings(TOKEN_PURCHASE_PACKAGES="240:990", TOKEN_PURCHASE_CURRENCY="NGN")
    @patch("apps.algo.views.payfonte_client")
    def test_payfonte_webhook_retries_verification_with_external_reference(self, client_mock):
        service = TokenWalletService()
        purchase = service.create_token_purchase(self.user, package_id="tokens_240_ngn_990")
        service.attach_purchase_payment(purchase, provider="payfonte", provider_reference="BP-TOK-6-a945b214")
        client_mock.return_value.verify_payment.side_effect = [
            PayfonteError("not found"),
            {
                "reference": "DDC20260820223050CQCJL",
                "externalReference": "BP-TOK-6-a945b214",
                "status": "success",
                "amount": 99000,
                "currency": "NGN",
            },
        ]

        response = self.client.post(
            "/api/algo/tokens/payfonte/webhook/",
            {
                "reference": "DDC20260820223050CQCJL",
                "data": {
                    "reference": "DDC20260820223050CQCJL",
                    "externalReference": "BP-TOK-6-a945b214",
                    "status": "success",
                },
            },
            format="json",
        )

        purchase.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "processed")
        self.assertEqual(purchase.status, TokenPurchase.Status.PAID)
        self.assertEqual(
            [call.args[0] for call in client_mock.return_value.verify_payment.call_args_list],
            ["DDC20260820223050CQCJL", "BP-TOK-6-a945b214"],
        )

    @override_settings(TOKEN_PURCHASE_PACKAGES="240:990", TOKEN_PURCHASE_CURRENCY="NGN")
    @patch("apps.algo.views.payfonte_client")
    def test_payfonte_webhook_returns_retry_when_verification_fails(self, client_mock):
        service = TokenWalletService()
        purchase = service.create_token_purchase(self.user, package_id="tokens_240_ngn_990")
        service.attach_purchase_payment(purchase, provider="payfonte", provider_reference="PF-RETRY-1")
        client_mock.return_value.verify_payment.side_effect = PayfonteError("timeout")

        response = self.client.post(
            "/api/algo/tokens/payfonte/webhook/",
            {"data": {"reference": "PF-RETRY-1"}},
            format="json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(TokenTransaction.objects.count(), 0)

    @override_settings(TOKEN_PURCHASE_PACKAGES="240:990", TOKEN_PURCHASE_CURRENCY="NGN")
    def test_token_purchase_admin_complete_rejects_non_admin_user(self):
        purchase = TokenWalletService().create_token_purchase(self.user, package_id="tokens_240_ngn_990")
        self.client.force_authenticate(user=self.user)

        response = self.client.post(
            f"/api/algo/tokens/purchases/{purchase.id}/admin-complete/",
            {"provider": "manual", "provider_reference": "ref-1"},
            format="json",
        )

        self.assertEqual(response.status_code, 403)
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, TokenPurchase.Status.PENDING)
        self.assertFalse(TokenTransaction.objects.exists())

    @override_settings(TOKEN_PURCHASE_PACKAGES="240:990", TOKEN_PURCHASE_CURRENCY="NGN")
    def test_token_purchase_admin_complete_credits_paid_tokens_once(self):
        admin = get_user_model().objects.create_user(
            username="token-purchase-admin",
            email="token-purchase-admin@example.com",
            password="test-pass",
            is_staff=True,
        )
        purchase = TokenWalletService().create_token_purchase(self.user, package_id="tokens_240_ngn_990")
        self.client.force_authenticate(user=admin)

        first = self.client.post(
            f"/api/algo/tokens/purchases/{purchase.id}/admin-complete/",
            {"provider": "manual", "provider_reference": "ref-1", "metadata": {"note": "cash received"}},
            format="json",
        )
        second = self.client.post(
            f"/api/algo/tokens/purchases/{purchase.id}/admin-complete/",
            {"provider": "manual", "provider_reference": "ref-1"},
            format="json",
        )

        wallet = TokenWallet.objects.get(user=self.user)
        purchase.refresh_from_db()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(wallet.paid_tokens, 240)
        self.assertEqual(purchase.status, TokenPurchase.Status.PAID)
        self.assertEqual(TokenTransaction.objects.count(), 1)
        self.assertFalse(first.json()["idempotent"])
        self.assertTrue(second.json()["idempotent"])
        self.assertEqual(first.json()["wallet"], {"free_tokens": 0, "paid_tokens": 240, "total_tokens": 240})
        self.assertEqual(first.json()["transaction"]["reason"], TokenTransaction.Reason.TOKEN_PURCHASE_CREDIT)

    @override_settings(TOKEN_PURCHASE_PACKAGES="240:990", TOKEN_PURCHASE_CURRENCY="NGN")
    def test_token_purchase_admin_fail_marks_pending_purchase_failed(self):
        admin = get_user_model().objects.create_user(
            username="token-purchase-fail-admin",
            email="token-purchase-fail-admin@example.com",
            password="test-pass",
            is_staff=True,
        )
        purchase = TokenWalletService().create_token_purchase(self.user, package_id="tokens_240_ngn_990")
        self.client.force_authenticate(user=admin)

        response = self.client.post(
            f"/api/algo/tokens/purchases/{purchase.id}/admin-fail/",
            {"provider": "manual", "provider_reference": "ref-failed", "metadata": {"reason": "declined"}},
            format="json",
        )

        purchase.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(purchase.status, TokenPurchase.Status.FAILED)
        self.assertEqual(purchase.provider_reference, "ref-failed")
        self.assertEqual(purchase.metadata["reason"], "declined")
        self.assertFalse(TokenTransaction.objects.exists())

    @override_settings(TOKEN_PURCHASE_PACKAGES="240:990", TOKEN_PURCHASE_CURRENCY="NGN")
    def test_token_purchase_admin_fail_rejects_paid_purchase(self):
        admin = get_user_model().objects.create_user(
            username="token-purchase-paid-admin",
            email="token-purchase-paid-admin@example.com",
            password="test-pass",
            is_staff=True,
        )
        purchase = TokenWalletService().create_token_purchase(self.user, package_id="tokens_240_ngn_990")
        TokenWalletService().complete_token_purchase(purchase)
        self.client.force_authenticate(user=admin)

        response = self.client.post(
            f"/api/algo/tokens/purchases/{purchase.id}/admin-fail/",
            {"provider_reference": "too-late"},
            format="json",
        )

        purchase.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(purchase.status, TokenPurchase.Status.PAID)
        self.assertEqual(TokenWallet.objects.get(user=self.user).paid_tokens, 240)
        self.assertEqual(TokenTransaction.objects.count(), 1)

    def test_admin_adjustment_endpoint_rejects_non_admin_user(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.post(
            "/api/algo/tokens/admin/adjust/",
            {"user_id": self.user.id, "paid_tokens_delta": 20},
            format="json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(TokenWallet.objects.filter(user=self.user).exists())

    def test_admin_adjustment_endpoint_credits_user_wallet(self):
        admin = get_user_model().objects.create_user(
            username="token-admin",
            email="token-admin@example.com",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_authenticate(user=admin)

        response = self.client.post(
            "/api/algo/tokens/admin/adjust/",
            {
                "email": self.user.email,
                "paid_tokens_delta": 240,
                "note": "Manual package credit",
                "reference_id": "manual-credit-1",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["user"]["id"], self.user.id)
        self.assertEqual(payload["wallet"], {"free_tokens": 0, "paid_tokens": 240, "total_tokens": 240})
        self.assertEqual(payload["transaction"]["amount"], 240)
        self.assertEqual(payload["transaction"]["paid_tokens_delta"], 240)
        self.assertEqual(payload["transaction"]["reason"], TokenTransaction.Reason.ADMIN_ADJUSTMENT)
        tx = TokenTransaction.objects.get(user=self.user)
        self.assertEqual(tx.metadata["note"], "Manual package credit")
        self.assertEqual(tx.metadata["admin_user_id"], admin.id)

    def test_admin_adjustment_endpoint_rejects_negative_bucket_result(self):
        admin = get_user_model().objects.create_user(
            username="token-admin-negative",
            email="token-admin-negative@example.com",
            password="test-pass",
            is_staff=True,
        )
        TokenWallet.objects.create(user=self.user, free_tokens=1, paid_tokens=0)
        self.client.force_authenticate(user=admin)

        response = self.client.post(
            "/api/algo/tokens/admin/adjust/",
            {"user_id": self.user.id, "free_tokens_delta": -2},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(TokenWallet.objects.get(user=self.user).free_tokens, 1)
        self.assertEqual(TokenTransaction.objects.count(), 0)


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
    @patch("apps.algo.views.SportyBetShareImporter.import_share")
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
    @patch("apps.algo.views.plan_slip_hydration")
    @patch("celery.chord")
    @patch("apps.algo.views.SportyBetShareImporter.import_share")
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
        from apps.algo.views import fail_slip_review_import

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
            "apps.algo.views.token_wallet_service.consume_reservation_amount",
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


class SignupTokenGrantTests(TestCase):
    """
    A newly verified account starts with a usable balance.

    Wallets are created lazily, so before this a new user had no wallet and no tokens
    until the 00:15 sweep -- their first slip review returned `insufficient_tokens` for
    up to a day after signing up.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="granted-user",
            email="granted@example.com",
            password="test-pass",
        )
        self.service = TokenWalletService()

    @override_settings(TOKEN_SIGNUP_GRANT=50)
    def test_grant_creates_the_wallet_and_credits_free_tokens(self):
        self.assertEqual(TokenWallet.objects.filter(user=self.user).count(), 0)

        result = self.service.grant_signup_tokens(self.user)

        self.assertTrue(result.granted)
        self.assertEqual(result.tokens_added, 50)
        self.assertEqual(result.balance_after, {"free_tokens": 50, "paid_tokens": 0, "total_tokens": 50})

    @override_settings(TOKEN_SIGNUP_GRANT=50)
    def test_the_grant_lands_in_the_free_bucket_not_the_paid_one(self):
        """A gift must not be refundable as paid tokens -- see the release accounting."""
        self.service.grant_signup_tokens(self.user)
        wallet = TokenWallet.objects.get(user=self.user)

        self.assertEqual(wallet.free_tokens, 50)
        self.assertEqual(wallet.paid_tokens, 0)

    @override_settings(TOKEN_SIGNUP_GRANT=50)
    def test_grant_is_written_to_the_ledger(self):
        result = self.service.grant_signup_tokens(self.user)

        tx = TokenTransaction.objects.get(pk=result.transaction.id)
        self.assertEqual(tx.reason, TokenTransaction.Reason.SIGNUP_GRANT)
        self.assertEqual(tx.amount, 50)
        self.assertEqual(tx.free_tokens_delta, 50)
        self.assertEqual(tx.balance_after["total_tokens"], 50)

    @override_settings(TOKEN_SIGNUP_GRANT=50)
    def test_a_second_grant_does_not_mint_a_second_allowance(self):
        self.service.grant_signup_tokens(self.user)

        result = self.service.grant_signup_tokens(self.user)

        self.assertFalse(result.granted)
        self.assertEqual(result.skipped_reason, "already_granted")
        self.assertEqual(TokenWallet.objects.get(user=self.user).total_tokens, 50)

    @override_settings(TOKEN_SIGNUP_GRANT=50)
    def test_a_replayed_grant_does_not_restore_spent_tokens(self):
        """Idempotency is keyed off the ledger, not the current balance."""
        self.service.grant_signup_tokens(self.user)
        self.service.charge_tokens(
            self.user, 30, reason=TokenTransaction.Reason.SMART_RANDOMIZE_CHARGE
        )

        self.service.grant_signup_tokens(self.user)

        self.assertEqual(TokenWallet.objects.get(user=self.user).total_tokens, 20)

    @override_settings(TOKEN_SIGNUP_GRANT=50)
    def test_the_new_user_can_immediately_run_a_slip_review(self):
        self.service.grant_signup_tokens(self.user)

        result = self.service.reserve_tokens(
            self.user, 3, reference_type="slip_review", reference_id="1"
        )

        self.assertEqual(result.free_tokens_used, 3)
        self.assertEqual(result.balance_after["total_tokens"], 47)

    @override_settings(TOKEN_SIGNUP_GRANT=50, TOKEN_FREE_DAILY_CAP=50, TOKEN_FREE_REFILL_THRESHOLD=10)
    def test_the_nightly_refill_does_not_top_up_again_the_same_day(self):
        self.service.grant_signup_tokens(self.user)

        result = self.service.refill_free_tokens(self.user)

        self.assertFalse(result.refilled)
        self.assertEqual(result.skipped_reason, "already_refilled_today")

    @override_settings(TOKEN_SIGNUP_GRANT=50, TOKEN_FREE_DAILY_CAP=50, TOKEN_FREE_REFILL_THRESHOLD=10)
    def test_a_spent_down_wallet_still_refills_the_next_day(self):
        self.service.grant_signup_tokens(self.user)
        self.service.charge_tokens(
            self.user, 45, reason=TokenTransaction.Reason.SLIP_REVIEW_CONSUME
        )

        result = self.service.refill_free_tokens(
            self.user, run_date=timezone.localdate() + timedelta(days=1)
        )

        self.assertTrue(result.refilled)
        self.assertEqual(result.balance_after["free_tokens"], 50)

    @override_settings(TOKEN_SIGNUP_GRANT=0)
    def test_the_grant_can_be_switched_off(self):
        result = self.service.grant_signup_tokens(self.user)

        self.assertFalse(result.granted)
        self.assertEqual(result.skipped_reason, "grant_disabled")


class VerifyEmailGrantTests(TestCase):
    """The grant is tied to email verification, not to account creation."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="verify-user",
            email="verify@example.com",
            password="test-pass",
        )
        self.user.is_email_verified = False
        self.user.email_verification_code = "123456"
        self.user.email_verification_sent_at = timezone.now()
        self.user.save()

    @override_settings(TOKEN_SIGNUP_GRANT=50)
    def test_an_unverified_account_has_no_tokens(self):
        self.assertEqual(TokenWallet.objects.filter(user=self.user).count(), 0)

    @override_settings(TOKEN_SIGNUP_GRANT=50)
    def test_verifying_the_email_grants_the_starting_balance(self):
        response = APIClient().post(
            "/api/auth/verify-email/",
            {"email": "verify@example.com", "code": "123456"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tokens"]["tokens_added"], 50)
        self.assertEqual(TokenWallet.objects.get(user=self.user).free_tokens, 50)

    @override_settings(TOKEN_SIGNUP_GRANT=50)
    def test_a_wrong_code_grants_nothing(self):
        response = APIClient().post(
            "/api/auth/verify-email/",
            {"email": "verify@example.com", "code": "000000"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(TokenWallet.objects.filter(user=self.user).count(), 0)

    @override_settings(TOKEN_SIGNUP_GRANT=50)
    def test_verification_still_succeeds_when_the_grant_fails(self):
        """The account is already verified by then; failing the request would strand it."""
        with patch(
            "apps.algo.tokens.TokenWalletService.grant_signup_tokens",
            side_effect=RuntimeError("wallet backend down"),
        ):
            response = APIClient().post(
                "/api/auth/verify-email/",
                {"email": "verify@example.com", "code": "123456"},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_email_verified)
        self.assertIsNone(response.json()["tokens"])
