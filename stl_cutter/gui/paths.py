"""Sökvägar för inställningar och logg, enligt XDG på Linux."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "stl-cutter"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / APP_NAME


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def settings_file() -> Path:
    return config_dir() / "settings.json"


def log_file() -> Path:
    return data_dir() / "log.txt"
