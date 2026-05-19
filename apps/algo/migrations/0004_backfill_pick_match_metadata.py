from django.db import migrations


def backfill_pick_match_metadata(apps, schema_editor):
    Pick = apps.get_model("algo", "Pick")
    for pick in Pick.objects.select_related("run").filter(match_date__isnull=True):
        update_fields = []
        if pick.run_id and pick.run.target_date:
            pick.match_date = pick.run.target_date
            update_fields.append("match_date")

        if pick.fixture and " vs " in pick.fixture:
            home_team, away_team = [part.strip() for part in pick.fixture.split(" vs ", 1)]
            if not pick.home_team:
                pick.home_team = home_team
                update_fields.append("home_team")
            if not pick.away_team:
                pick.away_team = away_team
                update_fields.append("away_team")

        if update_fields:
            pick.save(update_fields=update_fields)


class Migration(migrations.Migration):
    dependencies = [
        ("algo", "0003_pick_away_recent_form_pick_away_team_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_pick_match_metadata, migrations.RunPython.noop),
    ]
