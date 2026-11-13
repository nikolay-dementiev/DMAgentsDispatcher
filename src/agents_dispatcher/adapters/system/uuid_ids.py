"""Run identifiers from random UUIDv4 values."""

import uuid

from agents_dispatcher.domain.run import RunId


class UuidRunIds:
    """An `IdGenerator` that draws a fresh random UUIDv4 for every run."""

    def new_run_id(self) -> RunId:
        """Return a canonical lowercase UUIDv4."""
        return RunId(str(uuid.uuid4()))
