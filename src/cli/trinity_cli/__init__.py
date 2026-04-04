"""Trinity CLI — command-line interface for the Trinity Agent Platform."""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("trinity-cli")
except PackageNotFoundError:
    __version__ = "dev"
