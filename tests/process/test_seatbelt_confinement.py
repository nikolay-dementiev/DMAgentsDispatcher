"""Tests of the Seatbelt confinement with real sandbox-exec, real children and real signals.

Every claim is judged by the file system, the network and the process table, never by what the
worker says about itself (digest section 2).
"""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from collections.abc import Callable
from pathlib import Path

from agents_dispatcher.adapters.system.os_signaller import OsSignaller
from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.adapters.system.seatbelt_confinement import MARKER_ENV, SeatbeltConfinement
from agents_dispatcher.adapters.system.seatbelt_profile import (
    CLAUDE_CODE,
    ProviderProfile,
    VendorWrite,
    codex_profile,
)
from agents_dispatcher.adapters.system.settings_loader import load_settings, shipped_defaults_text
from agents_dispatcher.application.errors import LaunchError
from agents_dispatcher.application.ports import (
    EgressEndpoint,
    ExecutionOutcome,
    PreparedWorkspace,
    ProcessIdentity,
    Signaller,
    SignalResult,
    WorkerCommand,
)
from agents_dispatcher.composition.wiring import denied_read_paths
from agents_dispatcher.domain.process import Absence, ProcessBasics, ProcessKey
from agents_dispatcher.domain.routing import TaskMode
from agents_dispatcher.domain.run import LimitKind, Limits, RunId
from tests.support.confinement_kit import Knobs, real_confinement
from tests.support.hard_timeout import HardTimeoutCase
from tests.support.narrow import present

BIG = Limits(hard_s=60.0, memory_bytes=8 * 1024**3)
TERM_GRACE_S = 0.5
KILL_WAIT_S = 1.0
MIB = 1024 * 1024


