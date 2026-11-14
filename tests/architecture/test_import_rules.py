"""Tests of the import graph rules, using deliberately violating source snippets."""

import unittest
from collections.abc import Mapping

from tests.support.arch_check import Violation, check_architecture

PKG = "agents_dispatcher"


def check(files: Mapping[str, str]) -> list[Violation]:
    """Check a synthetic source tree; keys are paths relative to the source root."""
    return check_architecture({f"{PKG}/{path}": source for path, source in files.items()})


def rules_for(files: Mapping[str, str], module: str) -> set[str]:
    """Return the rules violated in one module."""
    return {v.rule for v in check(files) if v.module == f"{PKG}.{module}"}


class LayerMatrixTests(unittest.TestCase):
    """Inner layers must not depend on outer layers, in any import shape."""

    def test_forbidden_import_shapes_are_all_rejected(self) -> None:
        """Every way of writing the same dependency is caught."""
        shapes = {
            "direct": f"from {PKG}.adapters.system import impl\n",
            "plain import": f"import {PKG}.adapters.system.impl\n",
            "aliased import": f"import {PKG}.adapters.system.impl as impl\n",
            "aliased from-import": f"from {PKG}.adapters.system.impl import Impl as Other\n",
            "relative": "from ..adapters.system import impl\n",
            "function local": f"def f() -> None:\n    from {PKG}.adapters.system import impl\n",
            "type checking only": (
                f"from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
                f"    from {PKG}.adapters.system.impl import Impl\n"
            ),
        }
        for name, source in shapes.items():
            for layer in ("domain", "application"):
                with self.subTest(shape=name, layer=layer):
                    files = {f"{layer}/user.py": source, "adapters/system/impl.py": "Impl = 1\n"}
                    self.assertIn("layer-matrix", rules_for(files, f"{layer}.user"))

    def test_re_export_through_the_root_package_is_resolved_to_its_origin(self) -> None:
        """A name re-exported by another module keeps the layer of its origin."""
        files = {
            "__init__.py": f"from {PKG}.adapters.system.impl import Impl\n",
            "domain/user.py": f"from {PKG} import Impl\n",
            "adapters/system/impl.py": "Impl = 1\n",
        }
        self.assertIn("layer-matrix", rules_for(files, "domain.user"))

    def test_every_outer_layer_is_forbidden_for_inner_layers(self) -> None:
        """Interfaces and composition are as forbidden as adapters."""
        for target in ("interfaces.cli.app", "composition.cli"):
            with self.subTest(target=target):
                files = {
                    "domain/user.py": f"import {PKG}.{target}\n",
                    f"{target.replace('.', '/')}.py": "X = 1\n",
                }
                self.assertIn("layer-matrix", rules_for(files, "domain.user"))

    def test_interfaces_may_not_use_adapters(self) -> None:
        """Only composition may reference adapter implementations."""
        files = {
            "interfaces/cli/app.py": f"from {PKG}.adapters.system import impl\n",
            "adapters/system/impl.py": "Impl = 1\n",
        }
        self.assertIn("layer-matrix", rules_for(files, "interfaces.cli.app"))

    def test_one_adapter_package_may_not_use_another(self) -> None:
        """An adapter implementation is referenced only by roots and its own package."""
        files = {
            "adapters/workers/codex.py": f"from {PKG}.adapters.system import impl\n",
            "adapters/system/impl.py": "Impl = 1\n",
        }
        self.assertIn("adapter-reference", rules_for(files, "adapters.workers.codex"))

    def test_allowed_dependencies_are_accepted(self) -> None:
        """The matrix is not simply rejecting everything."""
        files = {
            "domain/model.py": "X = 1\n",
            "application/use.py": f"from {PKG}.domain import model\n",
            "adapters/system/impl.py": (
                f"from {PKG}.application import use\nfrom {PKG}.domain import model\n"
            ),
            "adapters/system/helper.py": "from . import impl\n",
            "interfaces/cli/app.py": f"from {PKG}.application import use\n",
            "composition/cli.py": (
                f"from {PKG}.adapters.system import impl\n"
                f"from {PKG}.interfaces.cli import app\n"
                f"from {PKG}.application import use\n"
            ),
        }
        self.assertEqual(check(files), [], msg="a clean layered tree has no violations")


class CycleTests(unittest.TestCase):
    """Import cycles are rejected."""

    def test_cycle_between_two_inner_modules_is_rejected(self) -> None:
        """Two modules importing each other form a cycle."""
        files = {
            "domain/a.py": f"from {PKG}.domain import b\n",
            "domain/b.py": f"from {PKG}.domain import a\n",
        }
        self.assertIn("cycle", {v.rule for v in check(files)})

    def test_acyclic_graph_has_no_cycle_violation(self) -> None:
        """A chain of imports is not a cycle."""
        files = {
            "domain/a.py": f"from {PKG}.domain import b\n",
            "domain/b.py": "X = 1\n",
        }
        self.assertNotIn("cycle", {v.rule for v in check(files)})
