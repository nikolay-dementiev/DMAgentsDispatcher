"""Builds a Seatbelt confinement over the real census and signaller, with fast test clocks."""

from collections.abc import Callable
from dataclasses import dataclass, field

from agents_dispatcher.adapters.system.os_disk_walk import OsDiskWalk
from agents_dispatcher.adapters.system.os_signaller import OsSignaller
from agents_dispatcher.adapters.system.process_census import LibprocCensus
from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.adapters.system.seatbelt_confinement import (
    ConfinementConfig,
    SeatbeltConfinement,
)
from agents_dispatcher.adapters.system.seatbelt_profile import ProviderProfile
from agents_dispatcher.adapters.system.system_clock import SuspendInclusiveClock
from agents_dispatcher.adapters.system.system_sleeper import SystemSleeper
from agents_dispatcher.application.containment import ContainmentCleanup
from agents_dispatcher.application.ports import DiskWalk, Signaller
from agents_dispatcher.application.settings import ObservationSettings, WalkSettings
from agents_dispatcher.domain.process import ProcessKey

# Looks every 0.1 s and path looks every 0.2 s keep the real-process tests quick.
FAST_OBSERVATION = ObservationSettings(0.1, 2.0, 0.2, 5.0, 2, 8)


# Generous: the tests that exercise these limits set their own.
ROOMY_FILE_BYTES = 1 << 40
ROOMY_DESCRIPTORS = 1024

FAST_WALK = WalkSettings(interval_s=0.2, maximum_age_s=30.0)


@dataclass
class Knobs:
    """The resource limits and the disk walk of one confinement; roomy unless a test turns one."""

    output_bytes: int = 1 << 40
    aggregate_descriptors: int = 100_000
    sandbox_bytes: int = 1 << 50
    sandbox_inodes: int = 1 << 40
    walk: DiskWalk = field(default_factory=OsDiskWalk)
    walk_settings: WalkSettings = FAST_WALK


def nobody_protected(_marker: str) -> frozenset[ProcessKey]:
    """Name no other live run."""
    return frozenset()


def real_cleanup(
    table: LibprocProcessTable, signaller: Signaller | None = None
) -> ContainmentCleanup:
    """Build the cleanup rounds over the real census and signaller."""
    return ContainmentCleanup(
        LibprocCensus(table),
        signaller if signaller is not None else OsSignaller(table),
        SystemSleeper(),
        FAST_OBSERVATION,
    )


def real_confinement(  # noqa: PLR0913 - one keyword per thing a process test varies
    *,
    interpreter: str,
    provider: ProviderProfile,
    term_grace_s: float,
    kill_wait_s: float,
    denied: tuple[str, ...] = (),
    table: LibprocProcessTable | None = None,
    signaller: Signaller | None = None,
    protected: Callable[[str], frozenset[ProcessKey]] = nobody_protected,
    knobs: Knobs | None = None,
) -> SeatbeltConfinement:
    """Build the confinement the way the composition root does, with fast observation clocks."""
    real_table = table if table is not None else LibprocProcessTable()
    limits = knobs if knobs is not None else Knobs()
    return SeatbeltConfinement(
        ConfinementConfig(
            denied_reads=denied,
            term_grace_s=term_grace_s,
            kill_wait_s=kill_wait_s,
            scan_s=0.1,
            interpreter=interpreter,
            provider=provider,
            observation=FAST_OBSERVATION,
            file_bytes=ROOMY_FILE_BYTES,
            descriptors=ROOMY_DESCRIPTORS,
            aggregate_descriptors=limits.aggregate_descriptors,
            output_bytes=limits.output_bytes,
            sandbox_bytes=limits.sandbox_bytes,
            sandbox_inodes=limits.sandbox_inodes,
            walk=limits.walk_settings,
        ),
        table=real_table,
        clock=SuspendInclusiveClock(),
        census=LibprocCensus(real_table),
        signaller=signaller if signaller is not None else OsSignaller(real_table),
        sleeper=SystemSleeper(),
        disk_walk=limits.walk,
        protected=protected,
    )