def alive(pid: int) -> bool:
    """Return whether a process with this pid exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


STOP_AFTER_POLLS = 3


class ConfinementCase(HardTimeoutCase):
    """A sandbox directory, a directory outside it, a listener standing in for the proxy."""

    def setUp(self) -> None:
        """Create the directories and the fake proxy port."""
        super().setUp()
        # A fresh id per test: the marker sweep must never see another test's (or suite's) workers.
        self.run_id = RunId(str(uuid.uuid4()))
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name).resolve()
        self.sandbox = root / self.run_id.sandbox_name
        self.worktree = self.sandbox / "worktree"
        self.worktree.mkdir(parents=True)
        self.outside = root / "outside"
        self.outside.mkdir()
        self.secret = root / "secret"
        self.secret.mkdir()
        (self.secret / "token.txt").write_text("hunter2", encoding="utf-8")
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.addCleanup(self.listener.close)
        self.other_listener = socket.socket()
        self.other_listener.bind(("127.0.0.1", 0))
        self.other_listener.listen()
        self.addCleanup(self.other_listener.close)
        self.proxy_port = self.listener.getsockname()[1]
        self.other_port = self.other_listener.getsockname()[1]
        self.table = LibprocProcessTable()
        self.signaller: Signaller | None = None
        self.knobs = Knobs()
        self.started: list[ProcessIdentity] = []

    def confinement(
        self, *, denied: tuple[str, ...] = (), provider: ProviderProfile = CLAUDE_CODE
    ) -> SeatbeltConfinement:
        """Build the confinement under test."""
        return real_confinement(
            interpreter=sys.executable,
            provider=provider,
            term_grace_s=TERM_GRACE_S,
            kill_wait_s=KILL_WAIT_S,
            denied=denied,
            table=self.table,
            signaller=self.signaller,
            knobs=self.knobs,
        )

    def execute(  # noqa: PLR0913 - one keyword per thing a process test varies
        self,
        code: str,
        *,
        limits: Limits = BIG,
        denied: tuple[str, ...] = (),
        argv: tuple[str, ...] | None = None,
        mode: TaskMode = TaskMode.WRITE,
        should_stop: Callable[[], bool] = lambda: False,
        provider: ProviderProfile = CLAUDE_CODE,
    ) -> ExecutionOutcome:
        """Run Python `code` (or `argv`) as the worker and return the outcome."""
        workspace = PreparedWorkspace(
            base_commit="b" * 40,
            run_id=self.run_id,
            sandbox_dir=str(self.sandbox),
            worktree_dir=str(self.worktree),
            branch_name=self.run_id.branch_name,
        )
        command = WorkerCommand(
            argv=argv or (sys.executable, "-B", "-c", textwrap.dedent(code)),
            environment={"PATH": "/usr/bin:/bin", "HOME": str(self.outside)},
            stdin="the brief",
            cwd=str(self.worktree),
        )
        egress = EgressEndpoint(host="127.0.0.1", port=self.proxy_port)
        return self.confinement(denied=denied, provider=provider).run(
            command, workspace, egress, limits, self.started.append, should_stop, mode
        )


class WriteAndReadTests(ConfinementCase):
    """File-system confinement."""

    def test_writes_are_allowed_in_the_sandbox_and_denied_everywhere_else(self) -> None:
        """Judged by the files that exist afterwards."""
        outcome = self.execute(
            f"""
            import pathlib
            for target in ({str(self.worktree / "in.txt")!r}, {str(self.outside / "out.txt")!r}):
                try:
                    pathlib.Path(target).write_text("x")
                except OSError:
                    pass
            """
        )
        self.assertEqual(outcome.exit_status, 0, msg="the worker ran")
        self.assertTrue((self.worktree / "in.txt").exists(), msg="sandbox write allowed")
        self.assertFalse((self.outside / "out.txt").exists(), msg="outside write denied")

    def test_denied_read_areas_cannot_be_read_but_the_sandbox_can(self) -> None:
        """The read-deny inventory is enforced."""
        (self.sandbox / "note.txt").write_text("fine", encoding="utf-8")
        outcome = self.execute(
            f"""
            import json, pathlib
            result = {{}}
            for name, target in (("secret", {str(self.secret / "token.txt")!r}),
                                 ("note", {str(self.sandbox / "note.txt")!r})):
                try:
                    result[name] = pathlib.Path(target).read_text()
                except OSError:
                    result[name] = "denied"
            print(json.dumps(result))
            """,
            denied=(str(self.secret),),
        )
        self.assertEqual(
            json.loads(outcome.stdout),
            {"secret": "denied", "note": "fine"},
            msg="secret denied, sandbox readable",
        )

    def test_the_brief_arrives_on_standard_input_and_output_is_captured(self) -> None:
        """Standard streams are files in the sandbox, not shell strings."""
        outcome = self.execute("import sys; print(sys.stdin.read().upper())")
        self.assertEqual(outcome.stdout.strip(), "THE BRIEF", msg="stdin to stdout")
        self.assertEqual(outcome.exit_status, 0, msg="exit status")


class LoginStateIsolationTests(ConfinementCase):
    """D-1: a worker reads its own tool's login and is denied every other tool's."""

    def read_each(self, provider: str) -> dict[str, str]:
        """Run a real child under the denies of `provider`; report which login files it read."""
        home = self.outside / "home"
        files = {
            "codex": home / ".codex" / "auth.json",
            "claude_dir": home / ".claude" / "credentials.json",
            "claude_json": home / ".claude.json",
        }
        for path in files.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("login", encoding="utf-8")
        settings = load_settings(
            defaults_text=shipped_defaults_text(), user_text=None, account_home=str(home)
        )
        outcome = self.execute(
            f"""
            import json, pathlib
            result = {{}}
            for name, target in { ({name: str(path) for name, path in files.items()})!r}.items():
                try:
                    result[name] = pathlib.Path(target).read_text()
                except OSError:
                    result[name] = "denied"
            print(json.dumps(result))
            """,
            denied=denied_read_paths(settings, provider),
        )
        return dict(json.loads(outcome.stdout))

    def test_a_codex_worker_reads_codex_state_and_not_claude_code_state(self) -> None:
        """Real child under the openai denies."""
        self.assertEqual(
            self.read_each("openai"),
            {"codex": "login", "claude_dir": "denied", "claude_json": "denied"},
            msg="own login readable, the other tool's denied",
        )

    def test_a_claude_code_worker_reads_its_state_and_not_codex_state(self) -> None:
        """Real child under the anthropic denies."""
        self.assertEqual(
            self.read_each("anthropic"),
            {"codex": "denied", "claude_dir": "login", "claude_json": "login"},
            msg="own login readable, the other tool's denied",
        )


class ReadModeTests(ConfinementCase):
    """A read task cannot change the worktree, whatever the worker does (judged by the files)."""

    def test_a_read_worker_cannot_create_modify_or_delete_in_the_worktree(self) -> None:
        """Create, overwrite, delete, make a folder and append: all denied; tmp stays writable."""
        (self.worktree / "tracked.txt").write_text("original", encoding="utf-8")
        tmp = self.sandbox / "tmp"
        tmp.mkdir()
        outcome = self.execute(
            f"""
            import os, pathlib
            worktree = pathlib.Path({str(self.worktree)!r})
            attempts = [
                lambda: (worktree / "new.txt").write_text("x"),
                lambda: (worktree / "tracked.txt").write_text("changed"),
                lambda: (worktree / "tracked.txt").unlink(),
                lambda: (worktree / "folder").mkdir(),
                lambda: open(worktree / "tracked.txt", "a").write("more"),
            ]
            for attempt in attempts:
                try:
                    attempt()
                except OSError:
                    pass
            pathlib.Path({str(tmp / "scratch.txt")!r}).write_text("kept")
            """,
            mode=TaskMode.READ,
        )
        self.assertEqual(outcome.exit_status, 0, msg="the worker ran to its end")
        self.assertEqual(
            sorted(p.name for p in self.worktree.iterdir()), ["tracked.txt"], msg="set"
        )
        self.assertEqual(
            (self.worktree / "tracked.txt").read_text(encoding="utf-8"),
            "original",
            msg="content untouched",
        )
        self.assertEqual((tmp / "scratch.txt").read_text(encoding="utf-8"), "kept", msg="tmp ok")

    def test_a_read_worker_cannot_write_into_the_worktree_through_a_link(self) -> None:
        """Hard and symbolic links from the scratch folder must not reopen the worktree."""
        target = self.worktree / "tracked.txt"
        target.write_text("original", encoding="utf-8")
        tmp = self.sandbox / "tmp"
        tmp.mkdir()
        outcome = self.execute(
            f"""
            import os, pathlib
            tmp = pathlib.Path({str(tmp)!r})
            target = {str(target)!r}
            for name, make in (("hard", os.link), ("soft", os.symlink)):
                try:
                    make(target, tmp / name)
                    (tmp / name).write_text("changed through " + name)
                except OSError:
                    pass
            """,
            mode=TaskMode.READ,
        )
        self.assertEqual(outcome.exit_status, 0, msg="the worker ran to its end")
        self.assertEqual(target.read_text(encoding="utf-8"), "original", msg="content untouched")

    def test_a_read_worker_can_still_read_the_worktree(self) -> None:
        """Reading is the whole point of a read task."""
        (self.worktree / "tracked.txt").write_text("quote me", encoding="utf-8")
        outcome = self.execute(
            f"print(open({str(self.worktree / 'tracked.txt')!r}).read())", mode=TaskMode.READ
        )
        self.assertEqual(outcome.stdout.strip(), "quote me", msg="the file was read")


LOOKUP = """
import ctypes, json, pathlib
lib = ctypes.CDLL("/usr/lib/libSystem.dylib")
lib.mach_task_self.restype = ctypes.c_uint32
port = ctypes.c_uint32(0)
lib.task_get_special_port(ctypes.c_uint32(lib.mach_task_self()), 4, ctypes.byref(port))
found = {}
for name in NAMES:
    out = ctypes.c_uint32(0)
    found[name] = lib.bootstrap_look_up(port, name.encode(), ctypes.byref(out))
