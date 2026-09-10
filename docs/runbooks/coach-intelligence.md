# Coach Intelligence

Coach Intelligence stores current and historical team-manager assignments separately
from manually researched tactical profiles. Provider synchronization never overwrites
research fields.

## Deploy

Apply the catalog migration:

```bash
python manage.py migrate catalog
```

The task `betpreneur.modules.catalog.tasks.sync_coach_intelligence` is routed to the
StatPal queue and is included in the nightly Team Intelligence workflow.

## Initial import

Import every active competition in the daily All Games registry:

```bash
python manage.py sync_coach_intelligence
```

Useful limited runs:

```bash
python manage.py sync_coach_intelligence --league england-premier-league
python manage.py sync_coach_intelligence --league england-premier-league --max-teams 2
python manage.py sync_coach_intelligence --include-coach-details
```

The same import can be queued from **Catalog > Coach profiles > Sync tracked leagues**.
The importer uses league standings first and falls back to league fixtures for cups or
other competitions without a standings table.

## Research workflow

1. Use **Catalog > Team profiles** to find teams whose current manager is missing.
2. Use **Catalog > Coach profiles** and filter by `Unresearched`.
3. Add a general or team-specific tactical profile from the coach page.
4. Enter 0-100 tactical ratings, a philosophy summary, and evidence URLs.
5. Approve the tactical profile when reviewed.

Confidence is calculated automatically from rating coverage, written tactical detail,
formations, evidence sources, and review status. Approved profiles require a philosophy
summary and at least one evidence source. Manager changes close the previous assignment and create a new current
assignment with detected date precision; administrators can replace those dates with
confirmed appointment/departure dates later.
