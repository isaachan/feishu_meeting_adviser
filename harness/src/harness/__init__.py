"""llm-harness: interactive meeting-caption REPL against llm-gateway."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version('llm-harness')
except PackageNotFoundError:
    __version__ = '0.0.0+unknown'
