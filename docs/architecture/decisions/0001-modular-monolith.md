# 1. Modular monolith, not microservices

**Status:** accepted · 2026-08-25

## Context

`apps/algo` held 66,138 of 67,898 lines — 97% of the codebase. `views.py` was
11,045 lines with 298 private helpers; `services.py` was 5,049 with a 109-method
god class; `models.py` held 31 models from six unrelated domains.

Microservices were considered and rejected on cost.

## Decision

Decompose in-process into layered modules with enforced boundaries.

The bottleneck was never deployment isolation — it was that domain logic had
nowhere to live except `views.py`. Network boundaries would have imposed
latency, serialization and ops cost to enforce something a linter enforces for
free. Getting the boundaries right in-process makes later extraction a
deployment decision rather than a rewrite.

## Consequences

- One repo, one deploy, one database.
- Boundaries are enforced by `import-linter` (15 contracts) rather than by
  network topology, so a violation fails CI instead of failing in production.
- Extraction remains available: a module with a single `api.py` and no inbound
  reach-through can become a service without touching its callers' logic.
