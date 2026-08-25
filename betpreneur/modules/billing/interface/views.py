"""Token wallet, packages, purchases and the Payfonte webhook.

Routes are unchanged: these are still served at /api/algo/tokens/... because
the public API is frozen. config/urls.py mounts this module at that prefix.
"""
from __future__ import annotations

import logging
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from betpreneur.modules.billing.interface.serializers import (
    TokenAdminAdjustmentRequestSerializer,
    TokenAdminAdjustmentResponseSerializer,
    TokenPackageListResponseSerializer,
    TokenPurchaseAdminCompleteRequestSerializer,
    TokenPurchaseAdminCompleteResponseSerializer,
    TokenPurchaseAdminFailRequestSerializer,
    TokenPurchaseAdminFailResponseSerializer,
    TokenPurchaseCreateRequestSerializer,
    TokenPurchaseCreateResponseSerializer,
    TokenPurchaseListResponseSerializer,
    TokenPurchaseSerializer,
    TokenPurchaseVerifyResponseSerializer,
    TokenTransactionSerializer,
    TokenWalletResponseSerializer,
)
from betpreneur.modules.billing.models import (
    TokenPurchase,
)
from betpreneur.modules.billing.services.payments import (
    PayfonteError,
    build_direct_charge_payload,
    payfonte_client,
)
from betpreneur.modules.billing.services.wallet import (
    token_package_by_id,
    token_package_catalogue,
    token_wallet_service,
    token_wallet_snapshot,
)

log = logging.getLogger(__name__)
User = get_user_model()


def _token_pricing_payload():
    return {
        "slip_review_token_cost_per_game": int(getattr(settings, "SLIP_REVIEW_TOKEN_COST_PER_GAME", 1)),
        "smart_randomize_token_cost": int(getattr(settings, "SLIP_REVIEW_RANDOMIZE_TOKEN_COST", 5)),
    }


def _token_refill_policy_payload():
    return {
        "signup_grant": int(getattr(settings, "TOKEN_SIGNUP_GRANT", getattr(settings, "TOKEN_FREE_DAILY_CAP", 50))),
        "free_daily_cap": int(getattr(settings, "TOKEN_FREE_DAILY_CAP", 50)),
        "free_refill_threshold": int(getattr(settings, "TOKEN_FREE_REFILL_THRESHOLD", 10)),
        "refill_hour": int(getattr(settings, "TOKEN_FREE_REFILL_HOUR", 0)),
        "refill_minute": int(getattr(settings, "TOKEN_FREE_REFILL_MINUTE", 15)),
    }


def _token_wallet_payload(user):
    wallet = token_wallet_service.get_or_create_wallet(user)
    wallet_payload = token_wallet_snapshot(wallet)
    return {
        "wallet": wallet_payload,
    }


def _payfonte_webhook_url(request):
    configured = str(getattr(settings, "PAYFONTE_WEBHOOK_URL", "") or "").strip()
    if configured:
        return configured
    return request.build_absolute_uri("/api/algo/tokens/payfonte/webhook/")


def _payfonte_purchase_reference(purchase):
    return f"BP-TOK-{purchase.id}-{secrets.token_hex(4)}"


def _initiate_payfonte_purchase(request, purchase):
    reference = _payfonte_purchase_reference(purchase)
    payload = build_direct_charge_payload(
        purchase=purchase,
        reference=reference,
        webhook_url=_payfonte_webhook_url(request),
        user=request.user,
    )
    data = payfonte_client().direct_charge(payload)
    provider_reference = str(data.get("reference") or reference)
    return token_wallet_service.attach_purchase_payment(
        purchase,
        provider="payfonte",
        provider_reference=provider_reference,
        metadata={
            "payfonte": {
                "direct_charge": {
                    "request": {**payload, "customerInput": payload.get("customerInput") or {}},
                    "data": data,
                }
            }
        },
    )


def _verified_payfonte_amount_matches(purchase, data):
    amount = data.get("amount")
    currency = data.get("currency")
    if amount is not None and int(amount) != int(purchase.amount_kobo):
        raise ValueError("Verified Payfonte amount does not match this token purchase.")
    if currency and str(currency).upper() != str(purchase.currency).upper():
        raise ValueError("Verified Payfonte currency does not match this token purchase.")


