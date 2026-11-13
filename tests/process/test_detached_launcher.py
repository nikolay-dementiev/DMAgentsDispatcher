"""Tests of the detached supervisor launch: brief channel, environment, session, survival."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from pathlib import Path

from agents_dispatcher.adapters.system.detached_launcher import (
    DetachedLauncherConfig,
    DetachedSupervisorLauncher,
)
from agents_dispatcher.application.errors import LaunchError
from agents_dispatcher.domain.run import RunId
from tests.support.hard_timeout import HardTimeoutCase

# A stand-in for the supervisor bootstrap: it records what it received and then may linger.
RECORDER = textwrap.dedent(
    """
    import json, os, sys, time
    out, linger = sys.argv[1], float(sys.argv[2])
    brief = sys.stdin.read()
    record = {
        "argv": sys.argv[3:],
        "env": dict(os.environ),
        "brief": brief,
        "sid": os.getsid(0),
        "pgid": os.getpgrp(),
        "pid": os.getpid(),
        "flags_isolated": sys.flags.isolated,
        "cwd": os.getcwd(),
    }
    with open(out + ".tmp", "w") as handle:
        json.dump(record, handle)
    os.rename(out + ".tmp", out)
    time.sleep(linger)
    open(out + ".done", "w").close()
    """
)
CANARY = "AGENTSD_TEST_CANARY"


class DetachedLauncherTests(HardTimeoutCase):
    """What the launched process sees and what the caller keeps."""

    def setUp(self) -> None:
        """Create a scratch folder with the recorder script."""
        super().setUp()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.script = self.root / "recorder.py"
        self.script.write_text(RECORDER, encoding="utf-8")
        self.out = self.root / "record.json"
        self.run_id = RunId(str(uuid.uuid4()))

    def launcher(
        self, *, linger: float = 0.0, interpreter: str = sys.executable
    ) -> DetachedSupervisorLauncher:
        """Build a launcher that starts the recorder instead of the real supervisor."""
        config = DetachedLauncherConfig(
            interpreter=interpreter,
            bootstrap=str(self.script),
            leading_args=(str(self.out), str(linger)),
            account_home="/home/example",
            account_user="example",
            path="/usr/bin:/bin",
            store_path="/state/example",
            user_settings="",
            write_timeout_s=5.0,
        )
        return DetachedSupervisorLauncher(config)

    def record(self) -> dict[str, object]:
        """Wait for the recorder's file and load it."""
        deadline = time.monotonic() + 20.0
        while not self.out.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.out.exists(), msg="the launched process wrote its record")
        record: dict[str, object] = json.loads(self.out.read_text(encoding="utf-8"))
        return record

    def test_the_brief_arrives_on_standard_input_and_nowhere_else(self) -> None:
        """Not in argv, not in the environment: other accounts can read both."""
        brief = "secret brief " + "x" * 200_000
        self.launcher().launch(self.run_id, brief)
        record = self.record()
        self.assertEqual(record["brief"], brief, msg="the whole brief, larger than a pipe buffer")
        self.assertNotIn("secret brief", json.dumps(record["argv"]), msg="not in argv")
        self.assertNotIn("secret brief", json.dumps(record["env"]), msg="not in the environment")

    def test_the_environment_is_exactly_the_four_account_variables(self) -> None:
        """The caller's environment never reaches the supervisor or, through it, the worker."""
        os.environ[CANARY] = "leak"
        self.addCleanup(os.environ.pop, CANARY, None)
        self.launcher().launch(self.run_id, "brief")
        env = self.record()["env"]
        self.assertIsInstance(env, dict, msg="env recorded")
        assert isinstance(env, dict)  # noqa: S101 - narrows the type for the checker in a test
        self.assertEqual(
            {key: env[key] for key in ("HOME", "USER", "LOGNAME", "PATH")},
            {
                "HOME": "/home/example",
                "USER": "example",
                "LOGNAME": "example",
                "PATH": "/usr/bin:/bin",
            },
            msg="the account values",
        )
        self.assertNotIn(CANARY, env, msg="the canary does not travel")

    def test_the_arguments_name_the_run_and_the_account_and_run_isolated(self) -> None:
        """The supervisor learns its configuration from argv and is started with `-I`."""
        self.launcher().launch(self.run_id, "brief")
        record = self.record()
        self.assertEqual(
            record["argv"],
            [
                "--run-id",
                self.run_id.value,
                "--store-path",
                "/state/example",
                "--account-home",
                "/home/example",
                "--account-user",
                "example",
                "--user-settings",
                "",
            ],
            msg="argv",
        )
        self.assertEqual(record["flags_isolated"], 1, msg="python -I")

    def test_the_supervisor_leads_its_own_session_and_the_call_does_not_wait(self) -> None:
        """A new session survives the caller's terminal; launch returns while it still runs."""
        started = time.monotonic()
        self.launcher(linger=3.0).launch(self.run_id, "brief")
        self.assertLess(time.monotonic() - started, 2.5, msg="launch returned before the end")
        record = self.record()
        self.assertNotEqual(record["sid"], os.getsid(0), msg="a new session")
        self.assertEqual(record["sid"], record["pid"], msg="the supervisor leads it")

    def test_an_unusable_interpreter_is_a_launch_error(self) -> None:
        """The failure names the cause and nothing is left half started."""
        with self.assertRaises(LaunchError):
            self.launcher(interpreter="/nonexistent/python").launch(self.run_id, "brief")

    def test_the_supervisor_survives_the_death_of_the_calling_process_group(self) -> None:
        """Killing the caller right after `start` must not stop the run."""
        caller = textwrap.dedent(
            f"""
            import sys, os
            sys.path.insert(0, {str(Path(__file__).resolve().parents[2] / "src")!r})
            from agents_dispatcher.adapters.system.detached_launcher import (
                DetachedLauncherConfig, DetachedSupervisorLauncher)
            from agents_dispatcher.domain.run import RunId
            config = DetachedLauncherConfig(
                interpreter=sys.executable, bootstrap={str(self.script)!r},
                leading_args=({str(self.out)!r}, "2.0"), account_home="/h", account_user="u",
                path="/usr/bin:/bin", store_path="/s", user_settings="", write_timeout_s=5.0)
            DetachedSupervisorLauncher(config).launch(RunId({self.run_id.value!r}), "brief")
            print("launched", flush=True)
            import time; time.sleep(60)
            """
        )
        child = subprocess.Popen(  # noqa: S603 - argv array of this interpreter, no shell
            [sys.executable, "-I", "-c", caller],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(child.wait)
        if child.stdout is not None:
            self.addCleanup(child.stdout.close)
        line = child.stdout.readline() if child.stdout else ""
        self.assertEqual(line.strip(), "launched", msg="the caller launched")
        self.record()
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=10)
        deadline = time.monotonic() + 20.0
        done = Path(str(self.out) + ".done")
        while not done.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(done.exists(), msg="the supervisor ran to its end after the caller died")


if __name__ == "__main__":
    unittest.main()
