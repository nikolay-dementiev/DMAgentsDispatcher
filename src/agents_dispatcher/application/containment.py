"""The cleanup rounds of a run's processes (O1 layer 3): stop, check, kill, observe.

One component serves the end of a run, the orphan sweep and the quarantine re-check. A round
looks at the table, attributes processes to the run, and when it finds any it pauses all of them
first (a paused process cannot fork), looks again for what appeared meanwhile, and only then ends
them. The run counts as empty after `empty_rounds` consecutive complete looks that found nothing;
a look that could not read the whole table proves nothing and resets the count.
"""

from dataclasses import dataclass, replace
from enum import Enum

from agents_dispatcher.application.ports import (
    Clock,
    ProcessCensus,
    Signaller,
    SignalResult,
    Sleeper,
)
from agents_dispatcher.application.settings import ObservationSettings
from agents_dispatcher.domain.attribution import Attributed, Attribution, RunFacts, attribute
from agents_dispatcher.domain.process import ProcessKey
from agents_dispatcher.domain.run import Survivor

ROUND_PAUSE_S = 0.1  # between rounds of one cleanup: the processes were just signalled


class CleanupVerdict(Enum):
    """How the cleanup ended."""

    OBSERVED_EMPTY = "OBSERVED_EMPTY"
    CLEANUP_UNCERTAIN = "CLEANUP_UNCERTAIN"


@dataclass(frozen=True)
class CleanupResult:
    """What the cleanup did and what it could not settle."""

    verdict: CleanupVerdict
    rounds: int
    killed: tuple[ProcessKey, ...]
    survivors: tuple[Attributed, ...]  # last seen alive, or refused; empty when observed empty
    residual: tuple[ProcessKey, ...]  # path matches that must never be signalled
    remembered: frozenset[ProcessKey]

    def unresolved(self) -> tuple[Survivor, ...]:
        """Name the processes that were left alive, with why each one belongs to the run."""
        return tuple(
            Survivor(
                pid=item.key.pid,
                start_time=item.key.start_time,
                sources=tuple(sorted(source.value.lower() for source in item.sources)),
            )
            for item in self.survivors
        )


class ContainmentCleanup:
    """Ends every process that belongs to a run and says honestly whether none is left."""

    def __init__(
        self,
        census: ProcessCensus,
        signaller: Signaller,
        sleeper: Sleeper,
        settings: ObservationSettings,
    ) -> None:
        """Keep the table reader, the signaller, the sleeper and the round bounds."""
        self._census = census
        self._signaller = signaller
        self._sleeper = sleeper
        self._settings = settings

    def run(self, facts: RunFacts) -> CleanupResult:
        """Repeat rounds until the run is observed empty or `cleanup_rounds` rounds are spent."""
        remembered = facts.remembered
        empty_streak = 0
        killed: list[ProcessKey] = []
        last: Attribution | None = None
        refused: dict[ProcessKey, Attributed] = {}
        spent = 0
        for number in range(1, self._settings.cleanup_rounds + 1):
            spent = number
            snapshot = self._census.look(paths=True)
            last = attribute(snapshot.rows, replace(facts, remembered=remembered))
            remembered = last.remembered
            if not last.found:
                empty_streak = empty_streak + 1 if snapshot.complete else 0
                if empty_streak >= self._settings.empty_rounds:
                    return self._result(CleanupVerdict.OBSERVED_EMPTY, number, killed, (), last)
            else:
                empty_streak = 0
                refused = self._strike(facts, last, killed)
                if len(refused) == len(last.found):
                    return self._result(
                        CleanupVerdict.CLEANUP_UNCERTAIN,
                        number,
                        killed,
                        tuple(refused.values()),
                        last,
                    )
            self._sleeper.sleep(ROUND_PAUSE_S)
        survivors = last.found if last is not None else ()
        return self._result(CleanupVerdict.CLEANUP_UNCERTAIN, spent, killed, survivors, last)

    def _strike(
        self, facts: RunFacts, seen: Attribution, killed: list[ProcessKey]
    ) -> dict[ProcessKey, Attributed]:
        """Pause everything found, look again for new arrivals, then end it; return the refused."""
        refused: dict[ProcessKey, Attributed] = {}
        targets = {item.key: item for item in seen.found}
        for item in seen.found:
            self._note(self._signaller.stop(item.key), item, refused)
        again = self._census.look(paths=True)
        for item in attribute(again.rows, replace(facts, remembered=seen.remembered)).found:
            if item.key not in targets:
                targets[item.key] = item
                self._note(self._signaller.stop(item.key), item, refused)
        for key, item in targets.items():
            result = self._signaller.kill(key)
            if result is SignalResult.SENT:
                killed.append(key)
            self._note(result, item, refused)
        return refused

    @staticmethod
    def _note(
        result: SignalResult, item: Attributed, refused: dict[ProcessKey, Attributed]
    ) -> None:
        """Remember a process whose signal was refused: it cannot be settled by trying again."""
        if result is SignalResult.DENIED:
            refused[item.key] = item

    @staticmethod
    def _result(
        verdict: CleanupVerdict,
        rounds: int,
        killed: list[ProcessKey],
        survivors: tuple[Attributed, ...],
        last: Attribution | None,
    ) -> CleanupResult:
        """Assemble the result from the last look."""
        return CleanupResult(
            verdict=verdict,
            rounds=rounds,
            killed=tuple(killed),
            survivors=survivors,
            residual=last.residual if last is not None else (),
            remembered=last.remembered if last is not None else frozenset(),
        )


