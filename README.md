# Betpreneur Backend

Django + DRF backend for Betpreneur GrindAlgo betting intelligence engine.

## Structure

A modular monolith: eleven domain modules over a platform and integration
layer, with the boundaries enforced by `import-linter` rather than convention.

```
betpreneur/
├── platform/       technical; knows no domain
├── integrations/   one adapter per external system (each ships fakes.py)
└── modules/
    ├── markets · identity          vocabulary and accounts
    ├── catalog · billing           fixtures/providers, and money
    ├── scoring                     statistics
    ├── pricing · explanations      judgement (no tables)
    ├── picks                       the daily product
    ├── slips                       the paid product
    └── settlement · analytics      grading and reporting
```

A module calls **down** through the callee's `api.py`; nothing reaches up.
`apps/algo` still exists but holds **only migrations** — see
[ADR 0002](docs/architecture/decisions/0002-state-only-model-moves.md).

- [docs/architecture/modules.md](docs/architecture/modules.md) — what each module owns
- [docs/architecture/dependency-rules.md](docs/architecture/dependency-rules.md) — the rules and how they are enforced
- [docs/architecture/decisions/](docs/architecture/decisions/) — why it is shaped this way
- [docs/runbooks/modular-monolith-cutover.md](docs/runbooks/modular-monolith-cutover.md) — deploying the migration

## Verify

`make verify` runs the gate that CI runs. All six must pass:

| Check | Asserts |
|---|---|
| `verify-schema` | no DDL beyond `scripts/expected_schema_changes.txt` |
| `verify-api` | the public OpenAPI schema is unchanged |
| `verify-migrations` | no model drift from migration state |
| `verify-imports` | 15 module-boundary contracts |
| `verify-lint` | ruff clean |
| `verify-tests` | the full suite |

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

Docker: `make up` (compose lives in `deploy/`, build context is the repo root).

## Starter Endpoints

- `GET /api/health/`
- `POST /api/auth/signup/`
- `POST /api/auth/login/`
- `POST /api/auth/token/refresh/`
- `GET /api/auth/me/`
- `GET /api/algo/public/summary/`
- `GET /api/algo/public/record/`
- `GET /api/algo/picks/`
- `GET /api/algo/picks/download/`
- `POST /api/algo/picks/{id}/back/`
- `GET /api/algo/top-pick/`
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
- Public users can view the 90-day audited record. Authenticated users can view daily picks/top pick, download picks, and mark "I backed this." Manual run endpoints are staff-only.
