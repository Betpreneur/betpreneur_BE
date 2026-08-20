from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from apps.algo.models import SlipReview, TokenPurchase, TokenReservation, TokenTransaction, TokenWallet


log = logging.getLogger(__name__)


class InsufficientTokens(Exception):
    def __init__(self, *, required_tokens: int, available_tokens: int, wallet: TokenWallet):
        self.required_tokens = int(required_tokens)
        self.available_tokens = int(available_tokens)
        self.shortfall = max(self.required_tokens - self.available_tokens, 0)
        self.wallet = wallet
        super().__init__(
            f"Insufficient tokens: required={self.required_tokens} available={self.available_tokens}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": "insufficient_tokens",
            "required_tokens": self.required_tokens,
            "available_tokens": self.available_tokens,
            "shortfall": self.shortfall,
            "wallet": token_wallet_snapshot(self.wallet),
        }


@dataclass(frozen=True)
class TokenOperationResult:
    wallet: TokenWallet
    transaction: TokenTransaction | None = None
    reservation: TokenReservation | None = None
    free_tokens_used: int = 0
    paid_tokens_used: int = 0

    @property
    def balance_after(self) -> dict[str, int]:
        return token_wallet_snapshot(self.wallet)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "wallet": self.balance_after,
            "free_tokens_used": self.free_tokens_used,
            "paid_tokens_used": self.paid_tokens_used,
        }
        if self.transaction:
            payload["transaction_id"] = self.transaction.id
        if self.reservation:
            payload["reservation_id"] = self.reservation.id
            payload["reservation_status"] = self.reservation.status
            payload["expires_at"] = self.reservation.expires_at
        return payload


@dataclass(frozen=True)
class TokenRefillResult:
    wallet: TokenWallet
    transaction: TokenTransaction | None = None
    refilled: bool = False
    tokens_added: int = 0
    skipped_reason: str = ""

    @property
    def balance_after(self) -> dict[str, int]:
        return token_wallet_snapshot(self.wallet)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "refilled": self.refilled,
            "tokens_added": self.tokens_added,
            "skipped_reason": self.skipped_reason,
            "wallet": self.balance_after,
        }
        if self.transaction:
            payload["transaction_id"] = self.transaction.id
        return payload


@dataclass(frozen=True)
class TokenGrantResult:
    wallet: TokenWallet
    transaction: TokenTransaction | None = None
    granted: bool = False
    tokens_added: int = 0
    skipped_reason: str = ""

    @property
    def balance_after(self) -> dict[str, int]:
        return token_wallet_snapshot(self.wallet)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "granted": self.granted,
            "tokens_added": self.tokens_added,
            "skipped_reason": self.skipped_reason,
            "wallet": self.balance_after,
        }
        if self.transaction:
            payload["transaction_id"] = self.transaction.id
        return payload


@dataclass(frozen=True)
class TokenPurchaseResult:
    purchase: TokenPurchase
    wallet: TokenWallet
    transaction: TokenTransaction | None = None
    idempotent: bool = False

    @property
    def balance_after(self) -> dict[str, int]:
        return token_wallet_snapshot(self.wallet)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "purchase_id": self.purchase.id,
            "status": self.purchase.status,
            "idempotent": self.idempotent,
            "wallet": self.balance_after,
        }
        if self.transaction:
            payload["transaction_id"] = self.transaction.id
        return payload


def token_wallet_snapshot(wallet: TokenWallet) -> dict[str, int]:
    return {
        "free_tokens": int(wallet.free_tokens or 0),
        "paid_tokens": int(wallet.paid_tokens or 0),
        "total_tokens": int(wallet.total_tokens),
    }


