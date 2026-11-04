"""Local bird field-journal plates for Pimoroni Inky displays."""

from importlib import metadata

PACKAGE_NAME = "inky-bird"
LEGACY_PACKAGE_NAME = "inky-bird-frame"


def application_version() -> str:
    """Return installed package metadata without failing in a source checkout."""
    for name in (PACKAGE_NAME, LEGACY_PACKAGE_NAME):
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return "0+unknown"


__version__ = application_version()