def _settle_payfonte_purchase(purchase, *, verification_data=None, verification_reference=None):
    reference = purchase.provider_reference
    if not reference:
        raise ValueError("Token purchase has no Payfonte reference to verify.")
    data = verification_data or payfonte_client().verify_payment(verification_reference or reference)
    status_value = str(data.get("status") or "").lower()
    metadata = {"payfonte": {"verification": {"data": data, "verified_at": timezone.now().isoformat()}}}
    if status_value == "success":
        _verified_payfonte_amount_matches(purchase, data)
        result = token_wallet_service.complete_token_purchase(
            purchase,
            provider="payfonte",
            provider_reference=reference,
            metadata=metadata,
        )
        return {
            "purchase": result.purchase,
            "wallet": result.balance_after,
            "transaction": result.transaction,
            "idempotent": result.idempotent,
            "payfonte_status": status_value,
        }
    if status_value in {"failed", "rejected", "expired", "reversed"}:
        purchase = token_wallet_service.fail_token_purchase(
            purchase,
            provider="payfonte",
            provider_reference=reference,
            metadata=metadata,
        )
        return {
            "purchase": purchase,
            "wallet": {},
            "transaction": None,
            "idempotent": False,
            "payfonte_status": status_value,
        }

    purchase = token_wallet_service.attach_purchase_payment(
        purchase,
        provider="payfonte",
        provider_reference=reference,
        metadata=metadata,
    )
    return {
        "purchase": purchase,
        "wallet": {},
        "transaction": None,
        "idempotent": False,
        "payfonte_status": status_value or "pending",
    }


def _token_purchase_verify_payload(result):
    return {
        "purchase": TokenPurchaseSerializer(result["purchase"]).data,
        "wallet": result.get("wallet") or {},
        "transaction": TokenTransactionSerializer(result["transaction"]).data if result.get("transaction") else None,
        "idempotent": bool(result.get("idempotent")),
        "payfonte_status": result.get("payfonte_status") or "",
    }


def _payfonte_webhook_references(payload, data):
    candidates = [
        data.get("externalReference"),
        data.get("external_reference"),
        data.get("externalReference".lower()),
        payload.get("externalReference"),
        payload.get("external_reference"),
        data.get("reference"),
        data.get("paymentReference"),
        data.get("payment_reference"),
        data.get("providerReference"),
        payload.get("reference"),
    ]
    return [str(value).strip() for value in candidates if value]


def _payfonte_transaction_reference(payload, data):
    for value in (
        data.get("reference"),
        data.get("paymentReference"),
        data.get("payment_reference"),
        payload.get("reference"),
    ):
        if value:
            return str(value).strip()
    references = _payfonte_webhook_references(payload, data)
    return references[0] if references else ""


def _unique_payfonte_references(*values):
    seen = set()
    references = []
    for value in values:
        if not value:
            continue
        reference = str(value).strip()
        if not reference or reference in seen:
            continue
        seen.add(reference)
        references.append(reference)
    return references






class TokenWalletView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = TokenWalletResponseSerializer

    @extend_schema(
        summary="Token wallet",
        description=(
            "Authenticated user endpoint. Returns the current user's free and paid token balances."
        ),
        tags=["Tokens"],
        responses={200: TokenWalletResponseSerializer},
    )
    def get(self, request):
        return Response(_token_wallet_payload(request.user))


class TokenPackageListView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = TokenPackageListResponseSerializer

    @extend_schema(
        summary="Token packages",
        description=(
            "Authenticated user endpoint. Returns the configured token purchase packages. "
            "This does not initiate payment; payment integration will validate against these package ids."
        ),
        tags=["Tokens"],
        responses={200: TokenPackageListResponseSerializer},
    )
    def get(self, request):
        packages = token_package_catalogue()
        return Response(
            {
                "currency": str(getattr(settings, "TOKEN_PURCHASE_CURRENCY", "NGN") or "NGN").upper(),
                "packages": packages,
            }
        )


