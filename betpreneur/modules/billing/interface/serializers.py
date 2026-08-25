"""Serializers for the token endpoints."""
from rest_framework import serializers

from betpreneur.modules.billing.models import TokenPurchase, TokenTransaction


class TokenTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = TokenTransaction
        fields = (
            "id",
            "amount",
            "free_tokens_delta",
            "paid_tokens_delta",
            "token_bucket",
            "reason",
            "reference_type",
            "reference_id",
            "balance_after",
            "metadata",
            "created_at",
        )


class TokenWalletResponseSerializer(serializers.Serializer):
    wallet = serializers.DictField()


class TokenPackageSerializer(serializers.Serializer):
    id = serializers.CharField()
    tokens = serializers.IntegerField()
    amount = serializers.IntegerField()
    amount_kobo = serializers.IntegerField()
    currency = serializers.CharField()
    label = serializers.CharField()


class TokenPackageListResponseSerializer(serializers.Serializer):
    currency = serializers.CharField()
    packages = TokenPackageSerializer(many=True)


class TokenPurchaseSerializer(serializers.ModelSerializer):
    payment = serializers.SerializerMethodField()

    class Meta:
        model = TokenPurchase
        fields = (
            "id",
            "package_id",
            "tokens",
            "amount",
            "amount_kobo",
            "currency",
            "status",
            "provider",
            "provider_reference",
            "paid_at",
            "failed_at",
            "created_at",
            "updated_at",
            "payment",
        )

    def get_payment(self, obj):
        if obj.provider != "payfonte":
            return None
        from betpreneur.modules.billing.services.payments import payfonte_payment_payload

        return payfonte_payment_payload(obj)


class TokenPurchaseCreateRequestSerializer(serializers.Serializer):
    package_id = serializers.CharField(max_length=120)
    metadata = serializers.DictField(required=False)


class TokenPurchaseCreateResponseSerializer(serializers.Serializer):
    purchase = TokenPurchaseSerializer()
    package = TokenPackageSerializer()


class TokenPurchaseListResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    purchases = TokenPurchaseSerializer(many=True)


class TokenPurchaseAdminCompleteRequestSerializer(serializers.Serializer):
    provider = serializers.CharField(required=False, allow_blank=True, max_length=40)
    provider_reference = serializers.CharField(required=False, allow_blank=True, max_length=160)
    metadata = serializers.DictField(required=False)


class TokenPurchaseAdminCompleteResponseSerializer(serializers.Serializer):
    purchase = TokenPurchaseSerializer()
    wallet = serializers.DictField()
    transaction = TokenTransactionSerializer(required=False, allow_null=True)
    idempotent = serializers.BooleanField()


class TokenPurchaseAdminFailRequestSerializer(serializers.Serializer):
    provider = serializers.CharField(required=False, allow_blank=True, max_length=40)
    provider_reference = serializers.CharField(required=False, allow_blank=True, max_length=160)
    metadata = serializers.DictField(required=False)


class TokenPurchaseAdminFailResponseSerializer(serializers.Serializer):
    purchase = TokenPurchaseSerializer()


class TokenPurchaseVerifyResponseSerializer(serializers.Serializer):
    purchase = TokenPurchaseSerializer()
    wallet = serializers.DictField(required=False)
    transaction = TokenTransactionSerializer(required=False, allow_null=True)
    idempotent = serializers.BooleanField(default=False)
    payfonte_status = serializers.CharField()


class TokenAdminAdjustmentRequestSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(required=False)
    email = serializers.EmailField(required=False)
    free_tokens_delta = serializers.IntegerField(default=0, min_value=-100000, max_value=100000)
    paid_tokens_delta = serializers.IntegerField(default=0, min_value=-100000, max_value=100000)
    note = serializers.CharField(required=False, allow_blank=True, max_length=500)
    reference_id = serializers.CharField(required=False, allow_blank=True, max_length=120)

    def validate(self, attrs):
        if not attrs.get("user_id") and not attrs.get("email"):
            raise serializers.ValidationError("Provide user_id or email.")
        if int(attrs.get("free_tokens_delta") or 0) == 0 and int(attrs.get("paid_tokens_delta") or 0) == 0:
            raise serializers.ValidationError("Provide a non-zero token delta.")
        return attrs


class TokenAdminAdjustmentResponseSerializer(serializers.Serializer):
    user = serializers.DictField()
    wallet = serializers.DictField()
    transaction = TokenTransactionSerializer()
