"""gui: organizer GUI client (recorder 的图形分发形态).

DESIGN: gui/DESIGN.md. Core logic modules (config/api/login/sessions)
are Qt-free and headless-testable; app.py + ui/ carry the PySide6 glue.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version('gui')
except PackageNotFoundError:
    __version__ = '0.0.0+unknown'
