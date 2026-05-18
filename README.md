# Betpreneur Backend

Django + DRF backend for Betpreneur GrindAlgo betting intelligence engine.

## The Current Shape

- `apps/accounts` - custom user model plus starter signup/login/me APIs.
- `apps/algo` - algo run records, pick records, and embedded GrindAlgo runners.
- `apps/bankroll` - bankroll snapshots.
- `apps/reports` - generated report metadata.
- `apps/integrations` - external integration configuration... All external API lives here.
- `apps/algo/grindalgo` - embedded GrindAlgo runner powered by API-Football, imported by Django instead of running on Cloud Run/GCP.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

## Starter Endpoints

- `GET /api/health/`
- `POST /api/auth/signup/`
- `POST /api/auth/login/`
- `POST /api/auth/token/refresh/`
- `GET /api/auth/me/`
- `GET /api/algo/public/summary/`
- `GET /api/algo/public/picks/`
- `GET /api/algo/public/top-pick/`
- `GET /api/algo/public/record/`
- `GET /api/algo/runs/`
- `POST /api/algo/runs/`
- `GET /api/algo/runs/{id}/`
- `POST /api/algo/runs/update-results/`
- `POST /api/algo/runs/run-auditor/`
- `GET /api/algo/tasks/{task_id}/`

`POST /api/algo/runs/` accepts an optional payload:

```json
{
  "target_date": "2026-05-04"
}
```

Algo runs are automated with Celery. Celery Beat queues tomorrow's picks at `00:05 WAT`, settles yesterday's results at `06:30 WAT`, and runs the auditor on the 1st of each month at `08:00 WAT` by default. Manual trigger endpoints return a `task_id`; poll `GET /api/algo/tasks/{task_id}/` for completion. The core football data flow uses only API-Football via `APS_KEY`; Google Sheets/Drive export is optional if `KEY_FILE` is configured. Do not commit `grind_key.json` or API keys.

Staff manual intervention lives in Django Admin:

- `AlgoRun` admin actions can queue pick generation, result settlement, or auditor runs for selected dates.
- `Pick` admin groups picks by `match_date`, supports date filtering/search, and allows staff to edit `status`, `score`, and `pnl` for manual corrections.
- Public users should consume only the public picks/record endpoints; manual run endpoints are staff-only.
