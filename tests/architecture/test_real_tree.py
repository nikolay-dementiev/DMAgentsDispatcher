"""Applies the architecture rules to the real source tree."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

from tests.support.arch_check import INNER_STDLIB_ALLOWLIST, ROOT, check_architecture

SRC = Path(__file__).resolve().parents[2] / "src"

# Modules that give access to processes, the network, randomness or temporary files.
# Operating-system basics such as os are loaded by the standard library itself and are
# left to the static rules.
HOST_ACCESS_MODULES = (
    "http",
    "multiprocessing",
    "random",
    "secrets",
    "select",
    "shutil",
    "signal",
    "socket",
    "sqlite3",
    "ssl",
    "subprocess",
    "tempfile",
    "threading",
    "urllib",
    "uuid",
)

# Runs in a fresh isolated interpreter: import the allowlist and the inner layer packages,
# then report which host access modules ended up loaded.
LOADED_HOST_MODULES = """
import importlib, json, sys
sys.path.insert(0, sys.argv[1])
host = set(sys.argv[2].split(","))
for name in sys.argv[3:]:
    importlib.import_module(name)
print(json.dumps(sorted({module.split(".")[0] for module in sys.modules} & host)))
"""


def load_sources() -> dict[str, str]:
    """Read every module of the package, keyed by path relative to the source root."""
    return {
        path.relative_to(SRC).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted((SRC / ROOT).rglob("*.py"))
    }


class RealTreeTests(unittest.TestCase):
    """The shipped code obeys the rules the checker enforces."""

    def test_the_checker_sees_the_whole_package(self) -> None:
        """A checker that reads nothing would pass trivially."""
        sources = load_sources()
        for layer in ("domain", "application", "adapters", "interfaces", "composition"):
            self.assertIn(f"{ROOT}/{layer}/__init__.py", sources, msg=f"{layer} is read")

    def test_source_tree_has_no_architecture_violations(self) -> None:
        """No layer, cycle, purity or dynamic code violation in src."""
        violations = check_architecture(load_sources())
        report = "\n".join(f"{v.rule}: {v.module}:{v.line} {v.detail}" for v in violations)
        self.assertEqual(violations, [], msg=report)

    def test_inner_layers_load_no_host_access_modules(self) -> None:
        """Importing the allowlist and the inner layers loads nothing that reaches the host."""
        modules = [*INNER_STDLIB_ALLOWLIST, f"{ROOT}.domain", f"{ROOT}.application"]
        result = subprocess.run(  # noqa: S603 - fixed argument list, no shell
            [
                sys.executable,
                "-I",
                "-c",
                LOADED_HOST_MODULES,
                str(SRC),
                ",".join(HOST_ACCESS_MODULES),
                *modules,
            ],
            env={},
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(json.loads(result.stdout), [], msg="host access modules were loaded")
