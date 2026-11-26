"""Laptop capture agent for the meeting adviser (plan C skeleton)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("recorder")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
