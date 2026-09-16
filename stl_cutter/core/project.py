"""Spara ett helt arbete och ta upp det igen.

Ett verkligt jobb är sällan klart på en gång: måtten ändras, snitten flyttas,
fogtypen byts, lasten anges, och nästa dag vill man fortsätta i stället för
att göra om alltihop. Den här modulen skriver allt det till en fil.

**Modellen ligger med i filen.** Det är hela poängen. Ett projekt som bara
pekar på en STL-fil hade gått sönder på tre sätt: filen flyttas, filen ändras
i CAD, eller - vanligast - så är modellen i programmet inte längre den som
ligger på disken, för den har måttändrats. Det som sparas ska vara det som
kommer tillbaka, exakt. En projektfil blir därmed lika stor som modellen plus
någon kilobyte, och det är ett pris värt att betala.

Filen är en vanlig zip med två saker i:

``project.json``
    Inställningar och snitt. Läsbart, och går att titta i med vilken
    zip-läsare som helst om något ser konstigt ut.
``model.stl``
    Modellen som den såg ut när projektet sparades.

Sökvägen till originalfilen sparas också, men bara som upplysning - den
används aldrig vid inläsning.

**Versionen.** `project.json` har ett `version`-fält. En nyare fil än
programmet känner till läses inte in: bättre ett tydligt fel än att tyst
tappa hälften av snitten.
"""

from __future__ import annotations

import json
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import trimesh

from .load import LoadCase
from .printers import PrinterProfile

log = logging.getLogger(__name__)

__all__ = [
    "PROJECT_VERSION",
    "PROJECT_SUFFIX",
    "ProjectError",
    "ProjectCut",
    "Project",
    "save_project",
    "load_project",
]

#: Formatets version. Höjs bara när en äldre läsare inte längre kan förstå
#: filen - nya fält med rimliga standardvärden räknas inte som en ny version.
PROJECT_VERSION = 1

#: Filändelsen. Egen ändelse så att den inte förväxlas med en modell.
PROJECT_SUFFIX = ".stlcut"

_SETTINGS_NAME = "project.json"
_MODEL_NAME = "model.stl"


class ProjectError(ValueError):
    """Projektet gick inte att läsa, med ett skäl som går att åtgärda."""


@dataclass
class ProjectCut:
    """Ett snitt som användaren bestämt, med sitt fogval."""

    axis: int
    position_mm: float
    normal: tuple[float, float, float] = (0.0, 0.0, 0.0)
    joint_type: str = ""
    #: Fogens parametrar, till exempel stoppkantens höjd i en laxstjärt.
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "axis": int(self.axis),
            "position_mm": round(float(self.position_mm), 4),
            "normal": [round(float(v), 6) for v in self.normal],
            "joint_type": self.joint_type,
            "params": self.params,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectCut":
        normal = data.get("normal") or [0.0, 0.0, 0.0]
        return cls(
            axis=int(data["axis"]),
            position_mm=float(data["position_mm"]),
            normal=tuple(float(v) for v in normal),
            joint_type=str(data.get("joint_type", "")),
            params=dict(data.get("params") or {}),
        )

    @property
    def has_normal(self) -> bool:
        """Har snittet en egen normal, alltså en lutning att återskapa?"""
        return any(abs(float(v)) > 1e-9 for v in self.normal)


@dataclass
class Project:
    """Ett sparat arbete."""

    mesh: trimesh.Trimesh
    printer: PrinterProfile
    source: str = ""
    assembly_intent: str = "glue"
    load: LoadCase | None = None
    cuts: list[ProjectCut] = field(default_factory=list)
    lay_flat: bool = True
    split_bodies: bool = True
    output_dir: str = ""
    base_profile: str = ""
    saved: str = ""
    #: Fritext från användaren. Tomt så länge inget skrivits.
    note: str = ""

    def settings_dict(self) -> dict:
        """Allt utom modellen, som det skrivs i `project.json`."""
        return {
            "version": PROJECT_VERSION,
            "saved": self.saved or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": self.source,
            "note": self.note,
            "printer": self.printer.to_dict(),
            "assembly_intent": self.assembly_intent,
            "load": self.load.to_dict() if self.load is not None else None,
            "cuts": [cut.to_dict() for cut in self.cuts],
            "export": {
                "lay_flat": bool(self.lay_flat),
                "split_bodies": bool(self.split_bodies),
                "output_dir": self.output_dir,
            },
            "base_profile": self.base_profile,
        }

    def describe(self) -> str:
        """Projektet i klartext."""
        x, y, z = self.mesh.extents
        lines = [
            f"Modell: {x:.1f} x {y:.1f} x {z:.1f} mm"
            + (f" (från {Path(self.source).name})" if self.source else ""),
            f"Skrivare: {self.printer.name}",
            f"Montering: {self.assembly_intent}",
        ]
        if self.load is not None and self.load.active:
            from .load import describe_load_case

            lines.append(f"Last: {describe_load_case(self.load)}")
        if self.cuts:
            lines.append(f"Snitt: {len(self.cuts)} st")
            for index, cut in enumerate(self.cuts, start=1):
                name = "XYZ"[cut.axis]
                joint = cut.joint_type or "ingen fog"
                tilt = ", vinklat" if cut.has_normal else ""
                lines.append(f"  {index}. {name} = {cut.position_mm:.1f} mm -> {joint}{tilt}")
        else:
            lines.append("Snitt: inga - programmet får räkna ut dem")
        if self.saved:
            lines.append(f"Sparat: {self.saved}")
        return "\n".join(lines)


