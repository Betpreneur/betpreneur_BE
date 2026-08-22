from __future__ import annotations

import ast
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]

LAYERS = {
    "markets": 0,
    "market_data": 1,
    "wallet": 1,
    "scoring": 2,
    "advisory": 3,
    "slip_review": 4,
    "picks": 4,
    "settlement": 5,
}

PUBLIC_API_ONLY = {
    "markets",
    "market_data",
    "wallet",
    "scoring",
    "advisory",
    "slip_review",
    "picks",
    "settlement",
}

# Transitional bridges left while the 11k views.py / services.py extraction continues.
LEGACY_BRIDGES = {
    "apps.algo.market_data.services": {"apps.algo.services"},
    "apps.algo.picks.services": {"apps.algo.services"},
    "apps.algo.slip_review.importers": {"apps.algo.services"},
}

# Shared Django model layer remains the persistence boundary for now.
ROOT_SHARED = {
    "apps.algo.models",
}


def _module_name(path: Path) -> str:
    relative = path.relative_to(ROOT.parent.parent).with_suffix("")
    return ".".join(relative.parts)


def _domain(module: str) -> str | None:
    parts = module.split(".")
    if len(parts) < 3 or parts[:2] != ["apps", "algo"]:
        return None
    return parts[2] if parts[2] in LAYERS else None


def _resolve_import(module: str, node: ast.ImportFrom) -> str:
    imported = node.module or ""
    if node.level == 0:
        return imported

    parts = module.split(".")
    # ImportFrom level is relative to the package containing the current module.
    base = parts[: max(0, len(parts) - node.level)]
    if imported:
        base.extend(imported.split("."))
    return ".".join(base)


def _imports(path: Path, module: str) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(_resolve_import(module, node))
    return imports


def _is_public_domain_import(source_domain: str, imported: str) -> bool:
    target = _domain(imported)
    if target is None or target == source_domain:
        return True
    return imported == f"apps.algo.{target}.api" or imported.startswith(f"apps.algo.{target}.api.")


class AlgoModuleBoundaryTests(TestCase):
    def test_domain_imports_only_flow_downward_through_public_apis(self):
        violations = []
        for path in sorted(ROOT.rglob("*.py")):
            if "__pycache__" in path.parts or "/tests/" in str(path):
                continue
            module = _module_name(path)
            source_domain = _domain(module)
            if source_domain is None:
                continue

            for imported in _imports(path, module):
                if not imported.startswith("apps.algo."):
                    continue
                if imported in ROOT_SHARED:
                    continue
                if imported in LEGACY_BRIDGES.get(module, set()):
                    continue

                target_domain = _domain(imported)
                if target_domain is None or target_domain == source_domain:
                    continue

                if LAYERS[target_domain] > LAYERS[source_domain]:
                    violations.append(f"{module} imports higher layer {imported}")
                    continue

                if LAYERS[target_domain] == LAYERS[source_domain]:
                    violations.append(f"{module} imports sibling module {imported}")
                    continue

                if target_domain in PUBLIC_API_ONLY and not _is_public_domain_import(source_domain, imported):
                    violations.append(f"{module} imports {imported}; use apps.algo.{target_domain}.api")

        self.assertEqual([], violations)

    def test_legacy_root_shims_stay_as_aliases_only(self):
        shims = [
            "daily_market_catalog.py",
            "leg_state.py",
            "market_capabilities.py",
            "market_taxonomy.py",
            "payfonte.py",
            "performance.py",
            "provider_mapping.py",
            "recommendation_policy.py",
            "repair.py",
            "slip_review_market_cache.py",
            "slip_review_redis.py",
            "statpal.py",
            "statpal_advisory.py",
            "statpal_daily_build.py",
            "statpal_provider.py",
            "statpal_snapshots.py",
            "ticket_risk.py",
            "tokens.py",
        ]
        for name in shims:
            source = (ROOT / name).read_text()
            with self.subTest(name=name):
                self.assertIn("sys.modules[__name__]", source)
                self.assertIn("import_module(", source)
