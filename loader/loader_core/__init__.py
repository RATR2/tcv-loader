from __future__ import annotations

import sys
from pathlib import Path


def repo_root() -> Path:
    """Directory holding modpacks/ and (unfrozen) loader/, or ui/ (frozen): the
    build's real, editable footprint, as opposed to PyInstaller's bundled internals."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def bundled_resource(relative: str) -> Path:
    """Path to a file PyInstaller's --add-data bundled alongside this module,
    or the real source file when running unfrozen."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    else:
        base = Path(__file__).resolve().parent
    return base / relative