class TokenPurchaseView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = TokenPurchaseListResponseSerializer

    @extend_schema(
        summary="Token purchases",
        description=(
            "Authenticated user endpoint. GET returns the current user's token purchase records. "
            "POST creates a pending token purchase and asks Payfonte for bank-transfer payment details."
        ),
        tags=["Tokens"],
        responses={200: TokenPurchaseListResponseSerializer},
    )
    def get(self, request):
        try:
            limit = int(request.query_params.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 200))
        purchases = TokenPurchase.objects.filter(user=request.user).order_by("-created_at", "-id")[:limit]
        return Response(
            {
                "count": len(purchases),
                "purchases": [
                    {
                        "id": purchase.id,
                        "date": purchase.created_at.isoformat() if purchase.created_at else None,
                        "tokens": int(purchase.tokens or 0),
                        "amount": int(purchase.amount or 0),
                        "currency": purchase.currency,
                        "status": purchase.status,
                    }
                    for purchase in purchases
                ],
            }
        )

    @extend_schema(
        summary="Create token purchase",
        description=(
            "Creates a pending purchase record from one of the configured token packages and generates "
            "Payfonte bank-transfer payment details. Tokens are credited only after Payfonte verification succeeds."
        ),
        tags=["Tokens"],
        request=TokenPurchaseCreateRequestSerializer,
        responses={201: TokenPurchaseCreateResponseSerializer},
    )
    def post(self, request):
        serializer = TokenPurchaseCreateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        package_id = serializer.validated_data["package_id"]
        package = token_package_by_id(package_id)
        if not package:
            return Response({"detail": "Unknown token package."}, status=status.HTTP_400_BAD_REQUEST)

        purchase = token_wallet_service.create_token_purchase(
            request.user,
            package_id=package_id,
            metadata=serializer.validated_data.get("metadata") or {},
        )
        try:
            purchase = _initiate_payfonte_purchase(request, purchase)
        except PayfonteError as exc:
            token_wallet_service.fail_token_purchase(
                purchase,
                provider="payfonte",
                metadata={"payfonte": {"initiation_error": str(exc)}},
            )
            return Response(
                {"detail": "Could not generate payment account. Please try again.", "provider_error": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                "purchase": TokenPurchaseSerializer(purchase).data,
                "package": package,
            },
            status=status.HTTP_201_CREATED,
        )


class TokenPurchaseAdminCompleteView(APIView):
    permission_classes = [IsAdminUser]
    serializer_class = TokenPurchaseAdminCompleteResponseSerializer

    @extend_schema(
        summary="Admin complete token purchase",
        description=(
            "Admin-only endpoint. Marks a pending token purchase as paid and credits paid tokens to the user's "
            "wallet. The operation is idempotent, so retrying a paid purchase does not credit tokens twice."
        ),
        tags=["Tokens"],
        request=TokenPurchaseAdminCompleteRequestSerializer,
        responses={200: TokenPurchaseAdminCompleteResponseSerializer},
    )
    def post(self, request, purchase_id):
        serializer = TokenPurchaseAdminCompleteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        purchase = get_object_or_404(TokenPurchase, id=purchase_id)
        try:
            result = token_wallet_service.complete_token_purchase(
                purchase,
                provider=serializer.validated_data.get("provider") or "",
                provider_reference=serializer.validated_data.get("provider_reference") or "",
                metadata={
                    **(serializer.validated_data.get("metadata") or {}),
                    "admin_user_id": request.user.id,
                    "admin_username": getattr(request.user, "username", ""),
                    "source": "admin_complete",
                },
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "purchase": TokenPurchaseSerializer(result.purchase).data,
                "wallet": result.balance_after,
                "transaction": TokenTransactionSerializer(result.transaction).data if result.transaction else None,
                "idempotent": result.idempotent,
            }
        )