class RunObserver:
    """Watches a running run: remembers descendants and weighs the tree it tracks.

    The table is read at most once per `process_interval_s`; the dearer read of working
    directories and open files at most once per `path_interval_s`. Between looks the last weight
    stands. A descendant is remembered by pid and start time, so it stays attributed after it
    changes group, parent, environment and directory.
    """

    def __init__(
        self, census: ProcessCensus, clock: Clock, settings: ObservationSettings, facts: RunFacts
    ) -> None:
        """Keep the table reader, the clock, the clocks of the look and the run's facts."""
        self._census = census
        self._clock = clock
        self._settings = settings
        self._facts = facts
        self._last_look: float | None = None
        self._last_paths: float | None = None
        start = clock.now()
        self._complete_look = start
        self._complete_paths = start
        self._weight = 0
        self._descriptors = 0

    @property
    def remembered(self) -> frozenset[ProcessKey]:
        """Return every process attributed to the run so far."""
        return self._facts.remembered

    def memory(self) -> int:
        """Return the footprint in bytes of the tracked tree, looking again when one is due."""
        now = self._clock.now()
        if self._last_look is None or now - self._last_look >= self._settings.process_interval_s:
            self._look(now)
        return self._weight

    def lost(self) -> str | None:
        """Return what can no longer be observed because the last complete look is too old."""
        now = self._clock.now()
        for name, last, maximum in (
            ("process", self._complete_look, self._settings.process_max_age_s),
            ("path", self._complete_paths, self._settings.path_max_age_s),
        ):
            age = now - last
            if age > maximum:
                return f"no complete {name} look for {age:.1f} s (maximum {maximum:g} s)"
        return None

    def descriptors(self) -> int:
        """Return the open descriptors summed over the tracked tree, looking again when due."""
        self.memory()
        return self._descriptors

    def _look(self, now: float) -> None:
        """Read the table, attribute, remember and weigh."""
        paths = self._last_paths is None or now - self._last_paths >= self._settings.path_interval_s
        snapshot = self._census.look(paths=paths)
        self._last_look = now
        if paths:
            self._last_paths = now
        if snapshot.complete:
            self._complete_look = now
            if paths:
                self._complete_paths = now
        attribution = attribute(snapshot.rows, self._facts)
        self._facts = replace(self._facts, remembered=attribution.remembered)
        self._weight = sum(self._census.footprint(item.key.pid) or 0 for item in attribution.found)
        held = {row.key: row.descriptors for row in snapshot.rows}
        self._descriptors = sum(held.get(item.key, 0) for item in attribution.found)
