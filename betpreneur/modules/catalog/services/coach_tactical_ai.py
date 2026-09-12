from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import requests
from django.conf import settings
from django.utils import timezone

from betpreneur.modules.catalog.models import CoachTacticalProfile

log = logging.getLogger(__name__)


class CoachTacticalAIReviewError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CoachTacticalAIReviewResult:
    review: dict[str, Any]
    model: str


class CoachTacticalAIReviewer:
    """Ask DeepSeek to judge whether a coach profile is tactically coherent."""

    SYSTEM_PROMPT = """
You are a football tactics QA analyst for a prediction engine.
Review the coach tactical profile and decide how believable and usable it is for prediction.
You must compare the written philosophy/style fields against the numeric ratings.
Return only valid JSON. No markdown, no commentary outside JSON.

Scoring meaning:
- text_rating_agreement: whether the words support the numeric ratings.
- tactical_coherence: whether the ratings make football sense together.
- evidence_clarity: whether the written profile explains the tactical identity clearly.
- prediction_usefulness: whether the profile gives usable signals for goals, BTTS, corners, cards, shots, and result analysis.
- ai_confidence_score: weighted final score from the four components.

Do not reward source URLs or admin approval status. Judge only tactical content.
""".strip()

    REQUIRED_SCORE_KEYS = (
        "ai_confidence_score",
        "text_rating_agreement",
        "tactical_coherence",
        "evidence_clarity",
        "prediction_usefulness",
    )

    def review_profile(self, profile: CoachTacticalProfile) -> CoachTacticalAIReviewResult:
        if not getattr(settings, "COACH_TACTICAL_AI_REVIEW_ENABLED", True):
            raise CoachTacticalAIReviewError("Coach tactical AI review is disabled.")
        api_key = str(getattr(settings, "DEEPSEEK_API_KEY", "") or "").strip()
        if not api_key:
            raise CoachTacticalAIReviewError("DEEPSEEK_API_KEY is not configured.")

        model = str(getattr(settings, "DEEPSEEK_MODEL", "") or "deepseek-v4-flash").strip()
        base_url = str(getattr(settings, "DEEPSEEK_BASE_URL", "") or "https://api.deepseek.com/v1").rstrip("/")
        timeout = int(getattr(settings, "COACH_TACTICAL_AI_REVIEW_TIMEOUT", 45) or 45)
        request_body = {
            "model": model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(self._profile_payload(profile), ensure_ascii=True)},
            ],
        }
        try:
            response = requests.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=request_body,
                timeout=timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CoachTacticalAIReviewError(f"DeepSeek coach review request failed: {exc}") from exc

        try:
            raw = response.json()["choices"][0]["message"]["content"]
            parsed = json.loads(raw)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise CoachTacticalAIReviewError("DeepSeek coach review returned invalid JSON.") from exc
        return CoachTacticalAIReviewResult(
            review=self._normalize_review(parsed),
            model=model,
        )

    def save_review(self, profile: CoachTacticalProfile) -> dict[str, Any]:
        result = self.review_profile(profile)
        now = timezone.now()
        profile.apply_ai_confidence_review(result.review, model=result.model, reviewed_at=now)
        profile.save(
            update_fields=[
                "ai_confidence_review",
                "ai_confidence_model",
                "ai_confidence_reviewed_at",
                "confidence",
                "confidence_score",
                "updated_at",
            ]
        )
        if profile.status == CoachTacticalProfile.Status.APPROVED:
            profile.coach.research_confidence = profile.confidence
            profile.coach.reviewed_at = profile.reviewed_at or now
            profile.coach.save(update_fields=["research_confidence", "reviewed_at", "updated_at"])
        return result.review

    @staticmethod
    def _profile_payload(profile: CoachTacticalProfile) -> dict[str, Any]:
        return {
            "coach": profile.coach.canonical_name if profile.coach_id else "",
            "team": profile.team.canonical_name if profile.team_id else "",
            "preferred_formation": profile.preferred_formation,
            "alternative_formations": profile.alternative_formations,
            "philosophy_summary": profile.philosophy_summary,
            "attacking_style": profile.attacking_style,
            "build_up_style": profile.build_up_style,
            "defensive_style": profile.defensive_style,
            "leading_approach": profile.leading_approach,
            "trailing_approach": profile.trailing_approach,
            "attacking_notes": profile.attacking_notes,
            "defensive_notes": profile.defensive_notes,
            "match_management_notes": profile.match_management_notes,
            "set_piece_notes": profile.set_piece_notes,
            "ratings": {field: getattr(profile, field) for field in CoachTacticalProfile.RATING_FIELDS},
            "required_json_shape": {
                "ai_confidence_score": "0-100 integer",
                "text_rating_agreement": "0-100 integer",
                "tactical_coherence": "0-100 integer",
                "evidence_clarity": "0-100 integer",
                "prediction_usefulness": "0-100 integer",
                "warnings": ["short warning strings"],
                "rating_warnings": ["rating mismatch strings"],
                "market_relevance": {
                    "total_goals": "negative|neutral|positive",
                    "btts": "negative|neutral|positive",
                    "corners": "negative|neutral|positive",
                    "cards": "negative|neutral|positive",
                    "shots_on_target": "negative|neutral|positive",
                    "result": "negative|neutral|positive",
                },
                "summary": "one short tactical QA summary",
            },
        }

    def _normalize_review(self, review: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(review or {})
        for key in self.REQUIRED_SCORE_KEYS:
            normalized[key] = self._score(normalized.get(key))
        normalized["ai_confidence_score"] = self._weighted_score(normalized)
        normalized["warnings"] = self._string_list(normalized.get("warnings"))
        normalized["rating_warnings"] = self._string_list(normalized.get("rating_warnings"))
        normalized["suggested_rating_adjustments"] = normalized.get("suggested_rating_adjustments") or {}
        normalized["market_relevance"] = self._market_relevance(normalized.get("market_relevance"))
        normalized["summary"] = str(normalized.get("summary") or "").strip()[:1000]
        normalized["reviewed_at"] = timezone.now().isoformat()
        return normalized

    @staticmethod
    def _weighted_score(review: dict[str, Any]) -> int:
        return round(
            review["text_rating_agreement"] * 0.35
            + review["tactical_coherence"] * 0.25
            + review["evidence_clarity"] * 0.20
            + review["prediction_usefulness"] * 0.20
        )

    @staticmethod
    def _score(value) -> int:
        try:
            return max(0, min(100, round(float(value))))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _string_list(value) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip()[:300] for item in value if str(item).strip()]

    @staticmethod
    def _market_relevance(value) -> dict[str, str]:
        value = value if isinstance(value, dict) else {}
        allowed = {"negative", "neutral", "positive"}
        markets = ("total_goals", "btts", "corners", "cards", "shots_on_target", "result")
        return {
            market: str(value.get(market) or "neutral").lower()
            if str(value.get(market) or "neutral").lower() in allowed
            else "neutral"
            for market in markets
        }


coach_tactical_ai_reviewer = CoachTacticalAIReviewer()
