"""Wallets, ledger, reservations and purchases.

Table names are pinned to their original algo_* values: these rows exist in
production and the refactor moves only Python packages, never data.
"""
from django.conf import settings
from django.db import models


class TokenWallet(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="token_wallet",
    )
    free_tokens = models.PositiveIntegerField(default=0)
    paid_tokens = models.PositiveIntegerField(default=0)
    last_free_refill_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "algo_tokenwallet"
        indexes = [
            models.Index(fields=["user"], name="algo_tokenw_user_id_a075d4_idx"),
            models.Index(fields=["last_free_refill_date"], name="algo_tokenw_last_fr_8b467c_idx"),
        ]

    @property
    def total_tokens(self):
        return int(self.free_tokens or 0) + int(self.paid_tokens or 0)

    def __str__(self):
        return f"Token wallet for {self.user}"


class TokenTransaction(models.Model):
    class TokenBucket(models.TextChoices):
        FREE = "free", "Free"
        PAID = "paid", "Paid"
        MIXED = "mixed", "Mixed"

    class Reason(models.TextChoices):
        SIGNUP_GRANT = "signup_grant", "Signup Grant"
        DAILY_FREE_REFILL = "daily_free_refill", "Daily Free Refill"
        TOKEN_PURCHASE_CREDIT = "token_purchase_credit", "Token Purchase Credit"
        SLIP_REVIEW_RESERVE = "slip_review_reserve", "Slip Review Reserve"
        SLIP_REVIEW_CONSUME = "slip_review_consume", "Slip Review Consume"
        SLIP_REVIEW_RELEASE = "slip_review_release", "Slip Review Release"
        TOKEN_RESERVATION_EXPIRE = "token_reservation_expire", "Token Reservation Expire"
        SMART_RANDOMIZE_CHARGE = "smart_randomize_charge", "Smart Randomize Charge"
        ADMIN_ADJUSTMENT = "admin_adjustment", "Admin Adjustment"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="token_transactions",
    )
    wallet = models.ForeignKey(TokenWallet, on_delete=models.CASCADE, related_name="transactions")
    amount = models.IntegerField(help_text="Positive credits tokens; negative debits tokens.")
    free_tokens_delta = models.IntegerField(default=0)
    paid_tokens_delta = models.IntegerField(default=0)
    token_bucket = models.CharField(max_length=20, choices=TokenBucket.choices, default=TokenBucket.MIXED)
    reason = models.CharField(max_length=40, choices=Reason.choices)
    reference_type = models.CharField(max_length=80, blank=True)
    reference_id = models.CharField(max_length=120, blank=True)
    balance_after = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "algo_tokentransaction"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "created_at"], name="algo_tokent_user_id_f2f456_idx"),
            models.Index(fields=["reference_type", "reference_id"], name="algo_tokent_referen_844759_idx"),
            models.Index(fields=["reason", "created_at"], name="algo_tokent_reason_0318f1_idx"),
        ]

    def __str__(self):
        sign = "+" if self.amount > 0 else ""
        return f"{sign}{self.amount} tokens for {self.user} ({self.reason})"


class TokenReservation(models.Model):
    class Status(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        CONSUMED = "consumed", "Consumed"
        RELEASED = "released", "Released"
        EXPIRED = "expired", "Expired"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="token_reservations",
    )
    wallet = models.ForeignKey(TokenWallet, on_delete=models.CASCADE, related_name="reservations")
    amount = models.PositiveIntegerField()
    free_tokens_reserved = models.PositiveIntegerField(default=0)
    paid_tokens_reserved = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RESERVED)
    reference_type = models.CharField(max_length=80, blank=True)
    reference_id = models.CharField(max_length=120, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "algo_tokenreservation"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "status"], name="algo_tokenr_user_id_4605d0_idx"),
            models.Index(fields=["reference_type", "reference_id"], name="algo_tokenr_referen_32a438_idx"),
            models.Index(fields=["status", "expires_at"], name="algo_tokenr_status__c5782f_idx"),
        ]

    def __str__(self):
        return f"{self.amount} tokens reserved for {self.user} ({self.status})"


class TokenPurchase(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="token_purchases",
    )
    package_id = models.CharField(max_length=120)
    tokens = models.PositiveIntegerField()
    amount = models.PositiveIntegerField(help_text="Major currency unit, e.g. naira.")
    amount_kobo = models.PositiveIntegerField()
    currency = models.CharField(max_length=10, default="NGN")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    provider = models.CharField(max_length=40, blank=True)
    provider_reference = models.CharField(max_length=160, blank=True)
    credited_transaction = models.ForeignKey(
        TokenTransaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="credited_token_purchases",
    )
    metadata = models.JSONField(default=dict, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "algo_tokenpurchase"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "status"], name="algo_tokenp_user_id_80d2d6_idx"),
            models.Index(fields=["package_id", "status"], name="algo_tokenp_package_3e1f55_idx"),
            models.Index(fields=["provider", "provider_reference"], name="algo_tokenp_prov_4f3a41_idx"),
            models.Index(fields=["status", "created_at"], name="algo_tokenp_status_3916fd_idx"),
        ]

    def __str__(self):
        return f"{self.tokens} tokens for {self.user} ({self.status})"
