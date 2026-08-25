"""Account emails: issue a code, record it on the user, send it."""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

from betpreneur.integrations.mailer import MailerConfig, ResendMailer, SendResult
from betpreneur.modules.identity.domain.codes import generate_code

logger = logging.getLogger(__name__)


def _mailer() -> ResendMailer:
    """Built per call so override_settings works in tests."""
    return ResendMailer(
        MailerConfig(
            api_key=settings.RESEND_API_KEY,
            from_name=settings.RESEND_FROM_NAME,
            from_email=settings.RESEND_FROM_EMAIL,
        )
    )


def send_email(*, to: str, subject: str, html: str) -> SendResult:
    return _mailer().send(to=to, subject=subject, html=html)


def send_verification_email(user) -> SendResult:
    """Issue a fresh verification code, store it, and email it."""
    code = generate_code()
    user.email_verification_code = code
    user.email_verification_sent_at = timezone.now()
    user.save(update_fields=["email_verification_code", "email_verification_sent_at"])

    html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <h2>Welcome to Betpreneur! 🎉</h2>
            <p>Your verification code is:</p>
            <div style="background: #f5f5f5; padding: 20px; font-size: 32px; letter-spacing: 8px; text-align: center; font-weight: bold; margin: 20px 0;">
                {code}
            </div>
            <p>This code expires in 10 minutes.</p>
            <p>If you didn't create an account, please ignore this email.</p>
        </div>
        """
    return send_email(to=user.email, subject="Verify your email - Betpreneur", html=html)


def send_password_reset_email(user) -> SendResult:
    """Issue a fresh reset code, store it, and email the reset link."""
    code = generate_code()
    user.password_reset_code = code
    user.password_reset_sent_at = timezone.now()
    user.save(update_fields=["password_reset_code", "password_reset_sent_at"])

    reset_link = f"{settings.FRONTEND_URL}/reset-password?token={code}&user={user.id}"
    html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <h2>Password Reset Request 🔐</h2>
            <p>Click the button below to reset your password:</p>
            <a href="{reset_link}" style="display: inline-block; background: #007bff; color: white; padding: 12px 24px; text-decoration: none; border-radius: 4px; margin: 20px 0;">
                Reset Password
            </a>
            <p>Or use this code: <strong>{code}</strong></p>
            <p>This link expires in 10 minutes.</p>
            <p>If you didn't request a password reset, please ignore this email.</p>
        </div>
        """
    return send_email(
        to=user.email, subject="Reset your password - Betpreneur", html=html
    )
