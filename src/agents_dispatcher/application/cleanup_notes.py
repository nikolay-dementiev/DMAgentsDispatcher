"""Report notes that every path closing a run shares, so no closer words them differently."""


def unreadable_notes(names: tuple[str, ...]) -> tuple[str, ...]:
    """Return one note per process whose paths could not be read after the worker started."""
    return tuple(
        f"paths of {name} could not be read; it started after the worker" for name in names
    )


def held_notes(names: tuple[str, ...]) -> tuple[str, ...]:
    """Return one note per process spared because it started outside the run's life."""
    return tuple(
        f"{name} holds a path in the run folder and started outside the run's life "
        "(before the run or after its last sign of life); "
        "it was not signalled and the run stays held"
        for name in names
    )
