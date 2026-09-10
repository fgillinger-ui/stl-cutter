"""Printerprofiler: byggvolym, marginal och tolerans."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_MARGIN_MM = 5.0
DEFAULT_CLEARANCE_MM = 0.15

#: Profiler som följer med paketet (repots data/-mapp, eller bredvid paketet).
_DATA_CANDIDATES = (
    Path(__file__).resolve().parents[2] / "data" / "printers.json",
    Path(__file__).resolve().parents[1] / "data" / "printers.json",
)
BUILTIN_DATA = next((p for p in _DATA_CANDIDATES if p.exists()), _DATA_CANDIDATES[0])

#: Sista utväg om datafilen saknas (t.ex. vid en ofullständig installation).
FALLBACK_PROFILES = [
    {"name": "Bambu Lab P1S", "bed_x": 256, "bed_y": 256, "bed_z": 256},
    {"name": "Bambu Lab X1C", "bed_x": 256, "bed_y": 256, "bed_z": 256},
    {"name": "Prusa MK4", "bed_x": 250, "bed_y": 210, "bed_z": 220},
    {"name": "Ender 3", "bed_x": 220, "bed_y": 220, "bed_z": 250, "clearance_mm": 0.2},
    {"name": "Custom", "bed_x": 200, "bed_y": 200, "bed_z": 200},
]

#: Användarens egna profiler.
USER_DATA = Path.home() / ".config" / "stl-cutter" / "printers.json"


@dataclass
class PrinterProfile:
    """En 3D-skrivares byggvolym och toleranser (allt i mm)."""

    name: str
    bed_x: float
    bed_y: float
    bed_z: float
    margin_mm: float = DEFAULT_MARGIN_MM
    clearance_mm: float = DEFAULT_CLEARANCE_MM

    @property
    def bed(self) -> tuple[float, float, float]:
        return (self.bed_x, self.bed_y, self.bed_z)

    @property
    def usable(self) -> tuple[float, float, float]:
        """Byggvolym minus marginal på båda sidor om varje axel."""
        return tuple(max(1e-6, float(v) - 2.0 * self.margin_mm) for v in self.bed)

    def fits(self, extents) -> bool:
        """Får en del med givna mått plats i den användbara volymen?"""
        return all(float(e) <= u + 1e-6 for e, u in zip(extents, self.usable))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PrinterProfile":
        return cls(
            name=str(data["name"]),
            bed_x=float(data["bed_x"]),
            bed_y=float(data["bed_y"]),
            bed_z=float(data["bed_z"]),
            margin_mm=float(data.get("margin_mm", DEFAULT_MARGIN_MM)),
            clearance_mm=float(data.get("clearance_mm", DEFAULT_CLEARANCE_MM)),
        )


def _read_file(path: Path) -> list[PrinterProfile]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("printers", raw) if isinstance(raw, dict) else raw
    return [PrinterProfile.from_dict(entry) for entry in entries]


def load_printers(user_path: Path | None = USER_DATA) -> list[PrinterProfile]:
    """Alla profiler: inbyggda först, användarens egna sist (de vinner vid namnkrock)."""
    profiles: dict[str, PrinterProfile] = {}
    builtin = _read_file(BUILTIN_DATA)
    if not builtin:
        builtin = [PrinterProfile.from_dict(entry) for entry in FALLBACK_PROFILES]
    for profile in builtin:
        profiles[profile.name] = profile
    if user_path is not None:
        for profile in _read_file(Path(user_path)):
            profiles[profile.name] = profile
    return list(profiles.values())


def get_printer(name: str, user_path: Path | None = USER_DATA) -> PrinterProfile:
    """Slå upp en profil på namn. Exakt match först, annars delsträng utan skiftlägeskrav."""
    profiles = load_printers(user_path)
    for profile in profiles:
        if profile.name == name:
            return profile
    needle = name.strip().lower()
    matches = [p for p in profiles if needle in p.name.lower()]
    if len(matches) == 1:
        return matches[0]
    # "Bambu P1S" ska hitta "Bambu Lab P1S": alla ord måste finnas i namnet.
    words = needle.split()
    matches = [p for p in profiles if all(w in p.name.lower() for w in words)]
    if len(matches) == 1:
        return matches[0]
    known = ", ".join(p.name for p in profiles)
    if not matches:
        raise KeyError(f"Okänd skrivare {name!r}. Kända profiler: {known}")
    raise KeyError(f"Flera profiler matchar {name!r}: {', '.join(p.name for p in matches)}")


def save_profile(profile: PrinterProfile, user_path: Path | None = USER_DATA) -> Path:
    """Spara (eller uppdatera) en egen profil i användarens profilfil."""
    path = Path(user_path or USER_DATA)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {p.name: p for p in _read_file(path)}
    existing[profile.name] = profile
    payload = {"printers": [p.to_dict() for p in existing.values()]}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def delete_profile(name: str, user_path: Path | None = USER_DATA) -> bool:
    """Ta bort en egen profil. Inbyggda profiler går inte att ta bort."""
    path = Path(user_path or USER_DATA)
    existing = {p.name: p for p in _read_file(path)}
    if name not in existing:
        return False
    del existing[name]
    payload = {"printers": [p.to_dict() for p in existing.values()]}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return True