pathlib.Path(RESULT).write_text(json.dumps(found))
"""
PR3_SERVICES = (
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.SystemConfiguration.configd",
    "com.apple.trustd",
    "com.apple.trustd.agent",
    "com.apple.securityd.xpc",
)


class ProviderProfileTests(ConfinementCase):
    """The provider decides the Mach services and the vendor-state writes (judged by the kernel)."""

    def lookups(self, provider: ProviderProfile) -> dict[str, int]:
        """Return the bootstrap lookup status of the five PR3 services under `provider`."""
        result = self.sandbox / "lookups.json"
        code = f"NAMES = {PR3_SERVICES!r}\nRESULT = {str(result)!r}\n{LOOKUP}"
        outcome = self.execute(code, provider=provider)
        self.assertEqual(outcome.exit_status, 0, msg="the lookup worker ran to its end")
        found: dict[str, int] = json.loads(result.read_text(encoding="utf-8"))
        return found

    def test_the_codex_profile_resolves_the_five_services_and_claude_code_does_not(self) -> None:
        """A lookup status of 0 is a resolved service; the denied ones fail."""
        codex = self.lookups(codex_profile())
        self.assertEqual(
            set(codex.values()), {0}, msg="all five services resolve under the Codex profile"
        )
        claude = self.lookups(CLAUDE_CODE)
        self.assertNotIn(0, claude.values(), msg="none of the five resolves for Claude Code")

    def test_granted_vendor_paths_are_writable_and_configuration_is_not(self) -> None:
        """Grants open exactly their paths; `config.toml` and `auth.json` stay write-denied."""
        vendor = self.outside / "home" / ".codex"
        (vendor / "tmp").mkdir(parents=True)
        (vendor / "config.toml").write_text("keep", encoding="utf-8")
        (vendor / "auth.json").write_text("keep", encoding="utf-8")
        provider = ProviderProfile(
            mach_services=CLAUDE_CODE.mach_services,
            vendor_writes=(
                VendorWrite("subpath", str(vendor / "tmp")),
                VendorWrite("literal", str(vendor / "installation_id")),
                VendorWrite("regex", f"^{vendor}/state_[0-9]+\\.sqlite$"),
            ),
        )
        outcome = self.execute(
            f"""
            import pathlib
            vendor = pathlib.Path({str(vendor)!r})
            for name in ("tmp/x", "installation_id", "state_1.sqlite", "config.toml",
                         "auth.json", "other.txt", "state_1.sqlite.bak"):
                try:
                    (vendor / name).write_text("changed")
                except OSError:
                    pass
            """,
            provider=provider,
        )
        self.assertEqual(outcome.exit_status, 0, msg="the worker ran to its end")
        self.assertEqual(
            sorted(p.name for p in vendor.iterdir()),
            ["auth.json", "config.toml", "installation_id", "state_1.sqlite", "tmp"],
            msg="only the granted names were created",
        )
        self.assertEqual((vendor / "tmp" / "x").read_text(encoding="utf-8"), "changed")
        for name in ("config.toml", "auth.json"):
            self.assertEqual(
                (vendor / name).read_text(encoding="utf-8"), "keep", msg=f"{name} untouched"
            )

    def test_a_granted_login_adjacent_file_cannot_become_a_link_to_the_login(self) -> None:
        """MEASURE: unlink then link/symlink to `auth.json` fails; a plain rewrite still works."""
        vendor = self.outside / "home" / ".codex"
        vendor.mkdir(parents=True)
        (vendor / "auth.json").write_text("secret", encoding="utf-8")
        (vendor / "installation_id").write_text("id", encoding="utf-8")
        provider = ProviderProfile(
            mach_services=CLAUDE_CODE.mach_services,
            vendor_writes=(VendorWrite("literal", str(vendor / "installation_id")),),
        )
        outcome = self.execute(
            f"""
            import os, pathlib
            vendor = pathlib.Path({str(vendor)!r})
            target = vendor / "installation_id"
            target.unlink()
            for make in (os.symlink, os.link):
                try:
                    make(vendor / "auth.json", target)
                except OSError:
                    pass
            if not target.exists() and not target.is_symlink():
                target.write_text("rewritten")
            """,
            provider=provider,
        )
        self.assertEqual(outcome.exit_status, 0, msg="the worker ran to its end")
        self.assertFalse((vendor / "installation_id").is_symlink(), msg="no symlink was planted")
        self.assertEqual(
            (vendor / "auth.json").stat().st_nlink, 1, msg="no hard link to the login was made"
        )
        self.assertEqual(
            (vendor / "installation_id").read_text(encoding="utf-8"),
            "rewritten",
            msg="the file itself is still writable",
        )
        self.assertEqual(
            (vendor / "auth.json").read_text(encoding="utf-8"), "secret", msg="login untouched"
        )


class NetworkTests(ConfinementCase):
    """Only the proxy port on loopback is reachable."""

    def test_only_the_proxy_port_is_reachable(self) -> None:
        """A connection to another loopback port fails; the proxy port works."""
        outcome = self.execute(
            f"""
            import json, socket
            result = {{}}
            for name, port in (("proxy", {self.proxy_port}), ("other", {self.other_port})):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=3).close()
                    result[name] = "open"
                except OSError:
                    result[name] = "refused"
            print(json.dumps(result))
            """
        )
        self.assertEqual(
            json.loads(outcome.stdout), {"proxy": "open", "other": "refused"}, msg="ports"
        )


class LimitTests(ConfinementCase):
    """Time and memory limits end the run and name the limit."""

    def test_the_hard_time_limit_stops_the_worker(self) -> None:
        """A sleeper is stopped after hard_s and the outcome names the limit."""
        started = time.monotonic()
        outcome = self.execute("import time; time.sleep(120)", limits=Limits(1.0, 8 * 1024**3))
        self.assertEqual(outcome.limit_hit, LimitKind.TIME, msg=f"time limit named: {outcome}")
        self.assertLess(time.monotonic() - started, 15.0, msg="stopped promptly")
        self.assertEqual(outcome.unresolved, (), msg="nothing left alive")

    def test_a_stop_request_ends_the_worker_and_is_reported(self) -> None:
        """The poll is read at the scan interval; a positive answer stops the whole group."""
        polls = []

        def should_stop() -> bool:
            polls.append(time.monotonic())
            return len(polls) >= STOP_AFTER_POLLS

        started = time.monotonic()
        outcome = self.execute("import time; time.sleep(120)", should_stop=should_stop)
        self.assertTrue(outcome.stopped, msg="reported as stopped")
        self.assertIsNone(outcome.limit_hit, msg="no limit was hit")
        self.assertEqual(outcome.unresolved, (), msg="nothing left alive")
        self.assertLess(time.monotonic() - started, 15.0, msg="stopped promptly")
        self.assertGreaterEqual(len(polls), STOP_AFTER_POLLS, msg="the poll ran in the watch loop")

    def test_a_poll_that_stays_false_lets_the_worker_finish(self) -> None:
        """No request: the exit status comes through untouched."""
        outcome = self.execute("import time; time.sleep(0.5)")
        self.assertFalse(outcome.stopped, msg="not stopped")
        self.assertEqual(outcome.exit_status, 0, msg="normal exit")

    def test_the_memory_limit_stops_the_worker(self) -> None:
        """Dirty memory above the limit is caught through the footprint."""
        outcome = self.execute(
            """
            import time
            data = bytearray(400 * 1024 * 1024)
            for index in range(0, len(data), 4096):
                data[index] = 1
            time.sleep(120)
            """,
            limits=Limits(hard_s=60.0, memory_bytes=150 * MIB),
        )
        self.assertEqual(outcome.limit_hit, LimitKind.MEMORY, msg="memory limit named")

    def test_memory_of_a_process_that_left_the_group_still_counts(self) -> None:
        """F4: the sum covers what the marker sweep finds, not only the group."""
        outcome = self.execute(
            """
            import subprocess, sys, time
            code = ("import time\\nd = bytearray(400*1024*1024)\\n"
                    "for i in range(0, len(d), 4096): d[i] = 1\\ntime.sleep(120)")
            subprocess.Popen([sys.executable, "-B", "-c", code], start_new_session=True)
            time.sleep(120)
            """,
            limits=Limits(hard_s=60.0, memory_bytes=150 * MIB),
        )
        self.assertEqual(outcome.limit_hit, LimitKind.MEMORY, msg="escaped memory counted")
        self.assertEqual(outcome.unresolved, (), msg="the escaped process was stopped")


class CleanupTests(ConfinementCase):
    """Nothing the worker started outlives the run."""

    def test_a_term_ignoring_background_child_is_gone_after_the_run(self) -> None:
        """Group stop reaches the whole group."""
        pid_file = self.sandbox / "child.pid"
        outcome = self.execute(
            f"""
            import pathlib, subprocess, sys
            child = subprocess.Popen([sys.executable, "-B", "-c",
                "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(120)"])
            pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))
            """
        )
        child = int(pid_file.read_text(encoding="utf-8"))
        self.assertEqual(outcome.exit_status, 0, msg="the worker itself finished")
        self.assertFalse(alive(child), msg="the background child is gone")
        self.assertEqual(outcome.unresolved, (), msg="no survivor reported alive")

    def test_a_group_member_without_the_marker_is_stopped_by_the_group_stop(self) -> None:
        """Only the group stop can reach a child whose environment hides the marker."""
        pid_file = self.sandbox / "bare.pid"
        outcome = self.execute(
            f"""
            import pathlib, subprocess, sys
            child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(120)"],
                                     env={{}})
            pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))
            """
        )
        child = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 5.0
        while alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(child), msg="the unmarked group member is gone")
        self.assertEqual(outcome.survivors, (), msg="the sweep had nothing to find")

    def test_a_setsid_descendant_with_the_marker_is_found_and_killed(self) -> None:
        """The sweep finds what left the group, kills it, and reports it."""
        pid_file = self.sandbox / "escaped.pid"
        outcome = self.execute(
            f"""
            import pathlib, subprocess, sys
            child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(120)"],
                                     start_new_session=True)
            pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))
            """
        )
        escaped = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 5.0
        while alive(escaped) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(escaped), msg="the escaped process was killed")
        self.assertTrue(
            any(str(escaped) in entry for entry in outcome.survivors), msg="and it was reported"
        )

    def test_a_process_that_cannot_be_signalled_is_reported_alive_not_raised(self) -> None:
        """A refused signal must not abort the cleanup: the process is named as still alive."""
        pid_file = self.sandbox / "refused.pid"

        class RefusingSignaller(OsSignaller):
            """The real signaller, except that it refuses the one process the worker leaves."""

            def _refusal(self, key: ProcessKey) -> SignalResult | None:
                if pid_file.exists() and key.pid == int(pid_file.read_text(encoding="utf-8")):
                    return SignalResult.DENIED
                return super()._refusal(key)

        self.signaller = RefusingSignaller(self.table)
        outcome = self.execute(
            f"""
            import pathlib, subprocess, sys, time
            child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(120)"],
                                     start_new_session=True)
            pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))
            time.sleep(0.5)
            """
        )
        escaped = int(pid_file.read_text(encoding="utf-8"))
        self.addCleanup(os.kill, escaped, signal.SIGKILL)
        self.assertIn(f"pid {escaped}", outcome.survivors, msg="the process was found")
        self.assertIn(
            escaped, [item.pid for item in outcome.unresolved], msg="and reported as alive"
        )

    def test_the_identity_of_the_group_leader_is_reported_at_start(self) -> None:
        """The store gets the group and the leader's start time for the start-up sweep."""
        self.execute("pass")
        identity = present(self.started[0] if self.started else None)
        self.assertGreater(identity.pgid, 1, msg="a real group")
        self.assertGreater(
            present(identity.start_time_s), 1_000_000_000, msg="a wall-clock start time"
        )

    def test_an_unreadable_leader_start_time_is_reported_as_unknown_not_zero(self) -> None:
        """A zero would look like a real start time; None tells the sweep not to trust the row."""

        class BlindTable(LibprocProcessTable):
            """The real table, except that no process can be read."""

            def basics(self, pid: int) -> ProcessBasics | Absence:  # noqa: ARG002 - fake
                return Absence.DENIED

        self.table = BlindTable()
        self.execute("pass")
        identity = present(self.started[0] if self.started else None)
        self.assertIsNone(identity.start_time_s, msg="unknown, not 0.0")
        note = (self.sandbox / "logs" / "supervisor.txt").read_text(encoding="utf-8")
        self.assertIn("DENIED", note, msg="and the reason is written to the run's log folder")

    def test_a_missing_worker_executable_is_a_launch_error(self) -> None:
        """The run never started; the caller maps this to LAUNCH_LOST."""
        with self.assertRaises(LaunchError):
            self.execute("", argv=("/nonexistent/worker",))


