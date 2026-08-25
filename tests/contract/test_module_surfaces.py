"""Every module's public surface, pinned.

These fail when a name silently disappears from an api.py — which is how a
"harmless" internal rename becomes a broken caller in another module. They
also assert the structural rules that import-linter cannot express.
"""
import importlib
import pkgutil
import unittest

MODULES = [
    "markets", "identity", "catalog", "billing", "scoring", "pricing",
    "explanations", "picks", "slips", "settlement", "analytics",
]

# Modules that own no tables are plain packages, not Django apps.
TABLELESS = {"markets", "pricing", "explanations"}


class ApiSurfaceTests(unittest.TestCase):
    def test_every_module_has_an_api(self):
        for name in MODULES:
            with self.subTest(module=name):
                importlib.import_module(f"betpreneur.modules.{name}.api")

    def test_every_export_resolves(self):
        """__all__ must not name something the module does not actually expose."""
        for name in MODULES:
            api = importlib.import_module(f"betpreneur.modules.{name}.api")
            missing = [n for n in getattr(api, "__all__", []) if not hasattr(api, n)]
            with self.subTest(module=name):
                self.assertEqual(missing, [], f"{name}.api.__all__ names missing attrs")

    def test_every_api_declares_all(self):
        for name in MODULES:
            api = importlib.import_module(f"betpreneur.modules.{name}.api")
            with self.subTest(module=name):
                self.assertTrue(
                    getattr(api, "__all__", None),
                    f"{name}.api must declare __all__ — it is the contract",
                )

    def test_tableless_modules_are_not_django_apps(self):
        """markets/pricing/explanations own no tables, so they must stay plain
        packages: no apps.py, no migrations, nothing in INSTALLED_APPS."""
        from django.conf import settings

        for name in TABLELESS:
            pkg = importlib.import_module(f"betpreneur.modules.{name}")
            children = {m.name for m in pkgutil.iter_modules(pkg.__path__)}
            with self.subTest(module=name):
                self.assertNotIn("apps", children, f"{name} should own no Django AppConfig")
                self.assertNotIn("migrations", children)
                self.assertNotIn(f"betpreneur.modules.{name}", settings.INSTALLED_APPS)

    def test_domain_packages_import_no_framework(self):
        """R5, asserted at runtime as well as statically: importing a domain
        package must not drag in Django."""
        import sys

        for name in MODULES:
            try:
                importlib.import_module(f"betpreneur.modules.{name}.domain")
            except ModuleNotFoundError:
                continue
            mod = sys.modules[f"betpreneur.modules.{name}.domain"]
            with self.subTest(module=name):
                self.assertFalse(
                    any(k.startswith("django") for k in vars(mod)),
                    f"{name}.domain must not bind django names",
                )
