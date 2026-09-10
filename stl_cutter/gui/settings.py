"""Sparade inställningar: senaste mapp, vald skrivare och monteringsval.

Filen ligger i ~/.config/stl-cutter/settings.json. En trasig eller saknad fil
ska aldrig hindra programmet från att starta - då används standardvärdena.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .paths import settings_file

log = logging.getLogger(__name__)


@dataclass
class Settings:
    """Allt som sparas mellan körningar."""

    printer: str = "Bambu Lab P1S"
    last_open_dir: str = ""
    last_output_dir: str = ""
    assembly_intent: str = "glue"
    clearance_mm: float = 0.15
    margin_mm: float = 5.0
    show_bed: bool = True
    auto_orient: bool = True
    explode_mm: float = 0.0

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = Path(path or settings_file())
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Kunde inte läsa %s (%s) - använder standardinställningar.", path, exc)
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path | None = None) -> Path:
        path = Path(path or settings_file())
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:  # pragma: no cover - full disk, rättigheter
            log.warning("Kunde inte spara inställningar till %s: %s", path, exc)
        return path
