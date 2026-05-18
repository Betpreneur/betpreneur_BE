from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed as AuthError

User = get_user_model()


class SignupSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])

    class Meta:
        model = User
        fields = ("id", "username", "email", "password")
        read_only_fields = ("id",)

    def validate_username(self, value):
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("Username already exists")
        return value

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("Email already exists")
        return value

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user


class VerifyEmailSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(max_length=6)


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField(required=False, allow_blank=True)


class ResendVerificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("email",)


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        username = attrs.get("username")
        password = attrs.get("password")

        # Authenticate with username or email
        user = authenticate(
            request=self.context.get("request"),
            username=username,
            password=password,
        )

        if not user:
            # Try to find user by email
            try:
                user_obj = User.objects.get(email__iexact=username)
                if user_obj.check_password(password):
                    user = user_obj
            except User.DoesNotExist:
                pass

        if not user:
            raise AuthError("Invalid credentials")

        if not user.is_active:
            raise AuthError("User account is disabled")

        attrs["user"] = user
        return attrs


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ResetPasswordSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=6)
    user_id = serializers.UUIDField()
    new_password = serializers.CharField(write_only=True, validators=[validate_password])
    confirm_password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        if attrs.get("new_password") != attrs.get("confirm_password"):
            raise serializers.ValidationError("Passwords do not match")
        return attrs


class UserSerializer(serializers.ModelSerializer):
    full_whatsapp = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id", "username", "email", "is_email_verified", "whatsapp_country_code",
            "whatsapp_number", "full_whatsapp", "date_joined"
        )
        read_only_fields = ("id", "date_joined")

    def get_full_whatsapp(self, obj):
        if obj.whatsapp_country_code and obj.whatsapp_number:
            return f"{obj.whatsapp_country_code}{obj.whatsapp_number}"
        return None


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, validators=[validate_password])
    confirm_password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        if attrs.get("new_password") != attrs.get("confirm_password"):
            raise serializers.ValidationError("Passwords do not match")
        return attrs