def token_package_catalogue() -> list[dict[str, Any]]:
    raw = str(
        getattr(
            settings,
            "TOKEN_PURCHASE_PACKAGES",
            "240:990,480:1980,720:2970,960:3960,1200:4950",
        )
        or ""
    )
    currency = str(getattr(settings, "TOKEN_PURCHASE_CURRENCY", "NGN") or "NGN").upper()
    packages: list[dict[str, Any]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        token_text, amount_text = item.split(":", 1)
        try:
            tokens = int(token_text.strip())
            amount = int(amount_text.strip())
        except (TypeError, ValueError):
            continue
        if tokens <= 0 or amount <= 0:
            continue
        packages.append(
            {
                "id": f"tokens_{tokens}_{currency.lower()}_{amount}",
                "tokens": tokens,
                "amount": amount,
                "amount_kobo": amount * 100 if currency == "NGN" else amount,
                "currency": currency,
                "label": f"{tokens} Tokens",
            }
        )
    return packages


def token_package_by_id(package_id: str) -> dict[str, Any] | None:
    for package in token_package_catalogue():
        if package["id"] == package_id:
            return package
    return None


class TokenWalletService:
    def get_or_create_wallet(self, user) -> TokenWallet:
        wallet, _ = TokenWallet.objects.get_or_create(user=user)
        return wallet

    def available_balance(self, user) -> dict[str, int]:
        return token_wallet_snapshot(self.get_or_create_wallet(user))

    def create_token_purchase(
        self,
        user,
        *,
        package_id: str,
        provider: str = "",
        provider_reference: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> TokenPurchase:
        package = token_package_by_id(package_id)
        if not package:
            raise ValueError("Unknown token package.")

        return TokenPurchase.objects.create(
            user=user,
            package_id=package["id"],
            tokens=int(package["tokens"]),
            amount=int(package["amount"]),
            amount_kobo=int(package["amount_kobo"]),
            currency=str(package["currency"]).upper(),
            provider=provider or "",
            provider_reference=provider_reference or "",
            metadata=metadata or {},
        )

    def complete_token_purchase(
        self,
        purchase: TokenPurchase | int,
        *,
        provider: str = "",
        provider_reference: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> TokenPurchaseResult:
        purchase_id = purchase if isinstance(purchase, int) else purchase.id
        metadata = metadata or {}
        with transaction.atomic():
            purchase = (
                TokenPurchase.objects.select_for_update()
                .select_related("user", "credited_transaction")
                .get(pk=purchase_id)
            )
            wallet = self._locked_wallet(purchase.user)
            if purchase.status == TokenPurchase.Status.PAID:
                return TokenPurchaseResult(
                    purchase=purchase,
                    wallet=wallet,
                    transaction=purchase.credited_transaction,
                    idempotent=True,
                )
            if purchase.status != TokenPurchase.Status.PENDING:
                raise ValueError(f"Cannot complete token purchase with status={purchase.status!r}.")

            if provider:
                purchase.provider = provider
            if provider_reference:
                purchase.provider_reference = provider_reference
            purchase.metadata = self._merge_metadata(purchase.metadata or {}, metadata)

            tokens = int(purchase.tokens or 0)
            wallet.paid_tokens += tokens
            wallet.save(update_fields=["paid_tokens", "updated_at"])
            tx = self._create_transaction(
                user=purchase.user,
                wallet=wallet,
                amount=tokens,
                free_delta=0,
                paid_delta=tokens,
                reason=TokenTransaction.Reason.TOKEN_PURCHASE_CREDIT,
                reference_type="token_purchase",
                reference_id=str(purchase.id),
                metadata={
                    **metadata,
                    "purchase_id": purchase.id,
                    "package_id": purchase.package_id,
                    "provider": purchase.provider,
                    "provider_reference": purchase.provider_reference,
                    "amount": purchase.amount,
                    "amount_kobo": purchase.amount_kobo,
                    "currency": purchase.currency,
                },
            )
            purchase.status = TokenPurchase.Status.PAID
            purchase.credited_transaction = tx
            purchase.paid_at = timezone.now()
            purchase.save(
                update_fields=[
                    "status",
                    "provider",
                    "provider_reference",
                    "metadata",
                    "credited_transaction",
                    "paid_at",
                    "updated_at",
                ]
            )
            return TokenPurchaseResult(purchase=purchase, wallet=wallet, transaction=tx)

    def attach_purchase_payment(
        self,
        purchase: TokenPurchase | int,
        *,
        provider: str,
        provider_reference: str,
        metadata: dict[str, Any] | None = None,
    ) -> TokenPurchase:
        purchase_id = purchase if isinstance(purchase, int) else purchase.id
        with transaction.atomic():
            purchase = TokenPurchase.objects.select_for_update().get(pk=purchase_id)
            if purchase.status != TokenPurchase.Status.PENDING:
                raise ValueError(f"Cannot attach payment to token purchase with status={purchase.status!r}.")
            purchase.provider = provider
            purchase.provider_reference = provider_reference
            if metadata:
                purchase.metadata = self._merge_metadata(purchase.metadata or {}, metadata)
            purchase.save(update_fields=["provider", "provider_reference", "metadata", "updated_at"])
            return purchase

    def fail_token_purchase(
        self,
        purchase: TokenPurchase | int,
        *,
        provider: str = "",
        provider_reference: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> TokenPurchase:
        purchase_id = purchase if isinstance(purchase, int) else purchase.id
        with transaction.atomic():
            purchase = TokenPurchase.objects.select_for_update().get(pk=purchase_id)
            if purchase.status == TokenPurchase.Status.PAID:
                raise ValueError("Cannot fail a paid token purchase.")
            if provider:
                purchase.provider = provider
            if provider_reference:
                purchase.provider_reference = provider_reference
            purchase.status = TokenPurchase.Status.FAILED
            purchase.failed_at = timezone.now()
            if metadata:
                purchase.metadata = self._merge_metadata(purchase.metadata or {}, metadata)
            purchase.save(
                update_fields=[
                    "status",
                    "provider",
                    "provider_reference",
                    "metadata",
                    "failed_at",
                    "updated_at",
                ]
            )
            return purchase

    def grant_signup_tokens(self, user, *, reference_id: str = "") -> TokenGrantResult:
        """
        The one-off starting balance, granted when a new account is verified.

        Wallets are created lazily, so before this existed a new user had no wallet and
        no tokens until the 00:15 refill swept the user table -- their first slip review
        returned `insufficient_tokens` for up to a day after signing up.

        Idempotent on the ledger: the grant is keyed off whether a `signup_grant`
        transaction already exists for the user, so re-verifying, a replayed request, or
        an admin re-running it cannot mint a second allowance.
        """
        tokens = int(getattr(settings, "TOKEN_SIGNUP_GRANT", getattr(settings, "TOKEN_FREE_DAILY_CAP", 50)))
        with transaction.atomic():
            wallet = self._locked_wallet(user)
            already_granted = TokenTransaction.objects.filter(
                user=user,
                reason=TokenTransaction.Reason.SIGNUP_GRANT,
            ).exists()
            if already_granted:
                return TokenGrantResult(wallet=wallet, skipped_reason="already_granted")
            if tokens <= 0:
                return TokenGrantResult(wallet=wallet, skipped_reason="grant_disabled")

            run_date = timezone.localdate()
            wallet.free_tokens += tokens
            # Mark the free allowance as applied for today so the nightly refill does not
            # treat a brand-new wallet as one that has been waiting since midnight.
            wallet.last_free_refill_date = run_date
            wallet.save(update_fields=["free_tokens", "last_free_refill_date", "updated_at"])
            tx = self._create_transaction(
                user=user,
                wallet=wallet,
                amount=tokens,
                free_delta=tokens,
                paid_delta=0,
                reason=TokenTransaction.Reason.SIGNUP_GRANT,
                reference_type="signup_grant",
                reference_id=reference_id or str(getattr(user, "id", "")),
                metadata={"granted_on": run_date.isoformat(), "tokens": tokens},
            )
            return TokenGrantResult(
                wallet=wallet,
                transaction=tx,
                granted=True,
                tokens_added=tokens,
            )

    def refill_free_tokens(
        self,
        user,
        *,
        run_date=None,
        force: bool = False,
        reference_type: str = "daily_free_tokens",
        reference_id: str = "",
    ) -> TokenRefillResult:
        run_date = run_date or timezone.localdate()
        cap = int(getattr(settings, "TOKEN_FREE_DAILY_CAP", 50))
        threshold = int(getattr(settings, "TOKEN_FREE_REFILL_THRESHOLD", 10))
        with transaction.atomic():
            wallet = self._locked_wallet(user)
            if not force and wallet.last_free_refill_date == run_date:
                return TokenRefillResult(
                    wallet=wallet,
                    skipped_reason="already_refilled_today",
                )
            if not force and int(wallet.free_tokens or 0) > threshold:
                return TokenRefillResult(
                    wallet=wallet,
                    skipped_reason="free_balance_above_threshold",
                )

            tokens_added = max(cap - int(wallet.free_tokens or 0), 0)
            wallet.last_free_refill_date = run_date
            if tokens_added > 0:
                wallet.free_tokens += tokens_added
                wallet.save(update_fields=["free_tokens", "last_free_refill_date", "updated_at"])
                tx = self._create_transaction(
                    user=user,
                    wallet=wallet,
                    amount=tokens_added,
                    free_delta=tokens_added,
                    paid_delta=0,
                    reason=TokenTransaction.Reason.DAILY_FREE_REFILL,
                    reference_type=reference_type,
                    reference_id=reference_id or run_date.isoformat(),
                    metadata={"run_date": run_date.isoformat(), "cap": cap, "threshold": threshold},
                )
                return TokenRefillResult(
                    wallet=wallet,
                    transaction=tx,
                    refilled=True,
                    tokens_added=tokens_added,
                )

            wallet.save(update_fields=["last_free_refill_date", "updated_at"])
            return TokenRefillResult(wallet=wallet, skipped_reason="free_balance_at_cap")

    def refill_daily_free_tokens(self, *, run_date=None, limit: int | None = None) -> dict[str, Any]:
        run_date = run_date or timezone.localdate()
        users = get_user_model().objects.order_by("id")
        if limit:
            users = users[: int(limit)]

        considered = refilled = skipped = tokens_added = failed = 0
        errors: list[dict[str, str]] = []
        for user in users.iterator():
            considered += 1
            try:
                result = self.refill_free_tokens(user, run_date=run_date)
                if result.refilled:
                    refilled += 1
                    tokens_added += result.tokens_added
                else:
                    skipped += 1
            except Exception as exc:
                failed += 1
                if len(errors) < 20:
                    errors.append({"user_id": str(user.id), "error": str(exc)[:200]})

        return {
            "run_date": run_date.isoformat(),
            "considered": considered,
            "refilled": refilled,
            "skipped": skipped,
            "failed": failed,
            "tokens_added": tokens_added,
            "errors": errors,
        }

    def charge_tokens(
        self,
        user,
        amount: int,
        *,
        reason: str,
        reference_type: str = "",
        reference_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> TokenOperationResult:
        amount = self._clean_amount(amount)
        metadata = metadata or {}
        with transaction.atomic():
            wallet = self._locked_wallet(user)
            free_used, paid_used = self._debit_wallet(wallet, amount)
            tx = self._create_transaction(
                user=user,
                wallet=wallet,
                amount=-amount,
                free_delta=-free_used,
                paid_delta=-paid_used,
                reason=reason,
                reference_type=reference_type,
                reference_id=reference_id,
                metadata=metadata,
            )
            return TokenOperationResult(
                wallet=wallet,
                transaction=tx,
                free_tokens_used=free_used,
                paid_tokens_used=paid_used,
            )

    def adjust_tokens(
        self,
        user,
        *,
        free_tokens_delta: int = 0,
        paid_tokens_delta: int = 0,
        reference_type: str = "admin_adjustment",
        reference_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> TokenOperationResult:
        free_tokens_delta = int(free_tokens_delta or 0)
        paid_tokens_delta = int(paid_tokens_delta or 0)
        if free_tokens_delta == 0 and paid_tokens_delta == 0:
            raise ValueError("Token adjustment must change at least one bucket.")

        metadata = metadata or {}
        with transaction.atomic():
            wallet = self._locked_wallet(user)
            next_free = int(wallet.free_tokens or 0) + free_tokens_delta
            next_paid = int(wallet.paid_tokens or 0) + paid_tokens_delta
            if next_free < 0 or next_paid < 0:
                raise ValueError("Token adjustment cannot make a wallet bucket negative.")

            wallet.free_tokens = next_free
            wallet.paid_tokens = next_paid
            wallet.save(update_fields=["free_tokens", "paid_tokens", "updated_at"])
            tx = self._create_transaction(
                user=user,
                wallet=wallet,
                amount=free_tokens_delta + paid_tokens_delta,
                free_delta=free_tokens_delta,
                paid_delta=paid_tokens_delta,
                reason=TokenTransaction.Reason.ADMIN_ADJUSTMENT,
                reference_type=reference_type,
                reference_id=reference_id,
                metadata=metadata,
            )
            return TokenOperationResult(wallet=wallet, transaction=tx)

    def reserve_tokens(
        self,
        user,
        amount: int,
        *,
        reference_type: str = "",
        reference_id: str = "",
        metadata: dict[str, Any] | None = None,
        ttl_minutes: int | None = None,
    ) -> TokenOperationResult:
        amount = self._clean_amount(amount)
        metadata = metadata or {}
        ttl_minutes = (
            int(ttl_minutes)
            if ttl_minutes is not None
            else int(getattr(settings, "TOKEN_RESERVATION_TTL_MINUTES", 30))
        )
        with transaction.atomic():
            wallet = self._locked_wallet(user)
            free_used, paid_used = self._debit_wallet(wallet, amount)
            reservation = TokenReservation.objects.create(
                user=user,
                wallet=wallet,
                amount=amount,
                free_tokens_reserved=free_used,
                paid_tokens_reserved=paid_used,
                reference_type=reference_type,
                reference_id=reference_id,
                metadata=metadata,
                expires_at=timezone.now() + timedelta(minutes=ttl_minutes),
            )
            tx = self._create_transaction(
                user=user,
                wallet=wallet,
                amount=-amount,
                free_delta=-free_used,
                paid_delta=-paid_used,
                reason=TokenTransaction.Reason.SLIP_REVIEW_RESERVE,
                reference_type=reference_type,
                reference_id=reference_id,
                metadata={**metadata, "reservation_id": reservation.id},
            )
            return TokenOperationResult(
                wallet=wallet,
                transaction=tx,
                reservation=reservation,
                free_tokens_used=free_used,
                paid_tokens_used=paid_used,
            )

    def consume_reservation(self, reservation: TokenReservation | int) -> TokenOperationResult:
        with transaction.atomic():
            reservation = self._locked_reservation(reservation)
            wallet = TokenWallet.objects.select_for_update().get(pk=reservation.wallet_id)
            if reservation.status == TokenReservation.Status.CONSUMED:
                return TokenOperationResult(wallet=wallet, reservation=reservation)
            if reservation.status != TokenReservation.Status.RESERVED:
                raise ValueError(f"Cannot consume token reservation with status={reservation.status!r}.")

            reservation.status = TokenReservation.Status.CONSUMED
            reservation.consumed_at = timezone.now()
            reservation.save(update_fields=["status", "consumed_at", "updated_at"])
            tx = self._create_transaction(
                user=reservation.user,
                wallet=wallet,
                amount=0,
                free_delta=0,
                paid_delta=0,
                reason=TokenTransaction.Reason.SLIP_REVIEW_CONSUME,
                reference_type=reservation.reference_type,
                reference_id=reservation.reference_id,
                metadata={"reservation_id": reservation.id},
            )
            return TokenOperationResult(wallet=wallet, transaction=tx, reservation=reservation)

    def release_reservation(self, reservation: TokenReservation | int) -> TokenOperationResult:
        with transaction.atomic():
            reservation = self._locked_reservation(reservation)
            wallet = TokenWallet.objects.select_for_update().get(pk=reservation.wallet_id)
            if reservation.status == TokenReservation.Status.RELEASED:
                return TokenOperationResult(wallet=wallet, reservation=reservation)
            if reservation.status != TokenReservation.Status.RESERVED:
                raise ValueError(f"Cannot release token reservation with status={reservation.status!r}.")

            free_returned = int(reservation.free_tokens_reserved or 0)
            paid_returned = int(reservation.paid_tokens_reserved or 0)
            amount = free_returned + paid_returned
            wallet.free_tokens += free_returned
            wallet.paid_tokens += paid_returned
            wallet.save(update_fields=["free_tokens", "paid_tokens", "updated_at"])
            reservation.status = TokenReservation.Status.RELEASED
            reservation.released_at = timezone.now()
            reservation.save(update_fields=["status", "released_at", "updated_at"])
            tx = self._create_transaction(
                user=reservation.user,
                wallet=wallet,
                amount=amount,
                free_delta=free_returned,
                paid_delta=paid_returned,
                reason=TokenTransaction.Reason.SLIP_REVIEW_RELEASE,
                reference_type=reservation.reference_type,
                reference_id=reservation.reference_id,
                metadata={"reservation_id": reservation.id},
            )
            return TokenOperationResult(wallet=wallet, transaction=tx, reservation=reservation)

    def expire_reservation(self, reservation: TokenReservation | int) -> TokenOperationResult:
        with transaction.atomic():
            reservation = self._locked_reservation(reservation)
            wallet = TokenWallet.objects.select_for_update().get(pk=reservation.wallet_id)
            if reservation.status == TokenReservation.Status.EXPIRED:
                return TokenOperationResult(wallet=wallet, reservation=reservation)
            if reservation.status != TokenReservation.Status.RESERVED:
                raise ValueError(f"Cannot expire token reservation with status={reservation.status!r}.")

            free_returned = int(reservation.free_tokens_reserved or 0)
            paid_returned = int(reservation.paid_tokens_reserved or 0)
            amount = free_returned + paid_returned
            wallet.free_tokens += free_returned
            wallet.paid_tokens += paid_returned
            wallet.save(update_fields=["free_tokens", "paid_tokens", "updated_at"])
            reservation.status = TokenReservation.Status.EXPIRED
            reservation.released_at = timezone.now()
            reservation.save(update_fields=["status", "released_at", "updated_at"])
            tx = self._create_transaction(
                user=reservation.user,
                wallet=wallet,
                amount=amount,
                free_delta=free_returned,
                paid_delta=paid_returned,
                reason=TokenTransaction.Reason.TOKEN_RESERVATION_EXPIRE,
                reference_type=reservation.reference_type,
                reference_id=reservation.reference_id,
                metadata={"reservation_id": reservation.id, "expired": True},
            )
            return TokenOperationResult(wallet=wallet, transaction=tx, reservation=reservation)

    def _slip_review_delivery(self, reservation: TokenReservation) -> str:
        """
        Whether the work a reservation paid for actually reached the user.

        Expiry means "we never delivered, so give the tokens back". That is only
        true if the review did not complete. A reservation can still be sitting in
        `reserved` after a successful review -- `consume_reservation` is best-effort
        at the call site, so a DB blip there leaves the escrow open. Refunding it
        would hand back the tokens for a review the user already received.
        """
        if reservation.reference_type != "slip_review":
            return "undeliverable"
        try:
            status_value = (
                SlipReview.objects.filter(pk=int(reservation.reference_id))
                .values_list("status", flat=True)
                .first()
            )
        except (TypeError, ValueError):
            return "undeliverable"
        if status_value is None:
            return "undeliverable"
        if status_value in {SlipReview.Status.COMPLETED, SlipReview.Status.PARTIAL}:
            return "delivered"
        if status_value in {
            SlipReview.Status.QUEUED,
            SlipReview.Status.IMPORTING,
            SlipReview.Status.ANALYSING,
        }:
            # Still running. The stale-review recovery job drives it to a terminal
            # state, which releases the escrow; refunding a live review would let it
            # finish and be delivered for free.
            return "in_flight"
        return "undeliverable"

    def expire_stale_reservations(self, *, now=None, limit: int | None = None) -> dict[str, Any]:
        now = now or timezone.now()
        queryset = (
            TokenReservation.objects.filter(
                status=TokenReservation.Status.RESERVED,
                expires_at__lte=now,
            )
            .order_by("expires_at", "id")
            .values_list("id", flat=True)
        )
        if limit:
            queryset = queryset[: int(limit)]
        reservation_ids = list(queryset)

        expired = failed = tokens_released = 0
        consumed = deferred = tokens_recognised = 0
        errors: list[dict[str, str]] = []
        for reservation_id in reservation_ids:
            try:
                reservation = TokenReservation.objects.get(pk=reservation_id)
                delivery = self._slip_review_delivery(reservation)
                if delivery == "in_flight":
                    deferred += 1
                    continue
                if delivery == "delivered":
                    # The user has the review. Close the escrow as revenue, not as a
                    # refund. Reaching here means the consume at the call site failed.
                    result = self.consume_reservation(reservation_id)
                    if result.transaction:
                        consumed += 1
                        tokens_recognised += int(reservation.amount or 0)
                        log.warning(
                            "Reconciled a stale reservation for a delivered slip review "
                            "reservation=%s review=%s tokens=%s -- consume at the call site failed",
                            reservation_id,
                            reservation.reference_id,
                            reservation.amount,
                        )
                    continue
                result = self.expire_reservation(reservation_id)
                if result.transaction:
                    expired += 1
                    tokens_released += int(result.transaction.amount or 0)
            except Exception as exc:
                failed += 1
                if len(errors) < 20:
                    errors.append({"reservation_id": str(reservation_id), "error": str(exc)[:200]})

        return {
            "considered": len(reservation_ids),
            "expired": expired,
            "consumed": consumed,
            "deferred": deferred,
            "failed": failed,
            "tokens_released": tokens_released,
            "tokens_recognised": tokens_recognised,
            "errors": errors,
        }

    def _locked_wallet(self, user) -> TokenWallet:
        wallet = self.get_or_create_wallet(user)
        return TokenWallet.objects.select_for_update().get(pk=wallet.pk)

    def _locked_reservation(self, reservation: TokenReservation | int) -> TokenReservation:
        reservation_id = reservation if isinstance(reservation, int) else reservation.id
        return TokenReservation.objects.select_for_update().select_related("user").get(pk=reservation_id)

    def _debit_wallet(self, wallet: TokenWallet, amount: int) -> tuple[int, int]:
        if wallet.total_tokens < amount:
            raise InsufficientTokens(
                required_tokens=amount,
                available_tokens=wallet.total_tokens,
                wallet=wallet,
            )

        free_used = min(int(wallet.free_tokens or 0), amount)
        paid_used = amount - free_used
        wallet.free_tokens -= free_used
        wallet.paid_tokens -= paid_used
        wallet.save(update_fields=["free_tokens", "paid_tokens", "updated_at"])
        return free_used, paid_used

    def _create_transaction(
        self,
        *,
        user,
        wallet: TokenWallet,
        amount: int,
        free_delta: int,
        paid_delta: int,
        reason: str,
        reference_type: str,
        reference_id: str,
        metadata: dict[str, Any],
    ) -> TokenTransaction:
        return TokenTransaction.objects.create(
            user=user,
            wallet=wallet,
            amount=amount,
            free_tokens_delta=free_delta,
            paid_tokens_delta=paid_delta,
            token_bucket=self._token_bucket(free_delta, paid_delta),
            reason=reason,
            reference_type=reference_type,
            reference_id=str(reference_id) if reference_id is not None else "",
            balance_after=token_wallet_snapshot(wallet),
            metadata=metadata,
        )

    def _token_bucket(self, free_delta: int, paid_delta: int) -> str:
        if free_delta and not paid_delta:
            return TokenTransaction.TokenBucket.FREE
        if paid_delta and not free_delta:
            return TokenTransaction.TokenBucket.PAID
        return TokenTransaction.TokenBucket.MIXED

    def _clean_amount(self, amount: int) -> int:
        amount = int(amount)
        if amount <= 0:
            raise ValueError("Token amount must be greater than zero.")
        return amount

    def _merge_metadata(self, base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base or {})
        for key, value in (incoming or {}).items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = self._merge_metadata(merged[key], value)
            else:
                merged[key] = value
        return merged


token_wallet_service = TokenWalletService()
