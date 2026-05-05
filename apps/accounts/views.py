import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from drf_spectacular.utils import extend_schema, OpenApiParameter, extend_schema_view
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .emails import EmailService
from .models import User
from .serializers import (
    ChangePasswordSerializer,
    ForgotPasswordSerializer,
    LoginSerializer,
    ResendVerificationSerializer,
    ResetPasswordSerializer,
    SignupSerializer,
    UserSerializer,
    VerifyEmailSerializer,
)

logger = logging.getLogger(__name__)


@extend_schema_view(
    post=extend_schema(
        summary="Register new user",
        description="""
        Create a new user account.
        
        **Flow:**
        1. Submit signup with username, email, password
        2. Verification code is sent to email
        3. Verify email using `/api/auth/verify-email/`
        4. Login with verified credentials
        """,
        tags=["Authentication"],
        request=SignupSerializer,
        responses={201: UserSerializer},
    )
)
class SignupView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = SignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        # Send verification email
        EmailService.send_verification_email(user)

        return Response(
            {
                "message": "Account created. Please verify your email.",
                "user": UserSerializer(user).data,
            },
            status=status.HTTP_201_CREATED,
        )


@extend_schema_view(
    post=extend_schema(
        summary="Verify email address",
        description="""
        Verify user's email address with 6-digit code.
        
        **Code sent to:** User's email address
        **Expires in:** 10 minutes
        """,
        tags=["Authentication"],
        request=VerifyEmailSerializer,
        responses={
            200: {"description": "Email verified successfully"},
            400: {"description": "Invalid or expired code"},
        },
    )
)
class VerifyEmailView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = VerifyEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = request.data.get("email")
        code = request.data.get("code")

        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            return Response(
                {"error": "Invalid verification request"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if user.is_email_verified:
            return Response(
                {"message": "Email already verified"},
                status=status.HTTP_200_OK,
            )

        if (
            user.email_verification_code == code
            and user.email_verification_sent_at
            and timezone.now() - user.email_verification_sent_at < timedelta(minutes=10)
        ):
            user.is_email_verified = True
            user.email_verification_code = ""
            user.save(update_fields=["is_email_verified", "email_verification_code"])

            return Response(
                {"message": "Email verified successfully"},
                status=status.HTTP_200_OK,
            )

        return Response(
            {"error": "Invalid or expired verification code"},
            status=status.HTTP_400_BAD_REQUEST,
        )


@extend_schema_view(
    post=extend_schema(
        summary="Resend verification code",
        description="""
        Resend email verification code.
        
        **Rate limit:** 1 minute between requests
        """,
        tags=["Authentication"],
        request=ResendVerificationSerializer,
        responses={
            200: {"description": "Verification code sent (if account exists)"},
            429: {"description": "Too many requests - please wait"},
        },
    )
)
class ResendVerificationView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get("email")
        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            return Response(
                {"message": "If an account exists, a verification code has been sent"},
                status=status.HTTP_200_OK,
            )

        if user.is_email_verified:
            return Response(
                {"message": "Email already verified"},
                status=status.HTTP_200_OK,
            )

        if (
            user.email_verification_sent_at
            and timezone.now() - user.email_verification_sent_at < timedelta(minutes=1)
        ):
            return Response(
                {"error": "Please wait before requesting another code"},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        EmailService.send_verification_email(user)

        return Response(
            {"message": "Verification code sent"},
            status=status.HTTP_200_OK,
        )


@extend_schema_view(
    post=extend_schema(
        summary="User login",
        description="""
        Authenticate user and return JWT tokens.
        
        **Login with:** Username OR email address + password
        
        **Returns:**
        - `access`: Short-lived access token (60 min)
        - `refresh`: Long-lived refresh token (7 days)
        - `user`: User profile data
        
        **Note:** Email must be verified before login succeeds.
        """,
        tags=["Authentication"],
        request=LoginSerializer,
        responses={
            200: {"description": "Login successful"},
            401: {"description": "Invalid credentials or email not verified"},
        },
    )
)
class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]

        if not user.is_email_verified:
            return Response(
                {"error": "Email not verified", "requires_verification": True},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        refresh = RefreshToken.for_user(user)

        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": UserSerializer(user).data,
            },
            status=status.HTTP_200_OK,
        )


@extend_schema_view(
    post=extend_schema(
        summary="User logout",
        description="""
        Logout user by blacklisting the refresh token.
        
        **Requires:** Valid access token in header
        """,
        tags=["Authentication"],
        request=None,
        responses={
            200: {"description": "Logged out successfully"},
            400: {"description": "Invalid token"},
        },
    )
)
class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            refresh_token = request.data.get("refresh")
            if refresh_token:
                token = RefreshToken(refresh_token)
                token.blacklist()

            return Response(
                {"message": "Logged out successfully"},
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )


@extend_schema_view(
    post=extend_schema(
        summary="Request password reset",
        description="""
        Request password reset email.
        
        **Flow:**
        1. Submit email address
        2. Reset link/code sent to email
        3. Use `/api/auth/reset-password/` to set new password
        
        **Rate limit:** 1 minute between requests
        """,
        tags=["Authentication"],
        request=ForgotPasswordSerializer,
        responses={
            200: {"description": "Reset email sent (if account exists)"},
            429: {"description": "Too many requests"},
        },
    )
)
class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get("email")
        try:
            user = User.objects.get(email__iexact=email)

            if (
                user.password_reset_sent_at
                and timezone.now() - user.password_reset_sent_at < timedelta(minutes=1)
            ):
                return Response(
                    {"error": "Please wait before requesting another reset email"},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

            EmailService.send_password_reset_email(user)
        except User.DoesNotExist:
            pass

        return Response(
            {"message": "If an account exists, a reset email has been sent"},
            status=status.HTTP_200_OK,
        )


@extend_schema_view(
    post=extend_schema(
        summary="Reset password",
        description="""
        Reset password using token from email.
        
        **Requirements:**
        - Valid token (from forgot password email)
        - New password + confirmation
        
        **Token expires:** 10 minutes
        """,
        tags=["Authentication"],
        request=ResetPasswordSerializer,
        responses={
            200: {"description": "Password reset successful"},
            400: {"description": "Invalid or expired token"},
        },
    )
)
class ResetPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        token = request.data.get("token")
        user_id = request.data.get("user_id")
        new_password = request.data.get("new_password")

        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response(
                {"error": "Invalid reset request"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if (
            user.password_reset_code == token
            and user.password_reset_sent_at
            and timezone.now() - user.password_reset_sent_at < timedelta(minutes=10)
        ):
            user.set_password(new_password)
            user.password_reset_code = ""
            user.save(update_fields=["password", "password_reset_code"])

            return Response(
                {"message": "Password reset successfully"},
                status=status.HTTP_200_OK,
            )

        return Response(
            {"error": "Invalid or expired reset token"},
            status=status.HTTP_400_BAD_REQUEST,
        )


@extend_schema_view(
    get=extend_schema(
        summary="Get current user profile",
        description="Get authenticated user's profile data",
        tags=["Authentication"],
        responses={200: UserSerializer},
    ),
    patch=extend_schema(
        summary="Update user profile",
        description="Update authenticated user's profile (partial update)",
        tags=["Authentication"],
        request=UserSerializer,
        responses={200: UserSerializer},
    ),
)
class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)

    def patch(self, request):
        serializer = UserSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UserSerializer(request.user).data)


@extend_schema_view(
    post=extend_schema(
        summary="Change password",
        description="""
        Change password while logged in.
        
        **Requires:** Current password verification
        """,
        tags=["Authentication"],
        request=ChangePasswordSerializer,
        responses={
            200: {"description": "Password changed successfully"},
            400: {"description": "Invalid old password"},
        },
    )
)
class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user
        old_password = request.data.get("old_password")

        if not user.check_password(old_password):
            return Response(
                {"error": "Incorrect old password"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.set_password(request.data.get("new_password"))
        user.save()

        return Response(
            {"message": "Password changed successfully"},
            status=status.HTTP_200_OK,
        )