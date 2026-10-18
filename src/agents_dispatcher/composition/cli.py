"""Composition root of the agentsd command: the only place that builds concrete objects."""

import sys

from agents_dispatcher.adapters.system.package_version import PackageMetadataVersionSource
from agents_dispatcher.interfaces.cli.app import main as run_cli


def main() -> int:
    """Entry point of the console script."""
    return run_cli(sys.argv[1:], sys.stdout, sys.stderr, PackageMetadataVersionSource())
