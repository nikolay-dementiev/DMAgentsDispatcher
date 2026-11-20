"""The real console entry point, `agentsd mcp serve`, run against a temporary account.

Only the lookup of the account's home directory is replaced, so the settings, the run store and
the workers of the test live under a temporary folder; everything else is the shipped composition
root, started as `python -m tests.support.mcp_real_child`.
"""

import os
import sys

from agents_dispatcher.composition import cli, mcp


def main() -> int:
    """Serve the temporary account named by `MCP_REAL_HOME` over standard input and output."""
    home = os.environ["MCP_REAL_HOME"]
    setattr(cli, "account_home_dir", lambda: home)  # noqa: B010 - replaces a module-level lookup
    setattr(mcp, "account_home_dir", lambda: home)  # noqa: B010
    sys.argv = ["agentsd", "mcp", "serve"]
    return cli.main()


if __name__ == "__main__":
    sys.exit(main())
