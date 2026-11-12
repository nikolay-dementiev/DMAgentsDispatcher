"""Tests of the shared test harness: temporary homes, the injected clock, fake executables."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support.clock import FakeClock
from tests.support.fake_executable import (
    FakeExecutable,
    SourceRef,
    unsourced_fake_executables,
)
from tests.support.home import TemporaryHome

TESTS_ROOT = Path(__file__).resolve().parents[1]


def valid_source() -> SourceRef:
    """Return a well-formed independent source for a fake worker."""
    return SourceRef(
        kind="captured_output",
        cli_name="examplecli",
        cli_version="1.2.3",
        excerpt='{"type": "result", "text": "done"}',
    )


class TemporaryHomeTests(unittest.TestCase):
    """A temporary home and workspace that never touch the real ones."""

    def test_directories_exist_inside_the_context_and_are_removed_after(self) -> None:
        """Both directories are created, writable, and cleaned up."""
        with TemporaryHome() as home:
            self.assertTrue(home.home.is_dir(), msg="home exists")
            self.assertTrue(home.workspace.is_dir(), msg="workspace exists")
            (home.workspace / "file.txt").write_text("x", encoding="utf-8")
        self.assertFalse(home.home.exists(), msg="home is removed")
        self.assertFalse(home.workspace.exists(), msg="workspace is removed")

    def test_the_home_is_not_the_real_home(self) -> None:
        """The temporary home is inside the system temporary directory."""
        with TemporaryHome() as home:
            self.assertNotEqual(home.home, Path.home(), msg="not the real home")
            self.assertTrue(
                home.home.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()),
                msg="home is under the temporary directory",
            )

    def test_environment_is_explicit_and_leaves_the_process_environment_alone(self) -> None:
        """The environment mapping is passed to children, never patched into this process."""
        before = dict(os.environ)
        with TemporaryHome() as home:
            self.assertEqual(home.environment["HOME"], str(home.home), msg="HOME points at it")
            self.assertEqual(dict(os.environ), before, msg="process environment unchanged")


class FakeClockTests(unittest.TestCase):
    """An injected clock that moves only when told to."""

    def test_time_stands_still_until_advanced(self) -> None:
        """Reading the clock does not move it."""
        clock = FakeClock(start=100.0)
        self.assertEqual(clock.now(), clock.now(), msg="two reads agree")
        self.assertEqual(clock.now(), 100.0, msg="starts where told")

    def test_advance_moves_time_forward(self) -> None:
        """Advancing adds the given seconds."""
        clock = FakeClock()
        clock.advance(2.5)
        clock.advance(0.5)
        self.assertEqual(clock.now(), 3.0, msg="advances accumulate")

    def test_advance_rejects_going_backwards(self) -> None:
        """A negative step is a test bug and fails loudly."""
        with self.assertRaises(ValueError):
            FakeClock().advance(-1.0)


class SourceRefTests(unittest.TestCase):
    """A fake worker must name an independent, versioned source."""

    def test_a_complete_source_is_accepted(self) -> None:
        """Kind, CLI name, version and excerpt together are enough."""
        self.assertEqual(valid_source().cli_version, "1.2.3")

    def test_incomplete_sources_are_rejected(self) -> None:
        """A URL alone, a missing or unknown version, or no excerpt each fail."""
        cases = {
            "url only": {"url": "https://example.test/docs", "excerpt": ""},
            "empty excerpt": {"excerpt": "   "},
            "unknown version": {"cli_version": "unknown"},
            "empty version": {"cli_version": ""},
            "not a version": {"cli_version": "latest"},
            "empty cli name": {"cli_name": " "},
            "unsupported kind": {"kind": "url"},
        }
        base = {
            "kind": "captured_output",
            "cli_name": "examplecli",
            "cli_version": "1.2.3",
            "excerpt": "output",
        }
        for name, change in cases.items():
            with self.subTest(case=name), self.assertRaises(ValueError):
                SourceRef(**{**base, **change})


class FakeExecutableTests(unittest.TestCase):
    """Fake worker executables that really run."""

    def test_written_executable_runs_and_prints(self) -> None:
        """The helper writes an executable script that a child process can run."""
        fake = FakeExecutable("worker", "echo hello\n", valid_source())
        with TemporaryHome() as home:
            path = fake.write_to(home.workspace)
            result = subprocess.run(  # noqa: S603 - path written by this test, no shell
                [str(path)], capture_output=True, text=True, check=False, timeout=30
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout, "hello\n", msg="the script ran")

    def test_scanner_flags_a_fake_without_a_source(self) -> None:
        """A constructor call with no source is reported."""
        sources = {"tests/x/test_a.py": 'fake = FakeExecutable("w", "echo hi")\n'}
        self.assertEqual(len(unsourced_fake_executables(sources)), 1, msg="reported once")

    def test_scanner_accepts_a_fake_with_a_source(self) -> None:
        """The source may be passed by keyword or as the third argument."""
        sources = {
            "tests/x/test_a.py": (
                'a = FakeExecutable("w", "echo hi", source=ref)\n'
                'b = FakeExecutable("w", "echo hi", ref)\n'
            ),
        }
        self.assertEqual(unsourced_fake_executables(sources), [], msg="both forms are fine")

    def test_scanner_flags_executable_files_made_outside_the_helper(self) -> None:
        """Making a file executable by hand bypasses the rule."""
        sources = {"tests/x/test_a.py": "path.chmod(0o755)\nos.chmod(path, 0o755)\n"}
        self.assertEqual(len(unsourced_fake_executables(sources)), 2, msg="both chmods reported")

    def test_scanner_ignores_the_helper_itself(self) -> None:
        """The helper is the one place allowed to set the executable bit."""
        sources = {"tests/support/fake_executable.py": "path.chmod(0o755)\n"}
        self.assertEqual(unsourced_fake_executables(sources), [], msg="helper is exempt")

    def test_every_fake_in_the_repository_names_a_source(self) -> None:
        """No test in the repository builds an unsourced fake."""
        sources = {
            "tests/" + path.relative_to(TESTS_ROOT).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(TESTS_ROOT.rglob("*.py"))
        }
        self.assertEqual(unsourced_fake_executables(sources), [], msg="all fakes are sourced")
