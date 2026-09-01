"""Write path for team-level prediction feedback."""
from __future__ import annotations

from dataclasses import replace

from .contracts import TeamMatchFeedbackRecord
from .models import PredictionTeamMatchFeedback


def record_team_match_feedback(
    record: TeamMatchFeedbackRecord | None = None, **kwargs
) -> PredictionTeamMatchFeedback:
    feedback = _normalize_record(record or TeamMatchFeedbackRecord(**kwargs))
    defaults = {
        "provider_match_id": feedback.provider_match_id,
        "fixture_name": feedback.fixture_name,
        "match_date": feedback.match_date,
        "league_key": feedback.league_key,
        "season": feedback.season,
        "team_id": feedback.team_id,
        "opponent_id": feedback.opponent_id,
        "opponent_name": feedback.opponent_name,
        "actual_result": feedback.actual_result,
        "goals_for": feedback.goals_for,
        "goals_against": feedback.goals_against,
        "corners_for": feedback.corners_for,
        "corners_against": feedback.corners_against,
        "cards_for": feedback.cards_for,
        "cards_against": feedback.cards_against,
        "shots_on_target_for": feedback.shots_on_target_for,
        "shots_on_target_against": feedback.shots_on_target_against,
        "referee_name": feedback.referee_name,
        "source": feedback.source,
        "prediction_snapshot": feedback.prediction_snapshot,
        "actual_stats": feedback.actual_stats,
        "metadata": feedback.metadata,
    }
    obj, created = PredictionTeamMatchFeedback.objects.get_or_create(
        fixture_id=feedback.fixture_id,
        team_name=feedback.team_name,
        side=feedback.side,
        defaults=defaults,
    )
    if created:
        return obj

    changed = False
    for field, value in defaults.items():
        if getattr(obj, field) != value:
            setattr(obj, field, value)
            changed = True
    if changed:
        obj.save()
    return obj


def _normalize_record(record: TeamMatchFeedbackRecord) -> TeamMatchFeedbackRecord:
    return replace(
        record,
        fixture_id=str(record.fixture_id or "").strip(),
        provider_match_id=str(record.provider_match_id or "").strip(),
        fixture_name=str(record.fixture_name or "").strip(),
        league_key=str(record.league_key or "").strip(),
        season=str(record.season or "").strip(),
        team_id=str(record.team_id or "").strip(),
        team_name=str(record.team_name or "").strip(),
        opponent_id=str(record.opponent_id or "").strip(),
        opponent_name=str(record.opponent_name or "").strip(),
        side=str(record.side or "").strip().lower(),
        actual_result=str(record.actual_result or "").strip().lower(),
        referee_name=str(record.referee_name or "").strip(),
        source=str(record.source or "").strip(),
    )