def _load_case_from_dict(data: dict | None) -> LoadCase | None:
    if not data:
        return None
    axis = data.get("axis", "Y")
    return LoadCase(
        mass_kg=float(data.get("mass_kg", 0.0)),
        support=str(data.get("support", "free")),
        axis="XYZ".index(axis) if isinstance(axis, str) else int(axis),
        fixed_at_low=bool(data.get("fixed_at_low", True)),
        guessed_from=str(data.get("guessed_from", "")),
    )


def save_project(project: Project, path: str | Path) -> Path:
    """Skriv projektet till `path` och returnera den skrivna sökvägen."""
    path = Path(path)
    if path.suffix.lower() != PROJECT_SUFFIX:
        path = path.with_suffix(PROJECT_SUFFIX)
    path.parent.mkdir(parents=True, exist_ok=True)

    settings = project.settings_dict()
    # Modellen skrivs som binär STL - kompakt, och det formatet kan alla läsa
    # om filen någon gång behöver plockas isär för hand.
    model = trimesh.exchange.stl.export_stl(project.mesh)

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            _SETTINGS_NAME, json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
        )
        archive.writestr(_MODEL_NAME, model)
    log.info("Sparade projektet till %s", path)
    return path


def load_project(path: str | Path) -> Project:
    """Läs ett projekt. Kastar `ProjectError` med ett begripligt skäl."""
    path = Path(path)
    if not path.exists():
        raise ProjectError(f"Hittar ingen projektfil på {path}.")
    if not zipfile.is_zipfile(path):
        raise ProjectError(
            f"{path.name} är inget projekt. En projektfil slutar på "
            f"{PROJECT_SUFFIX} och innehåller både modellen och inställningarna."
        )

    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            missing = {_SETTINGS_NAME, _MODEL_NAME} - names
            if missing:
                raise ProjectError(
                    f"{path.name} saknar {', '.join(sorted(missing))} och går "
                    "inte att öppna som projekt."
                )
            settings = json.loads(archive.read(_SETTINGS_NAME).decode("utf-8"))
            with archive.open(_MODEL_NAME) as handle:
                mesh = trimesh.load(handle, file_type="stl")
    except ProjectError:
        raise
    except (OSError, ValueError, KeyError) as error:
        raise ProjectError(f"{path.name} gick inte att läsa: {error}") from error

    version = int(settings.get("version", 0))
    if version > PROJECT_VERSION:
        raise ProjectError(
            f"{path.name} är sparad av en nyare version av programmet "
            f"(format {version}, den här förstår {PROJECT_VERSION}). "
            "Uppdatera STL Cutter så går den att öppna."
        )

    export = settings.get("export") or {}
    return Project(
        mesh=mesh,
        printer=PrinterProfile.from_dict(settings["printer"]),
        source=str(settings.get("source", "")),
        assembly_intent=str(settings.get("assembly_intent", "glue")),
        load=_load_case_from_dict(settings.get("load")),
        cuts=[ProjectCut.from_dict(item) for item in settings.get("cuts", [])],
        lay_flat=bool(export.get("lay_flat", True)),
        split_bodies=bool(export.get("split_bodies", True)),
        output_dir=str(export.get("output_dir", "")),
        base_profile=str(settings.get("base_profile", "")),
        saved=str(settings.get("saved", "")),
        note=str(settings.get("note", "")),
    )
