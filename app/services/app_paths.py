"""Cross-platform writable locations for installed and frozen builds."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


APP_DIR_NAME = "LegadoBookSourceCheck"


@dataclass(frozen=True)
class AppPaths:
    data: Path
    cache: Path


def default_app_paths(*, platform: str | None = None,
                      environ: Mapping[str, str] | None = None,
                      home: str | Path | None = None) -> AppPaths:
    """Resolve writable OS-standard roots without using the executable path."""
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    home_path = Path(home) if home is not None else Path.home()
    data_override = environ.get("LEGADO_BOOKSOURCE_DATA_DIR")
    cache_override = environ.get("LEGADO_BOOKSOURCE_CACHE_DIR")

    if data_override:
        data = Path(data_override).expanduser()
    elif platform.startswith("win"):
        data = Path(environ.get("LOCALAPPDATA") or
                    home_path / "AppData" / "Local") / APP_DIR_NAME
    elif platform == "darwin":
        data = home_path / "Library" / "Application Support" / APP_DIR_NAME
    else:
        data = Path(environ.get("XDG_DATA_HOME") or
                    home_path / ".local" / "share") / APP_DIR_NAME

    if cache_override:
        cache = Path(cache_override).expanduser()
    elif platform.startswith("win"):
        cache = data / "cache"
    elif platform == "darwin":
        cache = home_path / "Library" / "Caches" / APP_DIR_NAME
    else:
        cache = Path(environ.get("XDG_CACHE_HOME") or
                     home_path / ".cache") / APP_DIR_NAME
    return AppPaths(data=data, cache=cache)
