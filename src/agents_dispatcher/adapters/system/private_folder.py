"""Make a folder private to its owner without following a link to somewhere else."""

import os
from pathlib import Path


def ensure_private_folder(path: Path, mode: int, error: type[Exception]) -> None:
    """Create `path` if needed and narrow it to `mode`; raise `error` when it is not a plain folder.

    The last component is opened without following links and the mode is set on that open
    handle, so a link planted at `path` can neither redirect the change nor win a race between a
    check and the change. Links in the parents are the account's own layout and are followed.
    """
    try:
        path.mkdir(mode=mode, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as cause:
        message = (
            f"{path} is not a plain folder (a symbolic link or not a folder): {cause.strerror}"
        )
        raise error(message) from cause
    try:
        os.fchmod(descriptor, mode)  # an existing folder may be wider
    except OSError as cause:
        message = f"{path} cannot be made private: {cause.strerror}"
        raise error(message) from cause
    finally:
        os.close(descriptor)
