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

Import every active league in the Team Intelligence registry:

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

## Research workflow

1. Use **Catalog > Team profiles** to find teams whose current manager is missing.
2. Use **Catalog > Coach profiles** and filter by `Unresearched`.
3. Add a general or team-specific tactical profile from the coach page.
4. Enter 0-100 tactical ratings, a confidence level, a philosophy summary, and evidence URLs.
5. Approve the tactical profile when reviewed.

Approved profiles require a confidence level, a philosophy summary, and at least one
evidence source. Manager changes close the previous assignment and create a new current
assignment with detected date precision; administrators can replace those dates with
confirmed appointment/departure dates later.
