# Dependency rules

Enforced by `.importlinter`; run with `make verify-imports`.

| | Rule | Contract |
|---|---|---|
| **R1** | `platform` imports nothing of ours. | `platform-is-bottom` |
| **R2** | Only `<module>.api` / `.contracts` / `.events` cross a boundary. A module reaches its own internals freely. | `private-<module>` ×12 |
| **R3** | Calls go **down**. Peers on the same layer may not import each other. | `layers` |
| **R5** | `domain/` imports no django, DRF, celery or redis. | `domain-purity` |
| **R7** | Integrations import no module and no django. Config is passed in. | `integrations-are-leaves` |

Not machine-checked, but held throughout:

- **R4** — foreign keys point downward, to another module's root aggregate.
  Sideways references store an id and resolve through `api.py`
  (`TokenReservation.reference_type` / `reference_id` is the pattern).
- **R6** — Celery tasks are thin adapters; logic lives in `services/`.
- **R8** — every integration ships `fakes.py`, wired in by `settings/test.py`.

## Two escape hatches, both deliberate

**`config/` is the composition root.** It wires modules together and reaches
`interface/` directly — that is what `urls.py` and `asgi.py` are for. R2 covers
`betpreneur.modules.*` only.

**Cross-layer Celery dispatch goes by name.** `current_app.send_task(...)` /
`signature(...)` with a task-name string, so no import crosses a boundary. Used
where picks queues settlement work and analytics queues a settlement sweep.

## Naming

Every `api.py` export is public — no leading underscores. Names that were
private helpers inside `views.py` and became part of a contract were renamed
when the facades went in.