class IsolationBetweenRunsTests(ConfinementCase):
    """A run only ever touches processes that carry its own marker."""

    def test_a_process_with_another_runs_marker_survives_this_run(self) -> None:
        """Discriminator for the shared-marker flake: two suites at once must not collide."""
        other = RunId(str(uuid.uuid4()))
        bystander = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                "print('ready', flush=True); import time; time.sleep(120)",
            ],
            env={**os.environ, MARKER_ENV: other.value},
            start_new_session=True,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        self.addCleanup(bystander.stdout.close if bystander.stdout else (lambda: None))
        if bystander.stdout is not None:
            bystander.stdout.readline()
        self.execute("print('done')")
        self.assertIsNone(bystander.poll(), msg="the other run's process was not touched")


class DeadParentTests(ConfinementCase):
    """SIGKILL of the supervisor must not leave the worker behind."""

    def test_killing_the_supervisor_ends_the_worker_group(self) -> None:
        """The shim sees end-of-file on the control pipe and stops the group."""
        pid_file = self.sandbox / "worker.pid"
        supervisor_code = textwrap.dedent(
            f"""
            import sys
            sys.path[:0] = {sys.path!r}
            from agents_dispatcher.adapters.system.seatbelt_profile import CLAUDE_CODE
            from tests.support.confinement_kit import real_confinement
            from agents_dispatcher.application.ports import (
                EgressEndpoint, PreparedWorkspace, WorkerCommand)
            from agents_dispatcher.domain.routing import TaskMode
            from agents_dispatcher.domain.run import Limits, RunId
            run_id = RunId({self.run_id.value!r})
            workspace = PreparedWorkspace(base_commit="b" * 40, run_id=run_id,
                sandbox_dir={str(self.sandbox)!r}, worktree_dir={str(self.worktree)!r},
                branch_name=run_id.branch_name)
            worker_code = (
                "import os, pathlib, time; "
                "pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
                "time.sleep(300)"
            )
            command = WorkerCommand(
                argv=(sys.executable, "-B", "-c", worker_code),
                environment={{"PATH": "/usr/bin:/bin"}}, stdin="", cwd={str(self.worktree)!r})
            real_confinement(interpreter=sys.executable, provider=CLAUDE_CODE,
                term_grace_s={TERM_GRACE_S}, kill_wait_s={KILL_WAIT_S}).run(
                command, workspace, EgressEndpoint("127.0.0.1", {self.proxy_port}),
                Limits(300.0, 8 * 1024**3), lambda identity: None, lambda: False, TaskMode.WRITE)
            """
        )
        supervisor = subprocess.Popen(  # noqa: S603 - argv array of this interpreter, no shell
            [sys.executable, "-B", "-c", supervisor_code], stderr=subprocess.PIPE
        )
        self.addCleanup(supervisor.wait)
        self.addCleanup(present(supervisor.stderr).close)
        deadline = time.monotonic() + 30.0
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        worker = int(pid_file.read_text(encoding="utf-8"))
        self.assertTrue(alive(worker), msg="the worker runs while the supervisor lives")
        supervisor.send_signal(signal.SIGKILL)
        deadline = time.monotonic() + TERM_GRACE_S + KILL_WAIT_S + 10.0
        while alive(worker) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(alive(worker), msg="the worker is gone after the supervisor was killed")


if __name__ == "__main__":
    unittest.main()
