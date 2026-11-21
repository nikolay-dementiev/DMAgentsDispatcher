"""Builds a Seatbelt confinement over the real census and signaller, with fast test clocks."""

from collections.abc import Callable

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
from agents_dispatcher.application.ports import Signaller
from agents_dispatcher.application.settings import ObservationSettings
from agents_dispatcher.domain.process import ProcessKey

# Looks every 0.1 s and path looks every 0.2 s keep the real-process tests quick.
FAST_OBSERVATION = ObservationSettings(0.1, 2.0, 0.2, 5.0, 2, 8)


# Generous: the tests that exercise these limits set their own.
ROOMY_FILE_BYTES = 1 << 40
ROOMY_DESCRIPTORS = 1024
ROOMY_AGGREGATE_DESCRIPTORS = 100_000
ROOMY_OUTPUT_BYTES = 1 << 40


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
    output_bytes: int = ROOMY_OUTPUT_BYTES,
    aggregate_descriptors: int = ROOMY_AGGREGATE_DESCRIPTORS,
) -> SeatbeltConfinement:
    """Build the confinement the way the composition root does, with fast observation clocks."""
    real_table = table if table is not None else LibprocProcessTable()
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
            aggregate_descriptors=aggregate_descriptors,
            output_bytes=output_bytes,
        ),
        table=real_table,
        clock=SuspendInclusiveClock(),
        census=LibprocCensus(real_table),
        signaller=signaller if signaller is not None else OsSignaller(real_table),
        sleeper=SystemSleeper(),
        protected=protected,
    )
