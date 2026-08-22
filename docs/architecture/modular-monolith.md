# Algo Modular Monolith

The `apps.algo` package is being carved into module boundaries. Runtime code should import through each module's `api.py` surface.

Layer order:

- L0: `markets`
- L1: `market_data`, `wallet`
- L2: `scoring`
- L3: `advisory`
- L4: `slip_review`, `picks`
- L5: `settlement`

Rules enforced by `apps.algo.tests.test_module_boundaries`:

- A module may import lower layers only.
- Sibling modules may not import each other.
- Cross-module imports must use `apps.algo.<module>.api`.
- Root compatibility shims must remain aliases only.

Temporary bridges remain for pieces still backed by the old `services.py` extraction:

- `apps.algo.market_data.services`
- `apps.algo.picks.services`
- `apps.algo.slip_review.importers`
