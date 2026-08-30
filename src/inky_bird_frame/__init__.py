"""Local bird field-journal plates for Pimoroni Inky displays."""

from importlib import metadata

PACKAGE_NAME = "inky-bird-frame"


def application_version() -> str:
    """Return installed package metadata without failing in a source checkout."""
    try:
        return metadata.version(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return "0+unknown"


__version__ = application_version()
