"""Reads the installed version of the distribution from package metadata."""

from importlib.metadata import PackageNotFoundError, version

DISTRIBUTION = "agents-dispatcher"


class PackageMetadataVersionSource:
    """A version source backed by the installed distribution metadata."""

    def installed_version(self) -> str | None:
        """Return the installed version, or None when the distribution is not installed."""
        try:
            return version(DISTRIBUTION)
        except PackageNotFoundError:
            return None
