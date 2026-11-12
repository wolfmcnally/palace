"""Palace — a personal memory substrate for agents and applications."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("palace")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = ["__version__"]
