"""Loggning till ~/.local/share/stl-cutter/log.txt.

Full logg med stacktrace går till filen. Användaren ser bara begripliga
meddelanden i statusrutan.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .paths import log_file

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
MAX_BYTES = 1_000_000
BACKUPS = 2


def setup_logging(path: Path | None = None, level: int = logging.INFO) -> Path:
    """Koppla in filloggen. Går filen inte att skriva loggar vi bara till konsolen."""
    path = Path(path or log_file())
    root = logging.getLogger()
    root.setLevel(level)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(FORMAT))
        handler.setLevel(level)
        if not any(
            isinstance(h, RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")) == path.resolve()
            for h in root.handlers
        ):
            root.addHandler(handler)
    except OSError as exc:  # pragma: no cover - rättigheter
        logging.basicConfig(level=level, format=FORMAT)
        logging.getLogger(__name__).warning("Kunde inte öppna loggfilen %s: %s", path, exc)

    return path