class TokenPurchaseVerifyView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = TokenPurchaseVerifyResponseSerializer

    @extend_schema(
        summary="Verify token purchase payment",
        description=(
            "Authenticated user endpoint. Verifies the purchase with Payfonte using the provider reference. "
            "If Payfonte returns success, paid tokens are credited idempotently."
        ),
        tags=["Tokens"],
        responses={200: TokenPurchaseVerifyResponseSerializer},
    )
    def post(self, request, purchase_id):
        purchase = get_object_or_404(TokenPurchase, id=purchase_id, user=request.user)
        if purchase.provider != "payfonte":
            return Response({"detail": "This purchase has no Payfonte payment to verify."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result = _settle_payfonte_purchase(purchase)
        except (PayfonteError, ValueError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_token_purchase_verify_payload(result))


class PayfonteWebhookView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(
        summary="Payfonte payment webhook",
        description=(
            "Webhook endpoint for Payfonte collection events. The payload is never trusted by itself; "
            "the transaction is verified with Payfonte before tokens are credited."
        ),
        tags=["Tokens"],
        request=dict,
        responses={200: dict},
    )
    def post(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        references = _payfonte_webhook_references(payload, data)
        if not references:
            return Response({"status": "ignored", "reason": "missing_reference"})

        purchase = (
            TokenPurchase.objects.filter(provider="payfonte", provider_reference__in=references)
            .order_by("-created_at", "-id")
            .first()
        )
        if not purchase:
            metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            purchase_id = metadata.get("purchaseId") or metadata.get("purchase_id")
            if purchase_id:
                purchase = TokenPurchase.objects.filter(provider="payfonte", id=purchase_id).first()
        if not purchase:
            return Response({"status": "ignored", "reason": "purchase_not_found", "references": references})

        verification_references = _unique_payfonte_references(
            _payfonte_transaction_reference(payload, data),
            purchase.provider_reference,
            *references,
        )
        last_payfonte_error = None
        try:
            result = None
            for verification_reference in verification_references:
                try:
                    result = _settle_payfonte_purchase(purchase, verification_reference=verification_reference)
                    break
                except PayfonteError as exc:
                    last_payfonte_error = exc
                    log.warning(
                        "Payfonte webhook verification attempt failed purchase=%s verification_reference=%s error=%s",
                        purchase.id,
                        verification_reference,
                        exc,
                    )
            if result is None:
                raise last_payfonte_error or PayfonteError("verification_failed")
        except PayfonteError as exc:
            log.warning("Payfonte webhook verification failed references=%s error=%s", references, exc)
            return Response({"status": "retry", "detail": "verification_failed"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except ValueError as exc:
            log.warning("Payfonte webhook rejected references=%s error=%s", references, exc)
            return Response({"status": "rejected", "detail": str(exc)}, status=status.HTTP_200_OK)
        except Exception:
            log.exception("Payfonte webhook settlement failed references=%s", references)
            return Response({"status": "retry", "detail": "settlement_failed"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response(
            {
                "status": "processed",
                "purchase_id": result["purchase"].id,
                "payfonte_status": result.get("payfonte_status") or "",
                "idempotent": bool(result.get("idempotent")),
            }
        )


class TokenPurchaseAdminFailView(APIView):
    permission_classes = [IsAdminUser]
    serializer_class = TokenPurchaseAdminFailResponseSerializer

    @extend_schema(
        summary="Admin fail token purchase",
        description="Admin-only endpoint. Marks a pending token purchase as failed without crediting tokens.",
        tags=["Tokens"],
        request=TokenPurchaseAdminFailRequestSerializer,
        responses={200: TokenPurchaseAdminFailResponseSerializer},
    )
    def post(self, request, purchase_id):
        serializer = TokenPurchaseAdminFailRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        purchase = get_object_or_404(TokenPurchase, id=purchase_id)
        try:
            purchase = token_wallet_service.fail_token_purchase(
                purchase,
                provider=serializer.validated_data.get("provider") or "",
                provider_reference=serializer.validated_data.get("provider_reference") or "",
                metadata={
                    **(serializer.validated_data.get("metadata") or {}),
                    "admin_user_id": request.user.id,
                    "admin_username": getattr(request.user, "username", ""),
                    "source": "admin_fail",
                },
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"purchase": TokenPurchaseSerializer(purchase).data})


class TokenAdminAdjustmentView(APIView):
    permission_classes = [IsAdminUser]
    serializer_class = TokenAdminAdjustmentResponseSerializer

    @extend_schema(
        summary="Admin token adjustment",
        description=(
            "Admin-only endpoint. Adds or removes free/paid tokens from a user's wallet and records a ledger "
            "transaction. Intended for support credits, corrections, and future payment-webhook reuse."
        ),
        tags=["Tokens"],
        request=TokenAdminAdjustmentRequestSerializer,
        responses={200: TokenAdminAdjustmentResponseSerializer},
    )
    def post(self, request):
        serializer = TokenAdminAdjustmentRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user_model = get_user_model()
        if data.get("user_id"):
            target_user = get_object_or_404(user_model, id=data["user_id"])
        else:
            target_user = get_object_or_404(user_model, email=data["email"])

        try:
            result = token_wallet_service.adjust_tokens(
                target_user,
                free_tokens_delta=data.get("free_tokens_delta") or 0,
                paid_tokens_delta=data.get("paid_tokens_delta") or 0,
                reference_type="admin_adjustment",
                reference_id=data.get("reference_id") or "",
                metadata={
                    "note": data.get("note", ""),
                    "admin_user_id": request.user.id,
                    "admin_username": getattr(request.user, "username", ""),
                },
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "user": {
                    "id": target_user.id,
                    "username": getattr(target_user, "username", ""),
                    "email": getattr(target_user, "email", ""),
                },
                "wallet": token_wallet_snapshot(result.wallet),
                "transaction": TokenTransactionSerializer(result.transaction).data,
            }
        )
