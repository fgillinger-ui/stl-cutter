"""Flera objekt i samma fil, som ändrar mått tillsammans.

En fil kan innehålla flera separata kroppar som hör ihop mekaniskt: en hylla
och dess bakplatta, en låda och dess lock, en vänster- och en högerdel. Ändrar
man måttet på den ena måste den andra följa med, annars slutar de passa.

Modulen gör två saker:

**Håller isär objekten.** ``mesh_io`` slår ihop kroppar som möts till en enda
solid - det är rätt när två kroppar överlappar, för då är fyra trianglar per
kant ett fel. Men kroppar som ligger *isär* är riktiga separata objekt och ska
förbli det. ``split_parts`` skiljer på fallen.

**Låter dem följa varandra.** Den bärande regeln är att kopplade objekt delar
**tillskottet**, inte måttet. Hyllan är 230 mm och bakplattan 250 mm; ska
hyllan bli 270 ska plattan bli 290, inte 270. Först då sitter styrningarna
kvar mitt för varandra.

Varför tillskottet räcker: ``resize`` lägger materialet symmetriskt kring
modellens mitt och låter varje detalj behålla sitt avstånd till närmaste kant.
Två objekt som får samma tillskott flyttar därför sina detaljer lika långt åt
samma håll, och en laxstjärt på det ena möter fortfarande sitt spår i det
andra.

Ordlista:

``Part``
    Ett objekt i filen: en solid kropp med ett namn.
``ledare``
    Det objekt användaren skriver måttet på. Övriga är följare.
``styrning``
    En detalj som måste möta sin motpart i ett annat objekt - ett spår, en
    laxstjärt, en tapp. ``feature_positions`` hittar dem som svackor i
    tvärsnittsarean.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import trimesh

from . import mesh_io
from . import resize as resize_core
from .progress import report as report_progress
from .resize import AXIS_NAMES, ResizeError, section_polygon

log = logging.getLogger(__name__)

__all__ = [
    "Part",
    "AssemblyResize",
    "PartResize",
    "AssemblyError",
    "split_parts",
    "load_parts",
    "feature_positions",
    "feature_spacing",
    "resize_together",
    "describe_assembly",
]

#: Avstånd mellan provade tvärsnitt när styrningar letas upp, i mm. Finare än
#: så lönar sig inte - ett spår som är smalare än en millimeter är ingen
#: styrning utan en ytdetalj.
FEATURE_STEP_MM = 1.0

#: Hur mycket under mediantvärsnittet arean måste ligga för att räknas som en
#: svacka. Ett spår tar en rejäl tugga ur tvärsnittet; en fasad kant tar
#: någon procent, och den ska inte förväxlas med en styrning.
FEATURE_DIP_FRACTION = 0.9

#: Så nära måste två styrningar ha flyttat sig för att passningen ska anses
#: bevarad, i mm. Ligger under vad en 3D-skrivare kan återge.
ALIGNMENT_TOLERANCE_MM = 0.2


class AssemblyError(ResizeError):
    """Måttändringen gick inte att göra på alla objekt."""


@dataclass
class Part:
    """Ett objekt i filen."""

    name: str
    mesh: trimesh.Trimesh

    @property
    def extents_mm(self) -> np.ndarray:
        return np.asarray(self.mesh.extents, dtype=float)

    def summary(self) -> str:
        x, y, z = self.extents_mm
        return f"{self.name}: {x:.1f} x {y:.1f} x {z:.1f} mm"


@dataclass
class PartResize:
    """Vad som hände med ett objekt."""

    name: str
    from_mm: float
    to_mm: float
    is_leader: bool = False
    #: Styrningarnas lägen i förhållande till objektets egen mitt, före och
    #: efter. Används för att visa att passningen består.
    offsets_before: list[float] = field(default_factory=list)
    offsets_after: list[float] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def delta_mm(self) -> float:
        return float(self.to_mm - self.from_mm)

    @property
    def spacing_before(self) -> float | None:
        return _spacing(self.offsets_before)

    @property
    def spacing_after(self) -> float | None:
        return _spacing(self.offsets_after)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "from_mm": round(self.from_mm, 3),
            "to_mm": round(self.to_mm, 3),
            "delta_mm": round(self.delta_mm, 3),
            "is_leader": bool(self.is_leader),
            "guide_spacing_before_mm": _round_or_none(self.spacing_before),
            "guide_spacing_after_mm": _round_or_none(self.spacing_after),
            "warnings": list(self.warnings),
        }


@dataclass
class AssemblyResize:
    """Alla objekt efter måttändringen, plus rapporten."""

    parts: list[Part]
    axis: int
    delta_mm: float
    entries: list[PartResize] = field(default_factory=list)
    #: Sådant som är värt att veta men inte är fel - framför allt när
    #: passningskontrollen inte gick att köra.
    notes: list[str] = field(default_factory=list)

    @property
    def warnings(self) -> list[str]:
        return [warning for entry in self.entries for warning in entry.warnings]

    def to_dict(self) -> dict:
        return {
            "axis": AXIS_NAMES[self.axis],
            "delta_mm": round(self.delta_mm, 3),
            "parts": [entry.to_dict() for entry in self.entries],
            "warnings": self.warnings,
            "notes": list(self.notes),
        }


def _round_or_none(value: float | None) -> float | None:
    return None if value is None else round(float(value), 3)


def _spacing(offsets: list[float]) -> float | None:
    """Avståndet mellan den yttersta styrningen på var sida."""
    if len(offsets) < 2:
        return None
    return float(max(offsets) - min(offsets))


# --------------------------------------------------------------------------
# Dela upp filen i objekt
# --------------------------------------------------------------------------


def _bodies(loaded) -> list[trimesh.Trimesh]:
    """Varje sammanhängande kropp för sig, oavsett hur filen var uppbyggd."""
    if isinstance(loaded, trimesh.Scene):
        meshes = [g for g in loaded.geometry.values() if isinstance(g, trimesh.Trimesh)]
    elif isinstance(loaded, trimesh.Trimesh):
        meshes = [loaded]
    else:
        meshes = [g for g in loaded if isinstance(g, trimesh.Trimesh)]

    out: list[trimesh.Trimesh] = []
    for mesh in meshes:
        if len(mesh.faces) == 0:
            continue
        # En "kropp" i filen kan i sin tur bestå av flera öar.
        pieces = mesh.split(only_watertight=False)
        out.extend(pieces if len(pieces) else [mesh])
    return out


def split_parts(loaded, names: list[str] | None = None) -> list[Part]:
    """Dela upp inläst geometri i separata objekt.

    Kroppar som överlappar eller möts slås ihop till en solid - det är samma
    reparation som ``mesh_io.merge_bodies`` gör, och den behövs: där två
    kroppar möts delar kanterna fyra trianglar, och slicern kallar det
    non-manifold. Kroppar som ligger *isär* lämnas som skilda objekt.
    """
    bodies = _bodies(loaded)
    if not bodies:
        raise ValueError("Filen innehåller ingen triangelgeometri.")

    parts: list[Part] = []
    for index, group in enumerate(mesh_io.group_touching(bodies)):
        mesh = group[0] if len(group) == 1 else mesh_io.merge_bodies(group)
        name = names[index] if names and index < len(names) else f"Objekt {index + 1}"
        parts.append(Part(name=name, mesh=mesh))
    return parts


def load_parts(path: str | Path, repair: bool = True) -> list[Part]:
    """Läs en fil och behåll dess objekt var för sig."""
    path = Path(path)
    loaded = trimesh.load(str(path), force=None)
    names = None
    if isinstance(loaded, trimesh.Scene):
        names = [str(key) for key in loaded.geometry.keys()]
    parts = split_parts(loaded, names=names)

    if repair:
        for part in parts:
            if not part.mesh.is_watertight or mesh_io.bad_edges(part.mesh) != (0, 0):
                fixed, actions = mesh_io.repair_mesh(part.mesh)
                if actions:
                    log.info("%s: %s", part.name, "; ".join(actions))
                part.mesh = fixed
    return parts


# --------------------------------------------------------------------------
# Styrningar: var sitter det som måste mötas?
# --------------------------------------------------------------------------


def feature_positions(
    mesh: trimesh.Trimesh,
    axis: int,
    step: float = FEATURE_STEP_MM,
    dip_fraction: float = FEATURE_DIP_FRACTION,
) -> list[float]:
    """Styrningarnas mittlägen längs axeln, i modellens egna koordinater.

    Ett spår, en urtagning eller en laxstjärt tar en tugga ur tvärsnittet.
    Genom att mäta tvärsnittsarean längs axeln och leta upp svackorna hittar
    man dem utan att veta något om hur de ser ut.
    """
    low = float(mesh.bounds[0][axis])
    high = float(mesh.bounds[1][axis])
    if high - low < 2 * step:
        return []

    positions = np.arange(low + step / 2.0, high - step / 4.0, step)
    areas = []
    for position in positions:
        geometry = section_polygon(mesh, axis, float(position))
        areas.append(0.0 if geometry is None else float(geometry.area))
    areas = np.asarray(areas, dtype=float)
    if not len(areas) or not np.any(areas > 0):
        return []

    threshold = dip_fraction * float(np.median(areas[areas > 0]))
    below = areas < threshold

    centres: list[float] = []
    start: int | None = None
    for index, flag in enumerate(below):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            centres.append(float((positions[start] + positions[index - 1]) / 2.0))
            start = None
    if start is not None:
        centres.append(float((positions[start] + positions[-1]) / 2.0))

    # Svackor som ligger an mot en ände är modellens egen avsmalning, inte en
    # styrning som ska möta något.
    margin = 2 * step
    return [c for c in centres if low + margin < c < high - margin]


def feature_offsets(mesh: trimesh.Trimesh, axis: int, **kwargs) -> list[float]:
    """Styrningarna mätt från objektets egen mitt.

    Mitten är referensen därför att måttändringen är symmetrisk kring den. Två
    objekt passar ihop så länge deras styrningar ligger lika långt ut.
    """
    centre = float(mesh.bounds[0][axis] + mesh.bounds[1][axis]) / 2.0
    return [position - centre for position in feature_positions(mesh, axis, **kwargs)]


def feature_spacing(mesh: trimesh.Trimesh, axis: int, **kwargs) -> float | None:
    """Avståndet mellan den yttersta styrningen på var sida, eller None."""
    return _spacing(feature_offsets(mesh, axis, **kwargs))


# --------------------------------------------------------------------------
# Måttändring i grupp
# --------------------------------------------------------------------------


def resize_together(
    parts: list[Part],
    axis: int,
    target_mm: float,
    leader: int = 0,
    follow: list[bool] | None = None,
    measure_features: bool = True,
    progress=None,
    **resize_kwargs,
) -> AssemblyResize:
    """Ändra måttet på ett objekt och låt de övriga följa med.

    `target_mm` gäller objektet på plats `leader`. Alla följare får samma
    **tillskott**, inte samma mått - se modulens inledning.

    `follow` kan stänga av enskilda följare; utelämnad följer alla utom
    ledaren. Ett objekt som inte går att ändra stoppar hela operationen, för
    halvvägs är värre än inte alls: då har man två delar som inte passar.
    """
    if not parts:
        raise ValueError("Inga objekt att ändra.")
    if not 0 <= leader < len(parts):
        raise IndexError(f"Objekt {leader} finns inte (filen har {len(parts)}).")
    if axis not in (0, 1, 2):
        raise ValueError(f"Axeln måste vara 0, 1 eller 2, inte {axis!r}.")

    follow = list(follow) if follow is not None else [True] * len(parts)
    follow[leader] = True

    delta = float(target_mm) - float(parts[leader].extents_mm[axis])

    out_parts: list[Part] = []
    entries: list[PartResize] = []
    for index, part in enumerate(parts):
        before = float(part.extents_mm[axis])
        if not follow[index] or abs(delta) < 1e-9:
            out_parts.append(Part(part.name, part.mesh))
            entries.append(
                PartResize(
                    name=part.name,
                    from_mm=before,
                    to_mm=before,
                    is_leader=index == leader,
                )
            )
            continue

        target = float(target_mm) if index == leader else before + delta
        if target <= 0:
            raise AssemblyError(
                "target_too_small",
                f"{part.name} skulle bli {target:.1f} mm på {AXIS_NAMES[axis]} - "
                f"tillskottet {delta:+.1f} mm är större än objektet.",
                suggestion="Välj ett mindre tillskott.",
                part=part.name,
            )

        offsets_before = (
            feature_offsets(part.mesh, axis) if measure_features else []
        )
        report_progress(
            progress, index / max(len(parts), 1), f"Ändrar mått på {part.name} ..."
        )
        try:
            result = resize_core.resize_axis(
                part.mesh, axis, target, progress=None, **resize_kwargs
            )
        except ResizeError as exc:
            raise AssemblyError(
                exc.code,
                f"{part.name}: {exc.message}",
                suggestion=getattr(exc, "suggestion", ""),
                part=part.name,
                **getattr(exc, "details", {}),
            ) from exc

        offsets_after = (
            feature_offsets(result.mesh, axis) if measure_features else []
        )
        entry = PartResize(
            name=part.name,
            from_mm=before,
            to_mm=float(result.mesh.extents[axis]),
            is_leader=index == leader,
            offsets_before=offsets_before,
            offsets_after=offsets_after,
            warnings=list(result.warnings),
        )
        out_parts.append(Part(part.name, result.mesh))
        entries.append(entry)

    notes = _check_alignment(entries)
    return AssemblyResize(
        parts=out_parts, axis=axis, delta_mm=delta, entries=entries, notes=notes
    )


def _check_alignment(entries: list[PartResize]) -> list[str]:
    """Flyttade objektens styrningar lika långt?

    Kontrollen är hängslen och livrem: måttändringen är symmetrisk kring
    mitten, så styrningarna *ska* följa med. Men en modell kan ha ett parti
    som inte går att sträcka där man tror, och då hamnar materialet någon
    annanstans - det märks här och inte först i skrivaren.

    Alla objekt har inte mätbara styrningar. En del möter sin motpart med sina
    egna ändytor - hyllan med stolparna ytterst är ett sådant fall - och dem
    flyttar måttändringen aldrig i förhållande till kanten. Då finns det inget
    att jämföra, och det sägs rakt ut i stället för att tigas ihjäl.
    """
    measured = [
        (entry, entry.spacing_before, entry.spacing_after)
        for entry in entries
        if entry.spacing_before is not None and entry.spacing_after is not None
    ]
    changed = [entry for entry in entries if abs(entry.delta_mm) > 1e-9]
    if len(measured) < 2:
        if len(changed) > 1:
            without = [
                entry.name
                for entry in changed
                if entry.spacing_before is None or entry.spacing_after is None
            ]
            return [
                "Passningen kunde inte kontrolleras automatiskt - "
                f"{_join(without)} har inga mätbara spår. Måttet är ändå "
                "fördelat symmetriskt, så detaljer behåller sitt avstånd till "
                "närmaste kant."
            ]
        return []

    shifts = [(entry, after - before) for entry, before, after in measured]
    reference = shifts[0][1]
    for entry, shift in shifts[1:]:
        if abs(shift - reference) > ALIGNMENT_TOLERANCE_MM:
            entry.warnings.append(
                f"Styrningarna på {entry.name} flyttade isär {shift:+.1f} mm medan "
                f"{shifts[0][0].name} flyttade {reference:+.1f} mm. Kontrollera "
                f"passningen innan du skriver ut."
            )
    return []


def _join(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " och " + names[-1]


def describe_assembly(result: AssemblyResize) -> str:
    """Vad som hände, på svenska."""
    axis_word = {0: "bredden", 1: "djupet", 2: "höjden"}[result.axis]
    lines = [f"Ändrade {axis_word} med {result.delta_mm:+.1f} mm:"]
    for entry in result.entries:
        role = "ledare" if entry.is_leader else "följer med"
        line = f"  {entry.name}: {entry.from_mm:.1f} → {entry.to_mm:.1f} mm ({role})"
        if entry.spacing_before is not None and entry.spacing_after is not None:
            line += (
                f", styrningar {entry.spacing_before:.1f} → {entry.spacing_after:.1f} mm"
            )
        lines.append(line)
    return "\n".join(lines)
