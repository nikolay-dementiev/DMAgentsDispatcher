"""Process tests of the account dialogue pipeline against a fake app-server, no network."""

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.codex_account import CodexAccountDialogue
from agents_dispatcher.domain.login import LoginState
from agents_dispatcher.domain.quota import EvidenceKind
from tests.support.fake_executable import FakeExecutable, SourceRef

SOURCE = SourceRef(
    kind="captured_output",
    cli_name="codex",
    cli_version="0.160.0",
    excerpt=(
        "codex app-server --listen stdio:// reads one JSON request per line, answers each, "
        "and exits at the end of its input (measured offline, MEASURE-9)."
    ),
)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "codex" / "appserver_dialogue.jsonl"
# The fake imitates only what the pipeline depends on: it reads request lines, answers when the
# last one arrives, and ends at the end of its input (measured behaviour of the real server).
ANSWERING = """while IFS= read -r line; do
  case "$line" in
    *'"id": 6,'*) cat "$REPLIES" ;;
  esac
done
"""
SILENT = """while IFS= read -r line; do :; done
"""


class PipelineCase(unittest.TestCase):
    """A temporary folder with a fake tool and the captured replies."""

    def setUp(self) -> None:
        """Write the replies file."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        lines = []
        for raw in FIXTURE.read_text(encoding="utf-8").splitlines():
            entry = json.loads(raw)
            if "got" in entry:
                lines.append(json.dumps(entry["got"], separators=(",", ":")))
        self.replies = self.root / "replies.jsonl"
        self.replies.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def tool(self, text: str) -> str:
        """Write a fake tool and return its path."""
        return str(FakeExecutable("codex", text, SOURCE).write_to(self.root))

    def run_pipeline(self, dialogue: CodexAccountDialogue) -> tuple[int, str, float]:
        """Run the dialogue's command; return the exit status, standard output and seconds."""
        began = time.monotonic()
        finished = subprocess.run(  # noqa: S603 - the argv is the code under test
            dialogue.argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env={
                "PATH": "/usr/bin:/bin",
                "TMPDIR": str(self.root),
                "REPLIES": str(self.replies),
                "HOME": os.environ.get("HOME", "/"),
            },
        )
        return finished.returncode, finished.stdout, time.monotonic() - began


class PipelineTests(PipelineCase):
    """The pipeline ends when the last reply is in, and never waits past its bound."""

    def test_it_returns_as_soon_as_the_last_reply_arrives(self) -> None:
        """No fixed sleep: well under the bound, and the whole dialogue is read."""
        dialogue = CodexAccountDialogue(self.tool(ANSWERING), hold_s=8)
        status, stdout, seconds = self.run_pipeline(dialogue)
        self.assertEqual(status, 0, msg="the server's exit status is kept")
        self.assertLess(seconds, 4.0, msg="did not wait for the bound")
        reading = dialogue.interpret(status, stdout)
        self.assertEqual(reading.login, LoginState.LOGGED_IN, msg="login read")
        self.assertEqual(reading.quota.kind, EvidenceKind.SNAPSHOT, msg="quota read")

    def test_a_server_that_never_answers_ends_at_the_bound(self) -> None:
        """The wait is bounded; the unusable output is not a state."""
        dialogue = CodexAccountDialogue(self.tool(SILENT), hold_s=1)
        status, stdout, seconds = self.run_pipeline(dialogue)
        self.assertGreaterEqual(seconds, 0.9, msg="waited for the bound")
        self.assertLess(seconds, 6.0, msg="and not much longer")
        reading = dialogue.interpret(status, stdout)
        self.assertEqual(reading.login, LoginState.UNKNOWN, msg="no state from silence")

    def test_a_failing_server_keeps_its_exit_status(self) -> None:
        """The status of the server, not of the helper commands, reaches the reader."""
        failing = "cat >/dev/null\nexit 3\n"
        dialogue = CodexAccountDialogue(self.tool(failing), hold_s=1)
        status, _stdout, _seconds = self.run_pipeline(dialogue)
        self.assertEqual(status, 3, msg="exit status passes through")


if __name__ == "__main__":
    unittest.main()
