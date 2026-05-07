from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.accounts.emails import EmailService


class EmailServiceTests(SimpleTestCase):
    @override_settings(
        RESEND_API_KEY="test-api-key",
        RESEND_FROM_NAME="Betpreneur",
        RESEND_FROM_EMAIL="support@betpreneur.ng",
    )
    @patch("apps.accounts.emails.resend.Emails.send")
    def test_send_email_uses_configured_resend_sender(self, send_mock):
        send_mock.return_value = {"id": "email-id"}

        result = EmailService.send_email(
            to="user@example.com",
            subject="Verify your email",
            html="<p>Hello</p>",
        )

        self.assertTrue(result["success"])
        send_mock.assert_called_once_with(
            {
                "from": "Betpreneur <support@betpreneur.ng>",
                "to": "user@example.com",
                "subject": "Verify your email",
                "html": "<p>Hello</p>",
            }
        )

    @override_settings(RESEND_API_KEY="")
    @patch("apps.accounts.emails.resend.Emails.send")
    def test_send_email_is_mocked_without_resend_api_key(self, send_mock):
        result = EmailService.send_email(
            to="user@example.com",
            subject="Verify your email",
            html="<p>Hello</p>",
        )

        self.assertEqual(result, {"success": True, "mock": True})
        send_mock.assert_not_called()
