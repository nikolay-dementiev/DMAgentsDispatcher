"""Tests of the confined login inspector: every observation runs through the confinement."""

import tempfile
import unittest
import uuid
from collections.abc import Callable
from pathlib import Path

from agents_dispatcher.adapters.system.login_inspector import ConfinedLoginInspector

from agents_dispatcher.application.errors import LaunchError
from agents_dispatcher.application.ports import (
    EgressEndpoint,
    ExecutionOutcome,
    PreparedWorkspace,
    ProcessIdentity,
    WorkerCommand,
)
from agents_dispatcher.domain.login import LoginState
from agents_dispatcher.domain.routing import TaskMode
from agents_dispatcher.domain.run import LimitKind, Limits, RunId
from tests.support.clock import FakeClock
from tests.support.run_spies import Journal, SpyConfinement

LIMITS = Limits(hard_s=20.0, memory_bytes=1_073_741_824)


class FixedProbe:
    """A probe with a canned argv that reads `LOGGED_IN` only for the exact status 0."""

    argv = ("/opt/tools/tool", "status")

    @staticmethod
    def interpret(exit_status: int | None, stdout: str) -> LoginState:
        """Read exit 0 and the word `in` as a login."""
        if exit_status == 0 and stdout.strip() == "in":
            return LoginState.LOGGED_IN
        return LoginState.UNKNOWN


class FreshIds:
    """A new run id per call."""

    def new_run_id(self) -> RunId:
        """Return a random identifier."""
        return RunId(str(uuid.uuid4()))


class RecordingConfinement(SpyConfinement):
    """A spy confinement that also keeps the commands it was given."""

    def __init__(self, journal: Journal, clock: FakeClock) -> None:
        """Start with no commands."""
        super().__init__(journal, clock)
        self.commands: list[WorkerCommand] = []

    def run(  # noqa: PLR0913, PLR0917 - mirrors the port signature
        self,
        command: WorkerCommand,
        workspace: PreparedWorkspace,
        egress: EgressEndpoint,
        limits: Limits,
        on_started: Callable[[ProcessIdentity], None],
        should_stop: Callable[[], bool],
        mode: TaskMode,
    ) -> ExecutionOutcome:
        """Keep the command, then behave like the spy."""
        self.commands.append(command)
        return super().run(command, workspace, egress, limits, on_started, should_stop, mode)


class InspectorCase(unittest.TestCase):
    """An inspector over a spy confinement and a temporary scratch folder."""

    def setUp(self) -> None:
        """Create the scratch root and the inspector."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.scratch = Path(folder.name)
        self.confinement = SpyConfinement(Journal(), FakeClock(start=0.0))
        self.confinement.outcome = ExecutionOutcome(exit_status=0, stdout="in\n")
        self.inspector = ConfinedLoginInspector(
            confinement=self.confinement,
            ids=FreshIds(),
            scratch_root=str(self.scratch),
            limits=LIMITS,
            home="/home/example",
            user="example",
            path="/usr/bin:/bin",
        )


class RunsThroughTheConfinementTests(InspectorCase):
    """No observation ever runs outside the confinement."""

    def test_the_probe_command_runs_confined_in_read_mode_with_the_configured_limits(self) -> None:
        """One confined run, read mode, the inspector limits."""
        self.inspector.observe(FixedProbe())
        self.assertEqual(self.confinement.modes, [TaskMode.READ], msg="read mode, one run")
        self.assertEqual(self.confinement.limits_seen, [LIMITS], msg="limits from settings")

    def test_the_outcome_is_read_by_the_probe(self) -> None:
        """Exit status and standard output go to `interpret`."""
        observation = self.inspector.observe(FixedProbe())
        self.assertEqual(observation.state, LoginState.LOGGED_IN, msg="read as logged in")
        self.confinement.outcome = ExecutionOutcome(exit_status=2, stdout="in\n")
        self.assertEqual(
            self.inspector.observe(FixedProbe()).state, LoginState.UNKNOWN, msg="exit status counts"
        )

    def test_a_tool_that_cannot_be_launched_confined_is_unknown_and_never_run_unconfined(
        self,
    ) -> None:
        """LaunchError becomes UNKNOWN with the reason in the note."""
        self.confinement.launch_error = LaunchError("sandbox-exec is not usable")
        observation = self.inspector.observe(FixedProbe())
        self.assertEqual(observation.state, LoginState.UNKNOWN, msg="unknown")
        self.assertIn("sandbox-exec is not usable", observation.note, msg="the reason is kept")

    def test_a_limit_hit_or_a_survivor_makes_the_observation_unknown(self) -> None:
        """A status command that ran away is not trusted."""
        for outcome in (
            ExecutionOutcome(exit_status=0, stdout="in\n", limit_hit=LimitKind.TIME),
            ExecutionOutcome(exit_status=0, stdout="in\n", survivors=("1234 sleep",)),
        ):
            with self.subTest(outcome=outcome):
                self.confinement.outcome = outcome
                observation = self.inspector.observe(FixedProbe())
                self.assertEqual(observation.state, LoginState.UNKNOWN, msg="not trusted")
                self.assertTrue(observation.note, msg="says why")


class SandboxTests(InspectorCase):
    """Each observation has its own scratch sandbox, removed afterwards."""

    def test_the_sandbox_is_private_unique_and_removed(self) -> None:
        """Nothing is left behind, on success or on a launch error."""
        self.inspector.observe(FixedProbe())
        self.confinement.launch_error = LaunchError("x")
        self.inspector.observe(FixedProbe())
        self.assertEqual(list(self.scratch.iterdir()), [], msg="scratch folder is empty again")

    def test_the_command_gets_a_minimal_environment_and_no_brief(self) -> None:
        """HOME and USER for the tool's own login, a private TMPDIR, no proxy exemptions."""
        recording = RecordingConfinement(Journal(), FakeClock(start=0.0))
        recording.outcome = ExecutionOutcome(exit_status=0, stdout="in\n")
        inspector = ConfinedLoginInspector(
            confinement=recording,
            ids=FreshIds(),
            scratch_root=str(self.scratch),
            limits=LIMITS,
            home="/home/example",
            user="example",
            path="/usr/bin:/bin",
        )
        inspector.observe(FixedProbe())
        (command,) = recording.commands
        self.assertEqual(command.argv, FixedProbe.argv, msg="the probe's argv, unchanged")
        self.assertEqual(command.stdin, "", msg="no brief")
        self.assertEqual(command.environment["HOME"], "/home/example", msg="HOME")
        self.assertEqual(command.environment["USER"], "example", msg="USER")
        self.assertTrue(
            command.environment["TMPDIR"].startswith(str(self.scratch)), msg="private TMPDIR"
        )
        self.assertNotIn("NO_PROXY", command.environment, msg="no proxy exemption")


if __name__ == "__main__":
    unittest.main()
