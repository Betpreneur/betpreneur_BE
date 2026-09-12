from django.db import migrations, models

RATING_FIELDS = (
    "possession_tendency",
    "build_up_patience",
    "passing_directness",
    "attacking_tempo",
    "attacking_width",
    "crossing_tendency",
    "counterattack_tendency",
    "attacking_risk",
    "pressing_intensity",
    "defensive_block_height",
    "defensive_line_height",
    "defensive_compactness",
    "transition_defence",
    "defensive_aggression",
    "set_piece_emphasis",
    "rotation_tendency",
    "tactical_flexibility",
    "youth_usage",
)

CONFIDENCE_STYLE_FIELDS = (
    "philosophy_summary",
    "attacking_style",
    "build_up_style",
    "defensive_style",
)


def json_item_count(value):
    if isinstance(value, (list, tuple, set)):
        return sum(bool(str(item).strip()) for item in value)
    if isinstance(value, dict):
        return sum(item not in (None, "", [], {}) for item in value.values())
    return int(bool(str(value or "").strip()))


def confidence_from_score(score):
    if score == 0:
        return "unknown"
    if score < 45:
        return "low"
    if score < 80:
        return "medium"
    return "high"


def ai_confidence_score(profile):
    review = profile.ai_confidence_review if isinstance(profile.ai_confidence_review, dict) else {}
    for key in ("ai_confidence_score", "confidence_score"):
        value = review.get(key)
        if value in (None, ""):
            continue
        try:
            return max(0, min(100, round(float(value))))
        except (TypeError, ValueError):
            continue
    return None


def recalculate_confidence(apps, schema_editor):
    CoachTacticalProfile = apps.get_model("catalog", "CoachTacticalProfile")
    for profile in CoachTacticalProfile.objects.all().iterator():
        score = ai_confidence_score(profile)
        if score is not None:
            profile.confidence_score = score
            profile.confidence = confidence_from_score(score)
            profile.save(update_fields=["confidence", "confidence_score"])
            continue

        ratings_completed = sum(getattr(profile, field) is not None for field in RATING_FIELDS)
        rating_score = 65 * ratings_completed / len(RATING_FIELDS)

        style_completed = sum(bool(str(getattr(profile, field) or "").strip()) for field in CONFIDENCE_STYLE_FIELDS)
        style_score = 25 * style_completed / len(CONFIDENCE_STYLE_FIELDS)

        formation_score = 7 if str(profile.preferred_formation or "").strip() else 0
        formation_score += min(3, json_item_count(profile.alternative_formations))
        score = round(
            min(
                100,
                rating_score + style_score + formation_score,
            )
        )
        profile.confidence_score = score
        profile.confidence = confidence_from_score(score)
        profile.save(update_fields=["confidence", "confidence_score"])


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0005_coachtacticalprofile_confidence_score_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="coachtacticalprofile",
            name="ai_confidence_model",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name="coachtacticalprofile",
            name="ai_confidence_review",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="coachtacticalprofile",
            name="ai_confidence_reviewed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(recalculate_confidence, migrations.RunPython.noop),
    ]
