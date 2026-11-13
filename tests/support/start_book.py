"""In-memory stand-ins for what `start` touches: store, launcher, repository check."""

from dataclasses import dataclass, field

from agents_dispatcher.application.errors import LaunchError, WorkspaceNotGitError
from agents_dispatcher.application.ports import AcceptedRun, FinishRecord, RunSnapshot
from agents_dispatcher.domain.run import RunId, RunState, TerminalReason
from tests.support.run_spies import Journal


@dataclass
class StartBook:
    """A store with the three operations `start` uses; `claim_run` plays the supervisor."""

    journal: Journal
    accepted: list[AcceptedRun] = field(default_factory=list)
    closed: list[tuple[RunId, FinishRecord]] = field(default_factory=list)
    states: dict[RunId, RunState] = field(default_factory=dict)
    reasons: dict[RunId, TerminalReason] = field(default_factory=dict)
    tasks: dict[RunId, str] = field(default_factory=dict)
    faults: dict[RunId, str] = field(default_factory=dict)

    def accept(self, run: AcceptedRun) -> None:
        """Store the run as ACCEPTED."""
        self.journal.add("store.accept")
        self.accepted.append(run)
        self.states[run.run_id] = RunState.ACCEPTED
        self.tasks[run.run_id] = run.task

    def find(self, run_id: RunId) -> RunSnapshot | None:
        """Return a snapshot of the run, or None."""
        state = self.states.get(run_id)
        if state is None:
            return None
        return RunSnapshot(
            run_id=run_id,
            task=self.tasks[run_id],
            state=state,
            terminal_reason=self.reasons.get(run_id),
            branch=run_id.branch_name,
            revision=1,
            stop=None,
            report_json=None,
            fault=self.faults.get(run_id),
        )

    def finish_unclaimed(self, run_id: RunId, record: FinishRecord) -> bool:
        """Close the run only while it is still ACCEPTED."""
        self.journal.add("store.finish_unclaimed")
        if self.states.get(run_id) is not RunState.ACCEPTED:
            return False
        self.states[run_id] = RunState.TERMINAL
        self.reasons[run_id] = record.terminal_reason
        self.closed.append((run_id, record))
        return True

    def claim_run(self, run_id: RunId) -> None:
        """Play the supervisor: the run moves to STARTING."""
        self.states[run_id] = RunState.STARTING


class SpyLauncher:
    """A launcher that records what it was given and may fail or act like a supervisor."""

    def __init__(self, journal: Journal, book: StartBook) -> None:
        """Remember the journal and the book."""
        self.journal = journal
        self.book = book
        self.launched: list[tuple[RunId, str]] = []
        self.error: LaunchError | None = None
        self.claims_at_once = False

    def launch(self, run_id: RunId, brief: str) -> None:
        """Record the launch; fail or claim immediately when configured."""
        self.journal.add("launcher.launch")
        if self.error is not None:
            raise self.error
        self.launched.append((run_id, brief))
        if self.claims_at_once:
            self.book.claim_run(run_id)


class SpyRepository:
    """A repository check that refuses non-Git directories."""

    def __init__(self, journal: Journal) -> None:
        """Remember the journal."""
        self.journal = journal
        self.not_git = False

    def require_git(self, repo: str) -> None:
        """Raise for a non-Git directory."""
        self.journal.add("repo.check")
        if self.not_git:
            message = f"{repo} is not a Git repository"
            raise WorkspaceNotGitError(message)
