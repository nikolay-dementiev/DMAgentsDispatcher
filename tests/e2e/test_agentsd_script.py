"""End-to-end test of the installed agentsd console script."""

import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(sys.executable).parent / "agentsd"


class InstalledScriptTests(unittest.TestCase):
    """Runs the script of the project environment, as an installed user would."""

    def test_version_matches_the_project_metadata(self) -> None:
        """The script prints the version declared in pyproject.toml."""
        declared = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        expected = f"agentsd {declared['project']['version']}\n"
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run(  # noqa: S603 - fixed argument list, no shell
                [str(SCRIPT), "--version"],
                cwd=home,
                env={"HOME": home},
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout, expected, msg="version comes from package metadata")
