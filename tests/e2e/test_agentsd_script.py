"""End-to-end test of the installed agentsd console script."""

import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(sys.executable).parent / "agentsd"


def run_script(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the installed script in a temporary home; only paths that build no services use this."""
    with tempfile.TemporaryDirectory() as home:
        return subprocess.run(  # noqa: S603 - fixed argument list, no shell
            [str(SCRIPT), *arguments],
            cwd=home,
            env={"HOME": home},
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )


class InstalledScriptTests(unittest.TestCase):
    """Runs the script of the project environment, as an installed user would."""

    def test_version_matches_the_project_metadata(self) -> None:
        """The script prints the version declared in pyproject.toml."""
        declared = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        expected = f"agentsd {declared['project']['version']}\n"
        result = run_script("--version")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout, expected, msg="version comes from package metadata")

    def test_the_root_and_two_commands_answer_help_on_stdout(self) -> None:
        """Exit 0, the command's usage on stdout, nothing on stderr; no service is built."""
        for arguments, usage in (
            (("--help",), "usage: agentsd [-h]"),
            (("start", "--help"), "usage: agentsd start"),
            (("quarantine", "resolve", "--help"), "usage: agentsd quarantine resolve"),
        ):
            with self.subTest(arguments=arguments):
                result = run_script(*arguments)
                self.assertEqual(result.returncode, 0, msg=result.stderr)
                self.assertTrue(result.stdout.startswith(usage), msg="the command's usage")
                self.assertEqual(result.stderr, "", msg="nothing on stderr")

    def test_an_unknown_command_exits_two_with_nothing_on_stdout(self) -> None:
        """The usage error goes to stderr."""
        result = run_script("bogus")
        self.assertEqual(result.returncode, 2, msg="invalid input")
        self.assertEqual(result.stdout, "", msg="nothing on stdout")
        self.assertIn("usage: agentsd", result.stderr, msg="the usage is on stderr")


if __name__ == "__main__":
    unittest.main()
