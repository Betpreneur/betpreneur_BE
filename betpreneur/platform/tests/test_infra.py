import datetime as dt
from decimal import Decimal

from django.core.cache import cache
from django.http import HttpResponseNotModified
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIRequestFactory

from betpreneur.platform.cache.http import cached_response, payload_etag
from betpreneur.platform.cache.keys import key
from betpreneur.platform.db.json import json_safe
from betpreneur.platform.tasks.idempotency import AlreadyRunning, once, run_once


class JsonSafeTests(SimpleTestCase):
    """These pin the *existing* wire format, which is already in production
    JSONFields. Decimals become strings and datetimes are space-separated —
    surprising, but changing it would alter API payloads."""

    def test_decimal_becomes_a_string_not_a_float(self):
        self.assertEqual(json_safe({"amount": Decimal("1.50")}), {"amount": "1.50"})

    def test_datetime_is_space_separated_not_iso_t(self):
        stamp = dt.datetime(2026, 8, 11, 0, 49, tzinfo=dt.UTC)
        self.assertEqual(json_safe({"at": stamp}), {"at": "2026-08-11 00:49:00+00:00"})

    def test_date_stringifies(self):
        self.assertEqual(json_safe({"d": dt.date(2026, 8, 25)}), {"d": "2026-08-25"})

    def test_recurses_through_containers(self):
        out = json_safe({"xs": [Decimal("1"), {"y": dt.date(2026, 1, 2)}]})
        self.assertEqual(out, {"xs": ["1", {"y": "2026-01-02"}]})

    def test_passes_through_primitives(self):
        for v in (None, "a", 1, 1.5, True):
            self.assertEqual(json_safe(v), v)

    def test_tuples_become_lists(self):
        self.assertEqual(json_safe({"t": (1, 2)}), {"t": [1, 2]})

    def test_unknown_types_become_strings(self):
        class Opaque:
            def __str__(self):
                return "opaque!"

        self.assertEqual(json_safe({"o": Opaque()}), {"o": "opaque!"})


class CacheKeyTests(SimpleTestCase):
    def test_namespaces_by_module(self):
        self.assertEqual(key("slips", "review", 12), "betpreneur:slips:review:12")
        self.assertEqual(key("slips"), "betpreneur:slips")


class CachedResponseTests(SimpleTestCase):
    """These pin the real behaviour lifted from views.py. The hash and the 304
    response type are load-bearing: changing either invalidates every client's
    cache or drops the conditional-request handling."""

    def setUp(self):
        self.factory = APIRequestFactory()

    def test_the_etag_is_sha256(self):
        import hashlib
        import json
        payload = {"a": 1}
        raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
        self.assertEqual(payload_etag(payload), f'"{hashlib.sha256(raw).hexdigest()}"')

    def test_etag_is_stable_regardless_of_key_order(self):
        self.assertEqual(payload_etag({"a": 1, "b": 2}), payload_etag({"b": 2, "a": 1}))

    def test_a_current_client_gets_a_real_304(self):
        payload = {"ok": True}
        request = self.factory.get("/", HTTP_IF_NONE_MATCH=payload_etag(payload))

        response = cached_response(payload, request=request)

        self.assertIsInstance(response, HttpResponseNotModified)
        self.assertEqual(response["ETag"], payload_etag(payload))

    def test_private_responses_vary_on_credentials(self):
        """Without Vary a shared cache can serve one user's payload to another."""
        response = cached_response({"ok": True}, private=True)
        self.assertEqual(response["Vary"], "Authorization, Cookie")

    def test_public_responses_do_not_vary(self):
        response = cached_response({"ok": True}, private=False)
        self.assertNotIn("Vary", response)
        self.assertIn("public,", response["Cache-Control"])

    def test_cache_control_carries_the_stale_directives(self):
        response = cached_response({"ok": True}, seconds=60)
        self.assertEqual(
            response["Cache-Control"],
            "private, max-age=60, stale-while-revalidate=60, stale-if-error=86400",
        )

    def test_ttl_falls_back_to_the_setting(self):
        with override_settings(ALGO_READ_CACHE_SECONDS=42):
            self.assertIn("max-age=42", cached_response({})["Cache-Control"])


class IdempotencyTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_second_entry_is_refused_while_held(self):
        with run_once("settlement", "2026-08-25"):
            with self.assertRaises(AlreadyRunning):
                with run_once("settlement", "2026-08-25"):
                    pass

    def test_key_is_released_after_the_block(self):
        with run_once("settlement", "2026-08-25"):
            pass
        with run_once("settlement", "2026-08-25"):
            pass  # must not raise

    def test_key_is_released_even_when_the_block_raises(self):
        with self.assertRaises(ValueError):
            with run_once("settlement", "2026-08-25"):
                raise ValueError("boom")
        with run_once("settlement", "2026-08-25"):
            pass  # a failed run must be retryable immediately

    def test_different_parts_do_not_collide(self):
        with run_once("settlement", "2026-08-25"):
            with run_once("settlement", "2026-08-26"):
                pass

    def test_functional_form_returns_none_when_held(self):
        with run_once("settle", 1):
            self.assertIsNone(once("settle", 1)(lambda: "ran"))
        self.assertEqual(once("settle", 1)(lambda: "ran"), "ran")
