"""Report notes that every path closing a run shares, so no closer words them differently."""


def unreadable_notes(names: tuple[str, ...]) -> tuple[str, ...]:
    """Return one note per process whose paths could not be read after the worker started."""
    return tuple(
        f"paths of {name} could not be read; it started after the worker" for name in names
    )
