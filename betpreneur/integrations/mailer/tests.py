"""Mailer tests.

Plain unittest, deliberately: an integration takes its config as an argument,
so proving it needs no Django to exercise is the point of the boundary. The
R7 import contract enforces that these stay framework-free.
"""
import unittest
from unittest.mock import patch

from betpreneur.integrations.mailer import FakeMailer, MailerConfig, ResendMailer


class MailerConfigTests(unittest.TestCase):
    def test_disabled_without_an_api_key(self):
        self.assertFalse(MailerConfig().enabled)
        self.assertTrue(MailerConfig(api_key="k").enabled)

    def test_sender_combines_name_and_address(self):
        config = MailerConfig(from_name="Betpreneur", from_email="support@betpreneur.ng")
        self.assertEqual(config.sender, "Betpreneur <support@betpreneur.ng>")


class ResendMailerTests(unittest.TestCase):
    @patch("betpreneur.integrations.mailer.client.resend.Emails.send")
    def test_sends_with_the_configured_sender(self, send):
        send.return_value = {"id": "email-id"}
        mailer = ResendMailer(
            MailerConfig(api_key="k", from_name="Betpreneur", from_email="s@b.ng")
        )

        result = mailer.send(to="u@example.com", subject="Hi", html="<p>x</p>")

        self.assertTrue(result.success)
        self.assertFalse(result.mocked)
        send.assert_called_once_with(
            {"from": "Betpreneur <s@b.ng>", "to": "u@example.com",
             "subject": "Hi", "html": "<p>x</p>"}
        )

    @patch("betpreneur.integrations.mailer.client.resend.Emails.send")
    def test_no_key_means_no_call(self, send):
        result = ResendMailer(MailerConfig()).send(to="u@e.com", subject="s", html="h")

        self.assertTrue(result.success)
        self.assertTrue(result.mocked)
        send.assert_not_called()

    @patch("betpreneur.integrations.mailer.client.resend.Emails.send")
    def test_transport_failure_never_raises(self, send):
        send.side_effect = RuntimeError("resend is down")

        result = ResendMailer(MailerConfig(api_key="k")).send(
            to="u@e.com", subject="s", html="h"
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "resend is down")


class FakeMailerTests(unittest.TestCase):
    def test_records_sends_instead_of_making_them(self):
        mailer = FakeMailer()

        mailer.send(to="a@e.com", subject="one", html="<p>1</p>")
        mailer.send(to="b@e.com", subject="two", html="<p>2</p>")

        self.assertEqual([e.to for e in mailer.sent], ["a@e.com", "b@e.com"])
        self.assertEqual(mailer.last_to("a@e.com").subject, "one")
        self.assertIsNone(mailer.last_to("nobody@e.com"))

    def test_can_be_told_to_fail(self):
        self.assertFalse(FakeMailer(fail=True).send(to="a@e.com", subject="s", html="h").success)
