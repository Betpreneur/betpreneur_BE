"""Importing a slip from a bookmaker.

Fetches a shared betslip from SportyBet or Betano and turns its legs into
canonical markets. The provider HTTP and page-scraping details stay here for
now; they split into integrations/ once the parsing is separable from the
canonicalisation.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import requests

from betpreneur.modules.catalog.api import descriptor_from_canonical
from betpreneur.modules.catalog.api import resolve as resolve_sportybet_market
from betpreneur.modules.markets.api import Resolution as MarketResolution
from betpreneur.modules.markets.api import describe_market

log = logging.getLogger(__name__)


class BookmakerImportError(ValueError):
    pass


class SportyBetShareImporter:
    SHARE_ENDPOINT = "https://www.sportybet.com/api/ng/orders/share/{code}"

    def extract_code(self, value):
        text = str(value or "").strip()
        if not text:
            return ""
        match = re.search(r"shareCode=([A-Za-z0-9_-]+)", text)
        if match:
            return match.group(1)
        if re.fullmatch(r"[A-Za-z0-9_-]{4,32}", text):
            return text
        return ""

    def fetch_share(self, code):
        try:
            return self._fetch_share_http(code)
        except BookmakerImportError as exc:
            log.warning("SportyBet direct import failed for code=%s; trying browser fallback: %s", code, exc)
            return self._fetch_share_with_browser(code)

    def _fetch_share_http(self, code):
        response = requests.get(
            self.SHARE_ENDPOINT.format(code=code),
            params={"_t": int(time.time() * 1000)},
            headers={
                "Accept": "*/*",
                "Accept-Language": "en",
                "Connection": "keep-alive",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": f"https://www.sportybet.com/ng/?shareCode={code}",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "same-origin",
                "Sec-Fetch-Site": "same-origin",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
                "clientid": "web",
                "operid": "2",
                "platform": "web",
            },
            timeout=20,
        )
        log.info(
            "SportyBet HTTP import response code=%s status=%s content_type=%s bytes=%s",
            code,
            response.status_code,
            response.headers.get("content-type", ""),
            len(response.content or b""),
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            status_code = response.status_code
            body_preview = (response.text or "")[:250].replace("\n", " ")
            if 400 <= status_code < 500:
                raise BookmakerImportError(
                    f"SportyBet rejected the share-code request with HTTP {status_code}. "
                    f"The provider may be blocking server-side import. Response: {body_preview}"
                ) from exc
            raise

        try:
            payload = response.json()
        except ValueError as exc:
            content_type = response.headers.get("content-type", "")
            body_preview = (response.text or "")[:250].replace("\n", " ")
            raise BookmakerImportError(
                "SportyBet did not return JSON for this share code. "
                f"content_type={content_type!r}, status={response.status_code}, response={body_preview!r}"
            ) from exc
        self._log_payload_shape("http", code, payload)
        return payload

    def _fetch_share_with_browser(self, code):
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Install project dependencies and Chromium browser runtime to enable SportyBet browser import."
            ) from exc

        timeout_ms = int(os.environ.get("SPORTYBET_IMPORT_TIMEOUT_MS", "30000") or 30000)
        url = f"https://www.sportybet.com/ng/?shareCode={code}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-sandbox",
                ],
            )
            context = browser.new_context(
                locale="en-US",
                timezone_id="Africa/Lagos",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(1500)
                attempts = page.evaluate(
                    """async (code) => {
                        const paths = [
                            `/orders/share/${code}?_t=${Date.now()}`,
                            `/api/ng/orders/share/${code}?_t=${Date.now()}`,
                            `https://www.sportybet.com/api/ng/orders/share/${code}?_t=${Date.now()}`
                        ];
                        const credentialModes = ["omit", "include"];
                        const results = [];
                        for (const path of paths) {
                            for (const credentials of credentialModes) {
                                try {
                                    const response = await fetch(path, {
                                        method: "GET",
                                        credentials,
                                        headers: {
                                            clientid: "web",
                                            operid: "2",
                                            platform: "web",
                                            Accept: "*/*",
                                            "Accept-Language": "en"
                                        }
                                    });
                                    const text = await response.text();
                                    results.push({
                                        url: path,
                                        credentials,
                                        ok: response.ok,
                                        status: response.status,
                                        contentType: response.headers.get("content-type") || "",
                                        text
                                    });
                                    try {
                                        JSON.parse(text);
                                        return results;
                                    } catch (error) {}
                                } catch (error) {
                                    results.push({
                                        url: path,
                                        credentials,
                                        ok: false,
                                        status: 0,
                                        contentType: "",
                                        text: String(error)
                                    });
                                }
                            }
                        }
                        return results;
                    }""",
                    code,
                )
            except PlaywrightTimeoutError as exc:
                raise RuntimeError("Timed out while loading SportyBet share page.") from exc
            finally:
                context.close()
                browser.close()

        attempts = attempts or []
        log.info("SportyBet browser import attempts code=%s attempts=%s", code, len(attempts))
        for payload in attempts:
            text = (payload or {}).get("text") or ""
            log.info(
                "SportyBet browser attempt code=%s url=%s credentials=%s status=%s content_type=%s bytes=%s",
                code,
                (payload or {}).get("url", ""),
                (payload or {}).get("credentials", ""),
                (payload or {}).get("status"),
                (payload or {}).get("contentType", ""),
                len(text),
            )
            try:
                data = json.loads(text)
            except ValueError:
                continue
            if not (payload or {}).get("ok"):
                raise BookmakerImportError(
                    f"SportyBet browser import returned JSON but failed with HTTP {(payload or {}).get('status')}. "
                    f"Response: {text[:250].replace(chr(10), ' ')}"
                )
            self._log_payload_shape("browser", code, data)
            return data

        last_payload = attempts[-1] if attempts else {}
        last_text = (last_payload or {}).get("text") or ""
        try:
            json.loads(last_text)
        except ValueError as exc:
            raise BookmakerImportError(
                "SportyBet browser import did not return JSON. "
                f"attempts={len(attempts)}, status={(last_payload or {}).get('status')}, "
                f"content_type={(last_payload or {}).get('contentType')!r}, "
                f"url={(last_payload or {}).get('url')!r}, "
                f"response={last_text[:250].replace(chr(10), ' ')!r}"
            ) from exc
        raise BookmakerImportError("SportyBet browser import did not return a usable response.")

    def import_share(self, *, code=None, url=None, payload=None):
        share_code = self.extract_code(code or url)
        if payload is None:
            if not share_code:
                raise ValueError("SportyBet share code or URL is required.")
            payload = self.fetch_share(share_code)
        else:
            share_code = share_code or str((payload.get("data") or {}).get("shareCode") or "").strip()

        data = payload.get("data") or {}
        ticket = data.get("ticket") or {}
        outcomes = ticket.get("outcomes") or data.get("outcomes") or []
        log.info(
            "SportyBet parse start code=%s biz_code=%s available=%s ticket_keys=%s selections=%s outcomes=%s data_keys=%s",
            share_code,
            payload.get("bizCode"),
            payload.get("isAvailable"),
            sorted(ticket.keys()),
            len(ticket.get("selections") or []),
            len(outcomes),
            sorted(data.keys()),
        )
        outcomes_by_event = self._merge_outcomes_by_event(outcomes)
        selections = []
        for item in ticket.get("selections") or []:
            event_id = str(item.get("eventId") or "")
            outcome = outcomes_by_event.get(event_id) or {}
            normalized = self._selection_from_item(item, outcome)
            if normalized:
                selections.append(normalized)
        if not selections:
            for outcome in outcomes:
                normalized = self._selection_from_outcome(outcome)
                if normalized:
                    selections.append(normalized)
        log.info(
            "SportyBet parsed selections code=%s count=%s markets=%s",
            share_code,
            len(selections),
            [
                {
                    "match": item.get("match"),
                    "market": item.get("market"),
                    "odds": item.get("odds"),
                }
                for item in selections[:20]
            ],
        )
        return {
            "provider": "sportybet",
            "share_code": share_code,
            "selection_count": len(selections),
            "selections": selections,
            "raw": payload,
        }

    def _log_payload_shape(self, source, code, payload):
        data = payload.get("data") if isinstance(payload, dict) else {}
        ticket = data.get("ticket") if isinstance(data, dict) else {}
        outcomes = (ticket or {}).get("outcomes") or (data or {}).get("outcomes") or []
        log.info(
            "SportyBet %s payload shape code=%s top_keys=%s data_keys=%s ticket_keys=%s selections=%s outcomes=%s",
            source,
            code,
            sorted(payload.keys()) if isinstance(payload, dict) else [],
            sorted(data.keys()) if isinstance(data, dict) else [],
            sorted(ticket.keys()) if isinstance(ticket, dict) else [],
            len((ticket or {}).get("selections") or []),
            len(outcomes),
        )
        if os.environ.get("SPORTYBET_IMPORT_DEBUG_PAYLOAD", "").lower() in {"1", "true", "yes"}:
            log.info("SportyBet %s raw payload code=%s payload=%s", source, code, json.dumps(payload, default=str)[:8000])

    def _selection_from_item(self, item, outcome):
        home = outcome.get("homeTeamName") or ""
        away = outcome.get("awayTeamName") or ""
        if not home or not away:
            return None
        market = self._market_name(item, outcome)
        if not market:
            return None
        canonical, market_descriptor = self._resolve_market_identity(item, outcome, fallback_text=market)
        tournament = (((outcome.get("sport") or {}).get("category") or {}).get("tournament") or {}).get("name", "")
        return {
            "provider_event_id": item.get("eventId") or outcome.get("eventId") or "",
            "match": f"{home} vs {away}",
            "market": market_descriptor.canonical or market,
            "provider_market_text": market,
            "provider_market_guide": self._market_guide(item, outcome),
            "canonical_market": canonical.to_dict(),
            "market_taxonomy": market_descriptor.to_dict(),
            "home_team": home,
            "away_team": away,
            "competition": tournament,
            "kickoff_ms": outcome.get("estimateStartTime"),
            "odds": self._selection_odds(item, outcome),
            "provider_payload": {"selection": item, "outcome": outcome},
        }

    def _selection_from_outcome(self, outcome):
        home = outcome.get("homeTeamName") or ""
        away = outcome.get("awayTeamName") or ""
        markets = outcome.get("markets") or []
        market = self._market_name({}, {"markets": markets})
        if not home or not away or not market:
            return None
        canonical, market_descriptor = self._resolve_market_identity({}, outcome, fallback_text=market)
        return {
            "provider_event_id": outcome.get("eventId") or "",
            "match": f"{home} vs {away}",
            "market": market_descriptor.canonical or market,
            "provider_market_text": market,
            "provider_market_guide": self._market_guide({}, outcome),
            "canonical_market": canonical.to_dict(),
            "market_taxonomy": market_descriptor.to_dict(),
            "home_team": home,
            "away_team": away,
            "competition": (((outcome.get("sport") or {}).get("category") or {}).get("tournament") or {}).get("name", ""),
            "kickoff_ms": outcome.get("estimateStartTime"),
            "odds": None,
            "provider_payload": {"outcome": outcome},
        }

    @staticmethod
    def _merge_outcomes_by_event(outcomes):
        """
        Merge SportyBet's per-selection outcome entries into one entry per fixture.

        SportyBet emits a separate `outcomes` element for every selection, each carrying
        only that selection's market and only the chosen outcome within it. Indexing them
        into a plain dict keyed on eventId therefore keeps just the last one, and every
        other leg on the same fixture loses its market. Merging markets by
        (id, specifier) — and outcomes within them by id — keeps all legs resolvable,
        which matters for same-match multis and bet builders.
        """
        merged = {}
        market_index = {}
        for item in outcomes:
            event_id = str(item.get("eventId") or "")
            if not event_id:
                continue
            base = merged.get(event_id)
            if base is None:
                base = {key: value for key, value in item.items() if key != "markets"}
                base["markets"] = []
                merged[event_id] = base
                market_index[event_id] = {}
            for market in item.get("markets") or []:
                key = (str(market.get("id") or ""), str(market.get("specifier") or ""))
                existing = market_index[event_id].get(key)
                if existing is None:
                    existing = dict(market)
                    existing["outcomes"] = list(market.get("outcomes") or [])
                    base["markets"].append(existing)
                    market_index[event_id][key] = existing
                    continue
                seen = {str(entry.get("id") or "") for entry in existing["outcomes"]}
                for entry in market.get("outcomes") or []:
                    if str(entry.get("id") or "") not in seen:
                        existing["outcomes"].append(entry)
                        seen.add(str(entry.get("id") or ""))
        return merged

    def _resolve_market_identity(self, item, outcome, *, fallback_text=""):
        """
        Resolve the market from the bookmaker's ids, falling back to text only when the
        market id is unknown to us.

        `Over 2.5` has been observed meaning match goals, home-team goals, bookings and
        shots on target on the same feed, so the display string is not an identity. The
        fallback is flagged via the canonical market's `resolution`, never silently
        presented as if it were a confident identification.
        """
        market_id = str(item.get("marketId") or "")
        specifier = str(item.get("specifier") or "")
        market = next(
            (
                candidate
                for candidate in outcome.get("markets") or []
                if str(candidate.get("id") or "") == market_id
                and (not specifier or str(candidate.get("specifier") or "") == specifier)
            ),
            None,
        ) or (outcome.get("markets") or [{}])[0]
        outcome_id = str(item.get("outcomeId") or "")
        selected = next(
            (
                candidate
                for candidate in market.get("outcomes") or []
                if str(candidate.get("id") or "") == outcome_id
            ),
            None,
        ) or (market.get("outcomes") or [{}])[0]

        canonical = resolve_sportybet_market(
            market_id=market_id or market.get("id"),
            outcome_id=outcome_id or selected.get("id"),
            specifier=specifier or market.get("specifier") or "",
            market_label=market.get("name") or market.get("desc") or "",
            outcome_label=selected.get("desc") or "",
        )
        if canonical.resolution == MarketResolution.MAPPED:
            return canonical, descriptor_from_canonical(canonical, raw=fallback_text)

        log.info(
            "SportyBet unmapped market id=%s specifier=%s outcome=%s label=%r",
            market_id,
            specifier,
            outcome_id,
            market.get("desc") or market.get("name") or "",
        )
        return canonical, describe_market(fallback_text)

    def _market_guide(self, item, outcome):
        market_id = str(item.get("marketId") or "")
        specifier = str(item.get("specifier") or "")
        market = next(
            (
                market
                for market in outcome.get("markets") or []
                if (not market_id or str(market.get("id") or "") == market_id)
                and (not specifier or str(market.get("specifier") or "") == specifier)
            ),
            None,
        ) or (outcome.get("markets") or [{}])[0]
        return str(market.get("marketGuide") or "")

    def _market_name(self, item, outcome):
        market_id = str(item.get("marketId") or "")
        specifier = str(item.get("specifier") or "")
        outcome_id = str(item.get("outcomeId") or "")
        market = next(
            (
                market
                for market in outcome.get("markets") or []
                if str(market.get("id") or "") == market_id
                and (not specifier or str(market.get("specifier") or "") == specifier)
            ),
            None,
        )
        if market:
            outcome_name = self._outcome_name(market, outcome_id)
            if outcome_name:
                return self._canonical_market(market.get("name") or market.get("desc"), outcome_name, market.get("specifier") or specifier)
        return self._canonical_market("", self._fallback_outcome_name(outcome_id), specifier)

    def _outcome_name(self, market, outcome_id):
        for outcome in market.get("outcomes") or []:
            if str(outcome.get("id") or "") == str(outcome_id):
                return str(outcome.get("desc") or outcome.get("name") or "")
        return ""

    def _fallback_outcome_name(self, outcome_id):
        return {
            "1": "Home",
            "2": "Draw",
            "3": "Away",
            "12": "Over",
            "13": "Under",
        }.get(str(outcome_id), "")

    def _canonical_market(self, market_name, outcome_name, specifier):
        market_text = str(market_name or "").strip().lower()
        outcome_text = str(outcome_name or "").strip()
        if outcome_text and any(token in market_text for token in ("goalscorer", "goal scorer", "player to score")):
            return f"{outcome_text} To Score"
        if outcome_text and any(token in market_text for token in ("player shots", "shots on target", "shots on goal", "player shot")):
            if "target" in market_text or "on goal" in market_text:
                # The specifier is machine syntax (`total=7.5`); it must not reach a label.
                return f"{outcome_text} Shots On Target".strip()
            return f"{outcome_text} Shots".strip()
        if outcome_text and any(token in market_text for token in ("player to be booked", "player card", "to be booked")):
            return f"{outcome_text} To Be Booked"
        descriptor = describe_market(
            outcome_name or market_name,
            market_name=market_name,
            outcome_name=outcome_name,
            specifier=specifier,
        )
        canonical = descriptor.canonical if descriptor.recognized else outcome_name.strip()
        if canonical:
            normalized_canonical = str(canonical).strip().lower()
            if "1up" in market_text and not normalized_canonical.endswith("1up"):
                return f"{canonical} 1UP"
            if "2up" in market_text and not normalized_canonical.endswith("2up"):
                return f"{canonical} 2UP"
            if "never down" in market_text:
                return f"{canonical} Never Down"
        return canonical

    def _selection_odds(self, item, outcome):
        market_id = str(item.get("marketId") or "")
        outcome_id = str(item.get("outcomeId") or "")
        for market in outcome.get("markets") or []:
            if str(market.get("id") or "") != market_id:
                continue
            for market_outcome in market.get("outcomes") or []:
                if str(market_outcome.get("id") or "") == outcome_id:
                    return market_outcome.get("odds")
        return item.get("odds")


class BetanoBetslipImporter:
    def extract_code(self, value):
        text = str(value or "").strip()
        if not text:
            return ""
        match = re.search(r"/bookingcode/([A-Za-z0-9_-]+)", text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        if re.fullmatch(r"[A-Za-z0-9_-]{4,64}", text):
            return text
        return ""

    def import_betslip(self, *, code=None, url=None, payload=None):
        booking_code = self.extract_code(code or url)
        if payload is None:
            if not url and booking_code:
                url = f"https://www.betano.ng/bookingcode/{booking_code}"
            if not url:
                raise ValueError("Betano booking URL or code is required.")
            payload = self.fetch_betslip_payload(url)

        legs = self._legs_from_payload(payload)
        selections = []
        for leg in legs:
            normalized = self._selection_from_leg(leg)
            if normalized:
                selections.append(normalized)
        return {
            "provider": "betano",
            "booking_code": booking_code,
            "selection_count": len(selections),
            "selections": selections,
            "raw": payload,
        }

    def fetch_betslip_payload(self, url):
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Install project dependencies and Chromium browser runtime to enable Betano link import."
            ) from exc

        timeout_ms = int(os.environ.get("BETANO_IMPORT_TIMEOUT_MS", "30000") or 30000)
        target_payload = None
        request_payload = None

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-sandbox",
                ],
            )
            context = browser.new_context(
                locale="en-US",
                timezone_id="Africa/Lagos",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
            )
            page = context.new_page()

            def capture_request(request):
                nonlocal request_payload
                if "/api/betslip/v3/getbetslip" not in request.url:
                    return
                try:
                    post_data_json = getattr(request, "post_data_json", None)
                    request_payload = post_data_json() if callable(post_data_json) else post_data_json
                except Exception:
                    request_payload = None

            page.on("request", capture_request)
            try:
                with page.expect_response(
                    lambda response: "/api/betslip/v3/getbetslip" in response.url,
                    timeout=timeout_ms,
                ) as response_info:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                response = response_info.value
                try:
                    target_payload = response.json()
                except Exception as exc:
                    content_type = response.headers.get("content-type", "")
                    try:
                        body_preview = (response.text() or "")[:250].replace("\n", " ")
                    except Exception:
                        body_preview = ""
                    raise BookmakerImportError(
                        "Betano getbetslip response was captured, but it was not valid JSON. "
                        f"content_type={content_type!r}, status={response.status}, response={body_preview!r}"
                    ) from exc
            except PlaywrightTimeoutError as exc:
                raise RuntimeError("Timed out while waiting for Betano getbetslip response.") from exc
            finally:
                context.close()
                browser.close()

        if target_payload:
            return target_payload
        if request_payload:
            return request_payload
        raise RuntimeError("Betano getbetslip payload was not captured.")

    def _legs_from_payload(self, payload):
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and isinstance(data.get("legs"), list):
            return data.get("legs") or []
        betslip = payload.get("betslip") if isinstance(payload, dict) else None
        if isinstance(betslip, dict) and isinstance(betslip.get("legs"), list):
            return betslip.get("legs") or []
        if isinstance(payload.get("legs"), list):
            return payload.get("legs") or []
        return []

    def _selection_from_leg(self, leg):
        if str(leg.get("sportId") or "").upper() not in {"", "FOOT"}:
            return None
        participants = leg.get("participants") or []
        home = (participants[0] or {}).get("name", "") if len(participants) > 0 else ""
        away = (participants[1] or {}).get("name", "") if len(participants) > 1 else ""
        if not home or not away:
            home, away = self._teams_from_event_name(leg.get("eventName"))
        market = self._canonical_market(leg)
        if not home or not away or not market:
            return None
        market_descriptor = describe_market(market)
        return {
            "provider_event_id": str(leg.get("eventId") or ""),
            "match": f"{home} vs {away}",
            "market": market,
            "market_taxonomy": market_descriptor.to_dict(),
            "home_team": home,
            "away_team": away,
            "competition": str(leg.get("league") or leg.get("leagueName") or ""),
            "kickoff_ms": leg.get("eventStartTime"),
            "odds": leg.get("odds"),
            "provider_payload": {"leg": leg},
        }

    def _teams_from_event_name(self, value):
        text = str(value or "")
        if " - " in text:
            home, away = text.split(" - ", 1)
            return home.strip(), away.strip()
        if " vs " in text.lower():
            parts = re.split(r"\s+vs\s+", text, maxsplit=1, flags=re.IGNORECASE)
            return parts[0].strip(), parts[1].strip()
        return "", ""

    def _canonical_market(self, leg):
        description = str(leg.get("description") or "")
        market = str(leg.get("market") or "")
        market_sort = str(leg.get("marketSort") or "")
        event_home, event_away = self._teams_from_event_name(leg.get("eventName"))
        outcome = description
        if market_sort in {"MRES", "MR12"}:
            if description == event_home:
                outcome = "Home"
            elif description == event_away:
                outcome = "Away"
        descriptor = describe_market(
            description,
            market_name=f"{market} {market_sort}",
            outcome_name=outcome,
        )
        return descriptor.canonical if descriptor.recognized else description.strip()
