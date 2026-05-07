import logging
import random
import string

import resend
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


class EmailService:
    @staticmethod
    def generate_6_digit_code() -> str:
        """Generate a random 6-digit numeric code."""
        return "".join(random.choices(string.digits, k=6))

    @staticmethod
    def send_email(to: str, subject: str, html: str):
        """Send email using Resend API."""
        if not settings.RESEND_API_KEY:
            logger.warning(f"RESEND_API_KEY not set. Would send email to {to}: {subject}")
            return {"success": True, "mock": True}

        try:
            resend.api_key = settings.RESEND_API_KEY
            from_email = f"{settings.RESEND_FROM_NAME} <{settings.RESEND_FROM_EMAIL}>"
            response = resend.Emails.send({
                "from": from_email,
                "to": to,
                "subject": subject,
                "html": html,
            })
            return {"success": True, "response": response}
        except Exception as e:
            logger.error(f"Failed to send email to {to}: {e}")
            return {"success": False, "error": str(e)}

    @classmethod
    def send_verification_email(cls, user):
        """Send email verification code to user."""
        code = cls.generate_6_digit_code()
        user.email_verification_code = code
        user.email_verification_sent_at = timezone.now()
        user.save(update_fields=["email_verification_code", "email_verification_sent_at"])

        subject = "Verify your email - Betpreneur"
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

        return cls.send_email(user.email, subject, html)

    @classmethod
    def send_password_reset_email(cls, user):
        """Send password reset email to user."""
        code = cls.generate_6_digit_code()
        user.password_reset_code = code
        user.password_reset_sent_at = timezone.now()
        user.save(update_fields=["password_reset_code", "password_reset_sent_at"])

        reset_link = f"{settings.FRONTEND_URL}/reset-password?token={code}&user={user.id}"

        subject = "Reset your password - Betpreneur"
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

        return cls.send_email(user.email, subject, html)
