# Betpreneur Backend

Django + DRF backend for Betpreneur GrindAlgo betting intelligence engine.

## The Current Shape

- `apps/accounts` - custom user model plus starter signup/login/me APIs.
- `apps/algo` - algo run records, pick records, and a service boundary around the legacy runner.
- `apps/bankroll` - bankroll snapshots.
- `apps/reports` - generated report metadata.
- `apps/integrations` - external integration configuration... All external API lives here.
- `algo_runner.py` - legacy single-file runner, still supported while the logic is migrated into services.

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
- `GET /api/algo/runs/`
- `POST /api/algo/runs/`
- `GET /api/algo/runs/{id}/`

`POST /api/algo/runs/` accepts an optional payload:

```json
{
  "target_date": "2026-05-04"
}
```

For now, all the  endpoints here that I have done still runs the  `run_daily_algo()` synchronously through `apps.algo.services.AlgoRunnerService`. The next step I want to do is to migrate the legacy file into smaller services, then move long-running execution into Celery or a scheduler.
