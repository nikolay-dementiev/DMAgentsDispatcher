"""Tests that the lint and type gates reject findings instead of reporting them."""

import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BIN_DIR = Path(sys.executable).parent

CLEAN_MODULE = '''"""A clean module."""


def double(value: int) -> int:
    """Return twice the value."""
    return value * 2
'''

UNUSED_IMPORT_MODULE = '''"""A module with an unused import."""

import os
'''

UNTYPED_FUNCTION_MODULE = '''"""A module with an untyped function."""


def identity(value):
    """Return the value."""
    return value
'''


class GateProjectTestCase(unittest.TestCase):
    """Runs the real tool configuration against a throwaway project."""

    def make_project(self, module_source: str) -> Path:
        """Create a project holding the repository tool tables and one module."""
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        project = Path(temp_dir.name)
        shutil.copy(REPO_ROOT / "pyproject.toml", project / "pyproject.toml")
        package = project / "src" / "sample"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(module_source, encoding="utf-8")
        tests = project / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text('"""Tests."""\n', encoding="utf-8")
        return project

    def run_tool(self, project: Path, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Run a tool in the project with a minimal environment and a time limit."""
        return subprocess.run(  # noqa: S603 - fixed argument list, no shell
            command,
            cwd=project,
            env={"HOME": str(project)},
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )


class LintGateTests(GateProjectTestCase):
    """The lint gate must fail on findings."""

    ruff = (str(BIN_DIR / "ruff"), "check", "--no-cache", ".")

    def test_ruff_check_rejects_an_unused_import(self) -> None:
        """An unused import makes the lint command exit non-zero."""
        result = self.run_tool(self.make_project(UNUSED_IMPORT_MODULE), self.ruff)
        self.assertNotEqual(result.returncode, 0, msg=result.stdout)
        self.assertIn("F401", result.stdout, msg="the unused import rule must be what fires")

    def test_ruff_check_accepts_a_clean_module(self) -> None:
        """A clean module passes, so the gate is not failing for other reasons."""
        result = self.run_tool(self.make_project(CLEAN_MODULE), self.ruff)
        self.assertEqual(result.returncode, 0, msg=result.stdout)


class TypeGateTests(GateProjectTestCase):
    """The type gate must fail on findings."""

    mypy = (sys.executable, "-m", "mypy", "--cache-dir=/dev/null")

    def test_mypy_rejects_an_untyped_function(self) -> None:
        """Strict mode makes an untyped function fail the type command."""
        result = self.run_tool(self.make_project(UNTYPED_FUNCTION_MODULE), self.mypy)
        self.assertNotEqual(result.returncode, 0, msg=result.stdout)
        self.assertIn("no-untyped-def", result.stdout, msg="strict mode must be what fires")

    def test_mypy_accepts_a_clean_module(self) -> None:
        """A clean module passes, so the gate is not failing for other reasons."""
        result = self.run_tool(self.make_project(CLEAN_MODULE), self.mypy)
        self.assertEqual(result.returncode, 0, msg=result.stdout)


class ConfigurationTests(unittest.TestCase):
    """The repository configuration must not be weakened."""

    def setUp(self) -> None:
        """Load the repository pyproject."""
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.text = text
        self.config = tomllib.loads(text)

    def test_mypy_runs_in_strict_mode(self) -> None:
        """Strict mode stays on."""
        strict = self.config["tool"]["mypy"]["strict"]
        self.assertIs(strict, True)  # noqa: FBT003 - asserting a value, not passing a flag

    def test_ruff_selects_every_rule(self) -> None:
        """All rules stay selected; ignores are explicit entries with reasons."""
        self.assertEqual(self.config["tool"]["ruff"]["lint"]["select"], ["ALL"])

    def test_no_warning_filters_are_configured(self) -> None:
        """No warning filter can hide a warning from the test run."""
        self.assertNotIn("filterwarnings", self.text)
