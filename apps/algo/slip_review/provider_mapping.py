"""Provider-to-StatPal mapping helpers for slip review."""

from apps.algo.market_data.api import FixtureSearchService, provider_mapping_service
from apps.algo.slip_review.provider_payloads import sportybet_statpal_event


def try_sportybet_statpal_mapping(selection, *, provider_date, resolver_trace):
    provider_payload = (selection or {}).get("provider_payload") or {}
    provider_event_id = str(provider_payload.get("provider_event_id") or "").strip()
    if str((selection or {}).get("provider") or "").lower() != "sportybet" or not provider_event_id:
        return None

    search_service = FixtureSearchService()
    sync_result = {}

    try:
        result = provider_mapping_service.match_sportybet_to_statpal(sportybet_statpal_event(selection))
    except Exception as exc:
        result = {"matched": False, "reason": "sportybet_statpal_mapping_error", "error": str(exc)}

    if not result.get("matched") and provider_date:
        try:
            sync_result = search_service.sync_statpal_daily(provider_date)
        except Exception as exc:
            sync_result = {"synced": 0, "errors": [str(exc)]}
        try:
            result = provider_mapping_service.match_sportybet_to_statpal(sportybet_statpal_event(selection))
        except Exception as exc:
            result = {"matched": False, "reason": "sportybet_statpal_mapping_error_after_sync", "error": str(exc)}

    resolver_trace.append(
        {
            "strategy": "sportybet_statpal_mapping",
            "synced": sync_result.get("synced", 0),
            "sync_errors": sync_result.get("errors", []),
            "matched": bool(result.get("matched")),
            "reason": result.get("reason", ""),
            "candidate_match_id": (
                ((result.get("candidate") or {}).get("match_id") if isinstance(result.get("candidate"), dict) else "")
            ),
            "candidate_score": (
                ((result.get("candidate") or {}).get("match_score") if isinstance(result.get("candidate"), dict) else None)
            ),
        }
    )
    return result


__all__ = ["try_sportybet_statpal_mapping"]
