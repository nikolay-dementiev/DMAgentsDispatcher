"""Tests of the inner layer purity rules, using deliberately violating source snippets."""

import unittest

from tests.support.arch_check import INNER_STDLIB_ALLOWLIST, Violation, check_architecture

PKG = "agents_dispatcher"
INNER_LAYERS = ("domain", "application")
OUTER_LAYERS = ("adapters", "interfaces", "composition")


def check_module(layer: str, source: str) -> list[Violation]:
    """Check one module placed in the given layer."""
    return check_architecture({f"{PKG}/{layer}/user.py": source})


def rules(layer: str, source: str) -> set[str]:
    """Return the names of the rules a module breaks."""
    return {violation.rule for violation in check_module(layer, source)}


class ForbiddenModuleTests(unittest.TestCase):
    """Inner layers import only reviewed pure standard-library modules."""

    def test_modules_with_side_effects_are_rejected_in_every_import_shape(self) -> None:
        """The listed modules, and any unlisted one, are refused."""
        modules = ["os", "subprocess", "time", "random", "sqlite3", "sys", "datetime", "pathlib"]
        modules += ["socket", "shutil", "tempfile", "secrets", "uuid", "io", "threading", "json"]
        modules += ["importlib", "requests"]
        for module in modules:
            shapes = {
                "plain": f"import {module}\n",
                "aliased": f"import {module} as alias\n",
                "from-import": f"from {module} import something\n",
                "function local": f"def f() -> None:\n    import {module}\n",
                "type checking": f"if TYPE_CHECKING:\n    import {module}\n",
            }
            for shape, source in shapes.items():
                for layer in INNER_LAYERS:
                    with self.subTest(module=module, shape=shape, layer=layer):
                        self.assertIn("forbidden-module", rules(layer, source))

    def test_dynamic_import_helpers_are_rejected(self) -> None:
        """Importing the import machinery is refused, whatever it is renamed to."""
        sources = [
            "from importlib import import_module as load\n",
            "import importlib.util\n",
            "from importlib.metadata import version\n",
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assertIn("forbidden-module", rules("domain", source))

    def test_reviewed_modules_and_package_imports_are_accepted(self) -> None:
        """The allowlist and imports inside the package pass."""
        source = (
            "from __future__ import annotations\n"
            "from collections.abc import Sequence\n"
            "from dataclasses import dataclass\n"
            "from typing import Protocol\n"
            "from . import sibling\n"
        )
        self.assertEqual(check_module("domain", source), [], msg="allowed imports are clean")

    def test_outer_layers_may_use_the_host_system(self) -> None:
        """Purity is an inner layer rule only."""
        for layer in OUTER_LAYERS:
            with self.subTest(layer=layer):
                self.assertEqual(check_module(layer, "import os\nimport subprocess\n"), [])

    def test_every_allowlist_entry_has_a_reason(self) -> None:
        """The allowlist is reviewed: no entry without a stated reason."""
        for name, reason in INNER_STDLIB_ALLOWLIST.items():
            with self.subTest(module=name):
                self.assertTrue(reason.strip(), msg=f"{name} needs a reason")


class DynamicCodeTests(unittest.TestCase):
    """Inner layers do not execute or load code dynamically, or reach the host by builtins."""

    names = (
        "eval",
        "exec",
        "compile",
        "__import__",
        "open",
        "input",
        "getattr",
        "setattr",
        "globals",
        "vars",
        "__builtins__",
    )

    def test_forbidden_builtins_are_rejected_when_called_or_referenced(self) -> None:
        """A call, a bare reference and an alias are all refused."""
        for name in self.names:
            shapes = {
                "call": f"result = {name}('x')\n",
                "reference": f"handler = {name}\n",
                "inside function": f"def f() -> None:\n    {name}('x')\n",
            }
            for shape, source in shapes.items():
                for layer in INNER_LAYERS:
                    with self.subTest(name=name, shape=shape, layer=layer):
                        self.assertIn("dynamic-code", rules(layer, source))

    def test_outer_layers_may_use_builtins_freely(self) -> None:
        """Only inner layers are restricted."""
        for layer in OUTER_LAYERS:
            with self.subTest(layer=layer):
                self.assertEqual(check_module(layer, "data = open('x')\n"), [])

    def test_ordinary_names_are_not_flagged(self) -> None:
        """A local variable that only contains a forbidden word is fine."""
        source = "evaluate = 1\nopen_count = 2\nreopen = evaluate + open_count\n"
        self.assertEqual(check_module("domain", source), [], msg="similar names are clean")
