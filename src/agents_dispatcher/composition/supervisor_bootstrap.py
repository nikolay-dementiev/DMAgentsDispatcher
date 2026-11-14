"""Entry file of the detached supervisor: `python -I <this file> <package root> <arguments>`.

`-I` removes the current directory and the environment from the import path, so the package
root arrives as the first argument, chosen by the launcher from the copy that launched it. The
supervisor therefore always runs the same package copy as the command that started it, also
from a tree that is not installed. The path is data on `argv`; it is never part of any code.
"""

import sys


def main(argv: list[str]) -> int:
    """Put the launcher's package root first on the import path, then hand over."""
    if len(argv) < 2:  # noqa: PLR2004 - the root plus at least one argument
        sys.stderr.write("usage: supervisor_bootstrap.py <package root> <arguments>\n")
        return 2
    sys.path.insert(0, argv[1])
    from agents_dispatcher.composition.supervisor import run_supervisor  # noqa: PLC0415

    return run_supervisor(argv[2:], sys.stdin)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
