"""Provider payload parsing helpers for slip review."""

from datetime import datetime

from django.utils import timezone


def provider_kickoff_ms(selection):
    provider_payload = (selection or {}).get("provider_payload") or {}
    kickoff_ms = provider_payload.get("kickoff_ms")
    if kickoff_ms in (None, ""):
        nested = provider_payload.get("provider_payload") or {}
        kickoff_ms = ((nested.get("outcome") or {}).get("estimateStartTime"))
        if kickoff_ms in (None, ""):
            kickoff_ms = ((nested.get("leg") or {}).get("eventStartTime"))
    return kickoff_ms


def provider_kickoff_datetime(selection):
    kickoff_ms = provider_kickoff_ms(selection)
    try:
        if kickoff_ms in (None, ""):
            return None
        return datetime.fromtimestamp(float(kickoff_ms) / 1000, tz=timezone.get_current_timezone())
    except (TypeError, ValueError, OSError):
        return None


def provider_match_date(selection):
    kickoff_at = provider_kickoff_datetime(selection)
    return kickoff_at.date() if kickoff_at else None


def provider_event_status(selection):
    provider_payload = (selection or {}).get("provider_payload") or {}
    nested = provider_payload.get("provider_payload") or {}
    outcome = nested.get("outcome") or {}
    status = str(outcome.get("status") if outcome.get("status") is not None else "").strip()
    match_status = str(outcome.get("matchStatus") or "").strip().lower()
    return status, match_status


def selection_expiry(selection):
    status, match_status = provider_event_status(selection)
    terminal_statuses = {"ended", "finished", "cancelled", "canceled", "postponed", "abandoned"}
    if status in {"3", "4", "5"} or match_status in terminal_statuses:
        return {
            "expired": True,
            "reason": "provider_event_not_reviewable",
            "message": "This event has already ended or is not available for pre-match review.",
        }
    kickoff_at = provider_kickoff_datetime(selection)
    if kickoff_at and kickoff_at <= timezone.now():
        return {
            "expired": True,
            "reason": "kickoff_already_passed",
            "message": "This event has already started and cannot be reviewed as a pre-match selection.",
        }
    return {"expired": False}


def provider_metadata(selection):
    selection = selection or {}
    provider_payload = selection.get("provider_payload") or {}
    nested = provider_payload.get("provider_payload") or {}
    outcome = nested.get("outcome") or {}
    sport = outcome.get("sport") or {}
    category = sport.get("category") or {}
    tournament = category.get("tournament") or {}
    provider_competition_id = str(tournament.get("id") or "")
    return {
        "provider": selection.get("provider") or provider_payload.get("provider") or "",
        "provider_event_id": provider_payload.get("provider_event_id") or outcome.get("eventId") or "",
        "provider_competition_id": provider_competition_id,
        "competition": provider_payload.get("competition") or tournament.get("name") or "",
        "home_team": provider_payload.get("home_team") or outcome.get("homeTeamName") or "",
        "away_team": provider_payload.get("away_team") or outcome.get("awayTeamName") or "",
    }


def sportybet_statpal_event(selection):
    provider_payload = (selection or {}).get("provider_payload") or {}
    nested = provider_payload.get("provider_payload") or {}
    outcome = nested.get("outcome") or {}
    event = dict(outcome) if isinstance(outcome, dict) else {}
    event.setdefault("eventId", provider_payload.get("provider_event_id") or "")
    event.setdefault("homeTeamName", provider_payload.get("home_team") or "")
    event.setdefault("awayTeamName", provider_payload.get("away_team") or "")
    event.setdefault("estimateStartTime", provider_payload.get("kickoff_ms") or "")
    if provider_payload.get("competition") and not event.get("sport"):
        event["sport"] = {"category": {"tournament": {"name": provider_payload.get("competition")}}}
    return event


__all__ = [
    "provider_event_status",
    "provider_kickoff_datetime",
    "provider_kickoff_ms",
    "provider_match_date",
    "provider_metadata",
    "selection_expiry",
    "sportybet_statpal_event",
]
