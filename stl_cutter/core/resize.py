"""Måttändring som bevarar godstjocklek, hål och detaljer (fas 3B).

Rak skalning duger inte när ett mått ska ändras: den gör runda hål ovala,
väggar tjockare och gängor obrukbara. I stället letar den här modulen upp de
partier där modellens tvärsnitt är **konstant** längs en axel - ett prismatiskt
parti - och skjuter in eller tar bort material just där. Allt annat lämnas
orört, hörnradier och hål inkluderade.

Ordning i pipelinen: ``ladda -> resize -> planera snitt -> kapa -> exportera``.

Ordlista:

``PrismaticSpan``
    Ett intervall längs en axel där alla tvärsnitt är lika. Att förlänga
    modellen betyder att materialet i ett sådant parti blir längre.
``signatur``
    Beskrivningen av ett tvärsnitt: area, omkrets, antal konturer, bounding box
    och en normaliserad form-hash. Två tvärsnitt räknas som lika när
    signaturerna stämmer *och* polygonerna täcker varandra (symmetrisk
    differens under toleransen).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import shapely
import trimesh
from shapely.geometry import Polygon

from .joints.base import engine_name, union
from .progress import report as report_progress

log = logging.getLogger(__name__)

__all__ = [
    "AXIS_NAMES",
    "AxisResize",
    "PrismaticSpan",
    "ResizeError",
    "ResizeReport",
    "ResizeResult",
    "SectionSignature",
    "ValidationError",
    "find_prismatic_spans",
    "resize",
    "resize_axis",
    "write_resize_report",
]

AXIS_NAMES = ("X", "Y", "Z")

#: Standardavstånd mellan provade tvärsnitt, i mm.
DEFAULT_STEP_MM = 1.0

#: Relativ tolerans när två tvärsnitt jämförs.
DEFAULT_TOL = 0.02

#: Partier kortare än så här är inte värda att räkna som prismatiska - de ger
#: ingen plats för vare sig snitt eller mellanstycke.
MIN_SPAN_LENGTH_MM = 3.0

#: Material som måste bli kvar av ett parti när det kortas av. Snittplanen
#: läggs symmetriskt kring partiets mitt, så det blir 1 mm i var ände.
MIN_REMAINING_MM = 2.0

#: Mellanstycket görs så här mycket längre än `delta` och skjuts in halva
#: överlappet i varje halva. Koplanära ytor är den vanligaste orsaken till att
#: en boolean går sönder; överlappet ser till att de aldrig uppstår.
OVERLAP_MM = 0.05

#: Bounding box efter måttändringen får avvika så här mycket från målet.
BBOX_TOLERANCE_MM = 0.1

#: Volymförändringen ska motsvara tvärsnittsarea x delta inom den här andelen.
VOLUME_TOLERANCE = 0.01

#: Golv för volymkontrollen, så att flyttalsbrus i en stor mesh inte fäller
#: en i övrigt korrekt måttändring.
VOLUME_ABSOLUTE_FLOOR_MM3 = 1.0

#: Så många gånger provas ett förskjutet snittplan när en boolean misslyckas.
MAX_BOOLEAN_ATTEMPTS = 3

#: Hur långt snittplanet flyttas mellan försöken, i mm.
BOOLEAN_RETRY_SHIFT_MM = 1.0

#: Hur långt konturen får flytta sig i sidled och ändå räknas som oförändrad,
#: i mm. Kriteriet är absolut och inte relativt med flit: en relativ
#: areatolerans släpper igenom ett långsamt krympande hål i en stor platta -
#: arean ändras med någon promille medan hålväggen flyttar sig en halv
#: millimeter. Gränsen ligger under vad en 3D-skrivare kan återge.
SECTION_DEVIATION_MM = 0.05

#: Decimaler i form-hashen. Grövre än toleransen på syftet: hashen är en
#: snabb gallring, den exakta jämförelsen görs på polygonerna.
HASH_DECIMALS = 2


# --------------------------------------------------------------------------
# Fel
# --------------------------------------------------------------------------


class ResizeError(RuntimeError):
    """Måttändringen gick inte att göra. Bär med sig en förklaring på svenska.

    `code` är maskinläsbart, `message` är till användaren och `suggestion`
    säger vad hen kan göra i stället.
    """

    def __init__(self, code: str, message: str, suggestion: str = "", **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion
        self.details = details

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "suggestion": self.suggestion,
            "details": self.details,
        }

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.message} {self.suggestion}".strip()


class ValidationError(ResizeError):
    """Resultatet klarade inte kontrollerna och levereras därför inte."""


# --------------------------------------------------------------------------
# Tvärsnitt och signaturer
# --------------------------------------------------------------------------


def axis_frame(axis: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(u, v, n) för en axel. Cykliskt vald, så systemet är högerhänt."""
    axis = int(axis)
    if axis not in (0, 1, 2):
        raise ValueError(f"Axeln måste vara 0, 1 eller 2 - fick {axis!r}.")
    basis = np.eye(3)
    n = basis[axis]
    u = basis[(axis + 1) % 3]
    v = basis[(axis + 2) % 3]
    return u, v, n


def _to_2d_matrix(axis: int, position: float) -> np.ndarray:
    """Transform som plattar ut ett tvärsnitt vid `position` till z = 0.

    x och y är **absoluta** projektioner på u och v, lika för alla tvärsnitt
    längs axeln. Det är hela poängen: två polygoner går bara att jämföra om de
    ligger i samma koordinatsystem, och ett prismatiskt parti kräver att
    tvärsnittet ligger på samma ställe, inte bara har samma form.
    """
    u, v, n = axis_frame(axis)
    matrix = np.eye(4)
    matrix[0, :3] = u
    matrix[1, :3] = v
    matrix[2, :3] = n
    matrix[2, 3] = -float(position)
    return matrix


def section_polygon(mesh: trimesh.Trimesh, axis: int, position: float):
    """Tvärsnittet vid `position` som shapely-geometri, eller None om tomt."""
    _, _, normal = axis_frame(axis)
    origin = normal * float(position)
    try:
        section = mesh.section(plane_origin=origin, plane_normal=normal)
    except Exception as exc:  # pragma: no cover - trimesh kastar sällan här
        log.debug("Tvärsnitt vid %.3f misslyckades: %s", position, exc)
        return None
    if section is None:
        return None
    try:
        planar, _ = section.to_2D(to_2D=_to_2d_matrix(axis, position))
    except Exception as exc:
        log.debug("Kunde inte platta ut tvärsnitt vid %.3f: %s", position, exc)
        return None
    polygons = [p for p in planar.polygons_full if p.area > 1e-9]
    if not polygons:
        return None
    merged = shapely.union_all(polygons)
    if not merged.is_valid:
        merged = merged.buffer(0)
    return merged if not merged.is_empty else None


def _shape_hash(geometry) -> tuple:
    """Normaliserad form-hash: avrundade, sorterade konturkoordinater.

    Rundningen gör hashen okänslig för hur trianglarna råkar ligga, och
    sorteringen för var konturen börjar. Den används som snabb gallring innan
    den dyrare polygonjämförelsen.
    """
    if geometry is None or geometry.is_empty:
        return ()
    rings: list[tuple] = []
    for polygon in _polygons(geometry):
        for ring in [polygon.exterior, *polygon.interiors]:
            coords = np.asarray(ring.coords[:-1], dtype=float)
            if len(coords) == 0:
                continue
            rounded = np.round(coords, HASH_DECIMALS) + 0.0  # -0.0 -> 0.0
            order = np.lexsort((rounded[:, 1], rounded[:, 0]))
            rings.append(tuple(map(tuple, rounded[order])))
    return tuple(sorted(rings))


def _polygons(geometry) -> list[Polygon]:
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    return [g for g in getattr(geometry, "geoms", []) if isinstance(g, Polygon)]


@dataclass
class SectionSignature:
    """Beskrivning av ett tvärsnitt, tillräcklig för att jämföra två stycken."""

    position: float
    area: float
    perimeter: float
    contour_count: int
    bbox: tuple[float, float, float, float]
    shape_hash: tuple = field(repr=False, default=())
    polygon: object = field(repr=False, default=None)

    @property
    def empty(self) -> bool:
        return self.polygon is None or self.area <= 0.0

    def matches(
        self,
        other: "SectionSignature",
        tol: float = DEFAULT_TOL,
        max_deviation_mm: float = SECTION_DEVIATION_MM,
    ) -> bool:
        """Är de två tvärsnitten samma tvärsnitt?

        `tol` är den relativa toleransen för de skalära måtten (area, omkrets,
        bounding box) och `max_deviation_mm` hur långt konturen får ha flyttat
        sig i sidled. Båda måste vara uppfyllda.
        """
        if self.empty or other.empty:
            return False
        if self.contour_count != other.contour_count:
            return False
        if not _close(self.area, other.area, tol):
            return False
        if not _close(self.perimeter, other.perimeter, tol):
            return False
        scale = max(self.bbox[2] - self.bbox[0], self.bbox[3] - self.bbox[1], 1e-6)
        for mine, theirs in zip(self.bbox, other.bbox):
            if abs(mine - theirs) > tol * scale:
                return False
        if self.shape_hash and self.shape_hash == other.shape_hash:
            return True
        # Den avgörande kontrollen: hur mycket av ytorna ligger *inte* på
        # varandra? Formhashen kan skilja sig bara för att trianglarna råkar
        # ligga olika, men två prismatiska tvärsnitt täcker alltid varandra.
        # Ytan mellan konturerna delad med omkretsen är hur långt konturen har
        # flyttat sig i genomsnitt - måttet som avgör om ett hål har börjat
        # smalna av.
        difference = self.polygon.symmetric_difference(other.polygon)
        if difference.area > tol * max(self.area, other.area):
            return False
        perimeter = max(self.perimeter, other.perimeter, 1e-6)
        return difference.area / perimeter <= max_deviation_mm

    def to_dict(self) -> dict:
        return {
            "position_mm": round(self.position, 4),
            "area_mm2": round(self.area, 4),
            "perimeter_mm": round(self.perimeter, 4),
            "contour_count": self.contour_count,
            "bbox_mm": [round(value, 4) for value in self.bbox],
        }


def _close(a: float, b: float, tol: float) -> bool:
    scale = max(abs(a), abs(b), 1e-9)
    return abs(a - b) <= tol * scale


def section_signature(mesh: trimesh.Trimesh, axis: int, position: float) -> SectionSignature:
    """Signaturen för ett enda tvärsnitt."""
    geometry = section_polygon(mesh, axis, position)
    if geometry is None:
        return SectionSignature(
            position=float(position),
            area=0.0,
            perimeter=0.0,
            contour_count=0,
            bbox=(0.0, 0.0, 0.0, 0.0),
        )
    polygons = _polygons(geometry)
    return SectionSignature(
        position=float(position),
        area=float(geometry.area),
        perimeter=float(sum(p.exterior.length + sum(i.length for i in p.interiors) for p in polygons)),
        contour_count=len(polygons),
        bbox=tuple(float(value) for value in geometry.bounds),
        shape_hash=_shape_hash(geometry),
        polygon=geometry,
    )


def sample_signatures(
    mesh: trimesh.Trimesh,
    axis: int,
    step: float = DEFAULT_STEP_MM,
    progress=None,
) -> list[SectionSignature]:
    """Signaturer för tvärsnitt var `step` mm längs axeln."""
    low, high = (float(mesh.bounds[0][axis]), float(mesh.bounds[1][axis]))
    step = max(float(step), 1e-3)
    positions = np.arange(low + step / 2.0, high, step)
    if len(positions) == 0:
        positions = np.array([(low + high) / 2.0])
    signatures: list[SectionSignature] = []
    for index, position in enumerate(positions):
        signatures.append(section_signature(mesh, axis, float(position)))
        if progress is not None and index % 16 == 0:
            report_progress(
                progress,
                index / max(len(positions), 1),
                f"Söker konstanta tvärsnitt längs {AXIS_NAMES[axis]}",
            )
    return signatures


# --------------------------------------------------------------------------
# Prismatiska partier
# --------------------------------------------------------------------------


@dataclass
class PrismaticSpan:
    """Ett parti där tvärsnittet är konstant längs axeln."""

    axis: int
    start: float
    end: float
    section_area: float
    section_polygon: object = field(repr=False, default=None)
    sample_count: int = 0

    @property
    def length(self) -> float:
        return float(self.end - self.start)

    @property
    def middle(self) -> float:
        return float((self.start + self.end) / 2.0)

    @property
    def capacity_mm(self) -> float:
        """Hur mycket som går att ta bort här och lämna material kvar."""
        return max(0.0, self.length - MIN_REMAINING_MM)

    def contains(self, position: float, margin: float = 0.0) -> bool:
        return self.start + margin <= position <= self.end - margin

    def describe(self) -> str:
        return (
            f"{AXIS_NAMES[self.axis]} {self.start:.1f} … {self.end:.1f} mm "
            f"({self.length:.1f} mm långt, tvärsnitt {self.section_area:.0f} mm²)"
        )

    def to_dict(self) -> dict:
        return {
            "axis": AXIS_NAMES[self.axis],
            "start_mm": round(self.start, 3),
            "end_mm": round(self.end, 3),
            "length_mm": round(self.length, 3),
            "section_area_mm2": round(self.section_area, 3),
            "sample_count": self.sample_count,
        }


def find_prismatic_spans(
    mesh: trimesh.Trimesh,
    axis: int,
    step: float = DEFAULT_STEP_MM,
    tol: float = DEFAULT_TOL,
    min_length_mm: float = MIN_SPAN_LENGTH_MM,
    max_deviation_mm: float = SECTION_DEVIATION_MM,
    progress=None,
) -> list[PrismaticSpan]:
    """Hitta alla partier med konstant tvärsnitt längs `axis`.

    Tvärsnitten samplas var `step` mm och intilliggande tvärsnitt vars
    signaturer är lika inom `tol` grupperas ihop. Varje tvärsnitt jämförs med
    gruppens **första**, inte med sitt närmaste grannsnitt - annars kan en
    långsam avsmalning glida igenom en signatur i taget och ett koniskt parti
    räknas som prismatiskt.

    Returneras sorterade på längd, längst först. Partier kortare än
    `min_length_mm` kastas.
    """
    signatures = sample_signatures(mesh, axis, step=step, progress=progress)

    spans: list[PrismaticSpan] = []
    group: list[SectionSignature] = []

    def close_group() -> None:
        if len(group) < 2:
            group.clear()
            return
        first = group[0]
        span = PrismaticSpan(
            axis=int(axis),
            start=group[0].position,
            end=group[-1].position,
            section_area=first.area,
            section_polygon=first.polygon,
            sample_count=len(group),
        )
        if span.length >= min_length_mm:
            spans.append(span)
        group.clear()

    for signature in signatures:
        if signature.empty:
            close_group()
            continue
        if group and signature.matches(
            group[0], tol=tol, max_deviation_mm=max_deviation_mm
        ):
            group.append(signature)
        else:
            close_group()
            group.append(signature)
    close_group()

    spans.sort(key=lambda span: span.length, reverse=True)
    return spans


def describe_spans(mesh: trimesh.Trimesh, axis: int, spans: Sequence[PrismaticSpan]) -> str:
    """Läsbar sammanfattning till `analyze-spans` och GUI:t."""
    size = float(mesh.extents[axis])
    lines = [
        f"Axel {AXIS_NAMES[axis]}: modellen är {size:.1f} mm "
        f"({mesh.bounds[0][axis]:.1f} … {mesh.bounds[1][axis]:.1f} mm)."
    ]
    if not spans:
        lines.append("  Inget parti med konstant tvärsnitt - måttet går inte att bevara.")
        return "\n".join(lines)
    total = sum(span.length for span in spans)
    lines.append(f"  {len(spans)} parti(er) att sträcka i, sammanlagt {total:.1f} mm:")
    for index, span in enumerate(spans, start=1):
        lines.append(f"  {index:2d}. {span.describe()}")
    lines.append(
        f"  Att korta av går som mest {sum(s.capacity_mm for s in spans):.1f} mm "
        "totalt (2 mm måste bli kvar i varje parti)."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Själva måttändringen
# --------------------------------------------------------------------------


@dataclass
class _Operation:
    """En utförd in- eller urskjutning, uttryckt i **originalets** koordinater."""

    span_start: float
    span_end: float
    cut_low: float
    cut_high: float
    delta: float
    section_area: float

    def to_dict(self) -> dict:
        return {
            "span_start_mm": round(self.span_start, 3),
            "span_end_mm": round(self.span_end, 3),
            "cut_low_mm": round(self.cut_low, 3),
            "cut_high_mm": round(self.cut_high, 3),
            "delta_mm": round(self.delta, 3),
            "section_area_mm2": round(self.section_area, 3),
        }


@dataclass
class AxisResize:
    """Vad som hände på en axel."""

    axis: int
    from_mm: float
    to_mm: float
    mode: str
    span_selection: str
    spans: list[PrismaticSpan] = field(default_factory=list)
    chosen: list[tuple[PrismaticSpan, float]] = field(default_factory=list)
    expected_volume_change_mm3: float = 0.0
    actual_volume_change_mm3: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def delta_mm(self) -> float:
        return float(self.to_mm - self.from_mm)

    def to_dict(self) -> dict:
        return {
            "axis": AXIS_NAMES[self.axis],
            "from_mm": round(self.from_mm, 3),
            "to_mm": round(self.to_mm, 3),
            "delta_mm": round(self.delta_mm, 3),
            "mode": self.mode,
            "span_selection": self.span_selection,
            "spans_found": [span.to_dict() for span in self.spans],
            "spans_used": [
                {**span.to_dict(), "applied_delta_mm": round(delta, 3)}
                for span, delta in self.chosen
            ],
            "expected_volume_change_mm3": round(self.expected_volume_change_mm3, 3),
            "actual_volume_change_mm3": round(self.actual_volume_change_mm3, 3),
            "warnings": list(self.warnings),
        }


@dataclass
class ResizeResult:
    """Den ändrade meshen plus rapporten."""

    mesh: trimesh.Trimesh
    axes: list[AxisResize] = field(default_factory=list)
    original_extents_mm: tuple[float, float, float] = (0.0, 0.0, 0.0)
    target_extents_mm: tuple[float | None, float | None, float | None] = (None, None, None)

    @property
    def warnings(self) -> list[str]:
        return [warning for axis in self.axes for warning in axis.warnings]

    @property
    def changed(self) -> bool:
        return any(abs(axis.delta_mm) > 1e-9 for axis in self.axes)

    def to_dict(self) -> dict:
        return {
            "original_extents_mm": [round(v, 3) for v in self.original_extents_mm],
            "target_extents_mm": [
                None if v is None else round(float(v), 3) for v in self.target_extents_mm
            ],
            "result_extents_mm": [round(float(v), 3) for v in self.mesh.extents],
            "axes": [axis.to_dict() for axis in self.axes],
            "warnings": self.warnings,
        }


def _slice_half(mesh: trimesh.Trimesh, axis: int, position: float, keep_above: bool):
    """Ena halvan av ett plansnitt, med lock. None när inget blev kvar."""
    _, _, normal = axis_frame(axis)
    direction = normal if keep_above else -normal
    origin = normal * float(position)
    engine = engine_name()
    try:
        piece = trimesh.intersections.slice_mesh_plane(
            mesh, plane_normal=direction, plane_origin=origin, cap=True, engine=engine
        )
    except Exception as exc:
        log.debug("Snitt vid %.3f misslyckades med %r (%s) - provar utan motor.", position, engine, exc)
        piece = trimesh.intersections.slice_mesh_plane(
            mesh, plane_normal=direction, plane_origin=origin, cap=True
        )
    if piece is None or len(piece.faces) == 0:
        return None
    piece.merge_vertices()
    return piece


def _filler(polygon, axis: int, low: float, high: float) -> trimesh.Trimesh:
    """Mellanstycke: zonens tvärsnitt extruderat mellan två lägen på axeln."""
    height = float(high - low)
    if height <= 0:
        raise ResizeError(
            "internal_filler",
            "Mellanstycket fick noll längd.",
            "Det är ett programfel - rapportera gärna modellen.",
        )
    parts = []
    for piece in _polygons(polygon):
        solid = trimesh.creation.extrude_polygon(piece, height=height)
        parts.append(solid)
    if not parts:
        raise ResizeError(
            "internal_filler",
            "Zonens tvärsnitt gick inte att extrudera till ett mellanstycke.",
            "Prova mode=\"scale\" om modellen måste ändras ändå.",
        )
    solid = parts[0] if len(parts) == 1 else trimesh.util.concatenate(parts)

    u, v, n = axis_frame(axis)
    matrix = np.eye(4)
    matrix[:3, 0] = u
    matrix[:3, 1] = v
    matrix[:3, 2] = n
    matrix[:3, 3] = n * float(low)
    solid.apply_transform(matrix)
    return solid


def _stretch_once(
    mesh: trimesh.Trimesh, axis: int, span: PrismaticSpan, delta: float, cut_at: float
) -> trimesh.Trimesh:
    """Skjut in (delta > 0) eller ta bort (delta < 0) material vid `cut_at`."""
    _, _, normal = axis_frame(axis)
    half = OVERLAP_MM / 2.0

    if delta > 0:
        lower = _slice_half(mesh, axis, cut_at, keep_above=False)
        upper = _slice_half(mesh, axis, cut_at, keep_above=True)
        if lower is None or upper is None:
            raise ResizeError(
                "cut_failed",
                f"Snittet vid {cut_at:.1f} mm gav ingen delning av modellen.",
                "Välj ett annat parti att sträcka i.",
            )
        upper.apply_translation(normal * float(delta))
        filler = _filler(span.section_polygon, axis, cut_at - half, cut_at + delta + half)
        return union([lower, upper, filler])

    removed = abs(float(delta))
    low = cut_at - removed / 2.0
    high = cut_at + removed / 2.0
    lower = _slice_half(mesh, axis, low, keep_above=False)
    upper = _slice_half(mesh, axis, high, keep_above=True)
    if lower is None or upper is None:
        raise ResizeError(
            "cut_failed",
            f"Snitten vid {low:.1f} och {high:.1f} mm gav ingen delning av modellen.",
            "Välj ett annat parti att korta av i.",
        )
    upper.apply_translation(-normal * removed)
    # Halvorna möts nu i exakt samma plan. Ett tunt mellanstycke över skarven
    # ser till att booleanen aldrig ställs inför två koplanära lock.
    filler = _filler(span.section_polygon, axis, low - half, low + half)
    return union([lower, upper, filler])


def _apply_operation(
    mesh: trimesh.Trimesh, axis: int, span: PrismaticSpan, delta: float
) -> tuple[trimesh.Trimesh, _Operation, list[str]]:
    """Kör en in-/urskjutning med förskjutet snittplan som räddning."""
    warnings: list[str] = []
    removed = abs(delta) if delta < 0 else 0.0
    # Snittet måste ligga så att både det (och för avkortning båda planen)
    # hamnar innanför zonen med minst 1 mm material kvar i var ände.
    room = (span.length - removed) / 2.0
    last_error: Exception | None = None

    for attempt in range(MAX_BOOLEAN_ATTEMPTS):
        shift = 0.0
        if attempt:
            shift = BOOLEAN_RETRY_SHIFT_MM * (1 if attempt % 2 else -1) * ((attempt + 1) // 2)
        cut_at = span.middle + shift
        limit = max(room - 0.5, 0.0)
        cut_at = float(np.clip(cut_at, span.middle - limit, span.middle + limit))
        try:
            result = _stretch_once(mesh, axis, span, delta, cut_at)
        except Exception as exc:
            last_error = exc
            log.debug("Försök %d vid %.2f mm misslyckades: %s", attempt + 1, cut_at, exc)
            warnings.append(
                f"Snittplanet vid {cut_at:.1f} mm gick inte att union:era - provar ett nytt läge."
            )
            continue
        operation = _Operation(
            span_start=span.start,
            span_end=span.end,
            cut_low=cut_at - removed / 2.0,
            cut_high=cut_at + removed / 2.0,
            delta=float(delta),
            section_area=span.section_area,
        )
        # De varningar som hör till misslyckade försök är bara intressanta om
        # något faktiskt gick fel; ett lyckat första försök varnar inte.
        return result, operation, warnings

    raise ResizeError(
        "boolean_failed",
        f"Det gick inte att foga ihop modellen vid {span.describe()} "
        f"efter {MAX_BOOLEAN_ATTEMPTS} försök.",
        "Modellen kan ha överlappande ytor just där. Kör mesh-reparationen först, "
        "eller välj ett annat parti.",
        last_error=str(last_error),
    )


def _allocate(
    spans: list[PrismaticSpan], delta: float, selection: str, span_index: int | None
) -> list[tuple[PrismaticSpan, float]]:
    """Fördela `delta` över partierna enligt vald strategi.

    Vid förlängning finns ingen övre gräns - ett parti kan bli hur långt som
    helst. Vid avkortning begränsas varje parti av `capacity_mm`.
    """
    if selection == "manual":
        if span_index is None or not 0 <= span_index < len(spans):
            raise ResizeError(
                "bad_span",
                f"Zon {span_index} finns inte - modellen har {len(spans)} zon(er) längs axeln.",
                "Kör analyze-spans för att se vilka zoner som finns.",
            )
        span = spans[span_index]
        if delta < 0 and span.capacity_mm < abs(delta):
            raise ResizeError(
                "span_too_short",
                f"Zonen {span.describe()} är för kort för att korta av "
                f"{abs(delta):.1f} mm - den rymmer {span.capacity_mm:.1f} mm.",
                "Välj en annan zon eller fördela över flera med --distribute.",
            )
        return [(span, float(delta))]

    if selection == "longest":
        if delta > 0:
            return [(spans[0], float(delta))]
        # Prova zonerna i längdordning; den första som räcker till tar hela
        # avkortningen. Räcker ingen ensam faller vi tillbaka på att fördela.
        for span in spans:
            if span.capacity_mm >= abs(delta):
                return [(span, float(delta))]
        log.info("Ingen enskild zon rymmer avkortningen - fördelar över flera.")
        return _distribute(spans, delta)

    if selection == "distribute":
        return _distribute(spans, delta)

    raise ResizeError(
        "bad_selection",
        f"Okänt zonval {selection!r}.",
        "Använd longest, distribute eller manual.",
    )


def _distribute(spans: list[PrismaticSpan], delta: float) -> list[tuple[PrismaticSpan, float]]:
    """Fördela `delta` proportionellt mot zonernas längd.

    Proportionerna är poängen: en modell med fyra jämnt fördelade hyllplan
    behåller sin symmetri bara om varje mellanrum växer lika mycket i
    förhållande till sin längd.
    """
    total_length = sum(span.length for span in spans)
    if total_length <= 0:  # pragma: no cover - spans är alltid längre än 0
        raise ResizeError("no_span", "Zonerna har ingen längd.", "")

    shares = [span.length / total_length * float(delta) for span in spans]

    if delta < 0:
        capacity = [span.capacity_mm for span in spans]
        if sum(capacity) < abs(delta) - 1e-6:
            raise ResizeError(
                "not_enough_material",
                f"Modellen går att korta av som mest {sum(capacity):.1f} mm längs axeln, "
                f"men {abs(delta):.1f} mm begärdes.",
                "Minska ändringen, eller använd mode=\"scale\" om proportionerna "
                "får förändras.",
            )
        # Klipp mot kapaciteten och lägg om överskottet på zoner med luft kvar,
        # tills allt får plats.
        shares = [-min(abs(share), cap) for share, cap in zip(shares, capacity)]
        for _ in range(len(spans) + 1):
            rest = abs(delta) - sum(abs(share) for share in shares)
            if rest <= 1e-9:
                break
            slack = [cap - abs(share) for share, cap in zip(shares, capacity)]
            total_slack = sum(slack)
            if total_slack <= 1e-9:  # pragma: no cover - fångas av kontrollen ovan
                break
            shares = [
                -(abs(share) + rest * piece / total_slack)
                for share, piece in zip(shares, slack)
            ]

    return [(span, share) for span, share in zip(spans, shares) if abs(share) > 1e-6]


def _scale_axis(mesh: trimesh.Trimesh, axis: int, target_mm: float) -> trimesh.Trimesh:
    """Rak, icke-uniform skalning. Reservutväg - deformerar hål och väggar."""
    current = float(mesh.extents[axis])
    if current <= 0:
        raise ResizeError(
            "flat_model",
            f"Modellen har ingen utsträckning längs {AXIS_NAMES[axis]}.",
            "Kontrollera att filen innehåller en solid.",
        )
    factor = float(target_mm) / current
    low = float(mesh.bounds[0][axis])
    scale = np.ones(3)
    scale[axis] = factor
    out = mesh.copy()
    out.apply_translation(-np.eye(3)[axis] * low)
    out.apply_scale(scale)
    out.apply_translation(np.eye(3)[axis] * low)
    return out


def resize_axis(
    mesh: trimesh.Trimesh,
    axis: int,
    target_mm: float,
    mode: str = "preserve",
    span_selection: str = "longest",
    span_index: int | None = None,
    step: float = DEFAULT_STEP_MM,
    tol: float = DEFAULT_TOL,
    min_span_mm: float = MIN_SPAN_LENGTH_MM,
    validate: bool = True,
    progress=None,
) -> ResizeResult:
    """Ändra modellens mått längs en axel.

    `mode="preserve"` (standard) skjuter in eller tar bort material i de partier
    där tvärsnittet är konstant, så att godstjocklekar, hål och detaljer
    behåller sina mått. `mode="scale"` skalar rakt av och varnar.

    `span_selection` är `longest` (allt i den längsta zonen), `distribute`
    (proportionellt över alla zoner) eller `manual` (`span_index` pekar ut
    zonen i listan från `find_prismatic_spans`).
    """
    axis = int(axis)
    if axis not in (0, 1, 2):
        raise ValueError(f"Axeln måste vara 0, 1 eller 2 - fick {axis!r}.")
    target_mm = float(target_mm)
    if target_mm <= 0:
        raise ResizeError(
            "bad_target",
            f"Målmåttet {target_mm:g} mm är inte större än noll.",
            "Ange ett positivt mått i millimeter.",
        )

    current = float(mesh.extents[axis])
    entry = AxisResize(
        axis=axis,
        from_mm=current,
        to_mm=target_mm,
        mode=mode,
        span_selection=span_selection,
    )
    original_extents = tuple(float(v) for v in mesh.extents)
    delta = target_mm - current

    if abs(delta) < 1e-6:
        entry.to_mm = current
        return ResizeResult(
            mesh=mesh.copy(),
            axes=[entry],
            original_extents_mm=original_extents,
            target_extents_mm=tuple(
                target_mm if index == axis else None for index in range(3)
            ),
        )

    if mode == "scale":
        entry.warnings.append(
            f"Rak skalning längs {AXIS_NAMES[axis]}: godstjocklek, hörnradier och "
            "hål ändras i samma förhållande. Runda hål blir ovala."
        )
        out = _scale_axis(mesh, axis, target_mm)
        entry.actual_volume_change_mm3 = float(abs(out.volume) - abs(mesh.volume))
        entry.expected_volume_change_mm3 = entry.actual_volume_change_mm3
        if validate:
            _validate_bbox(out, axis, target_mm)
        return ResizeResult(
            mesh=out,
            axes=[entry],
            original_extents_mm=original_extents,
            target_extents_mm=tuple(
                target_mm if index == axis else None for index in range(3)
            ),
        )

    if mode != "preserve":
        raise ResizeError(
            "bad_mode", f"Okänt läge {mode!r}.", "Använd preserve eller scale."
        )

    report_progress(progress, 0.05, f"Analyserar tvärsnitt längs {AXIS_NAMES[axis]}")
    spans = find_prismatic_spans(
        mesh, axis, step=step, tol=tol, min_length_mm=min_span_mm, progress=progress
    )
    entry.spans = spans
    if not spans:
        raise ResizeError(
            "no_prismatic_span",
            f"Modellen har inget parti med konstant tvärsnitt längs "
            f"{_axis_word(axis)} — måttet kan bara ändras genom skalning, "
            "vilket förändrar godstjocklek och hål.",
            "Kör om med mode=\"scale\" om du accepterar det, eller ändra ett "
            "annat mått.",
            axis=AXIS_NAMES[axis],
        )

    report_progress(progress, 0.4, "Väljer parti att ändra i")
    chosen = _allocate(spans, delta, span_selection, span_index)
    entry.chosen = list(chosen)

    # Bearbeta uppifrån och ner. Varje ändring flyttar allt som ligger ovanför
    # snittet, så en zon längre ner behåller sina koordinater bara om den tas
    # efter de zoner som ligger över den.
    ordered = sorted(chosen, key=lambda pair: pair[0].start, reverse=True)

    before_signatures = sample_signatures(mesh, axis, step=step) if validate else []
    before_volume = float(abs(mesh.volume))

    out = mesh
    operations: list[_Operation] = []
    for index, (span, share) in enumerate(ordered):
        report_progress(
            progress,
            0.4 + 0.4 * index / max(len(ordered), 1),
            f"Ändrar {span.describe()}",
        )
        out, operation, warnings = _apply_operation(out, axis, span, share)
        operations.append(operation)
        entry.warnings.extend(warnings)

    entry.expected_volume_change_mm3 = float(
        sum(op.section_area * op.delta for op in operations)
    )
    entry.actual_volume_change_mm3 = float(abs(out.volume) - before_volume)

    if validate:
        report_progress(progress, 0.85, "Kontrollerar resultatet")
        _validate(
            out,
            axis=axis,
            target_mm=target_mm,
            expected_change=entry.expected_volume_change_mm3,
            actual_change=entry.actual_volume_change_mm3,
            before=before_signatures,
            operations=operations,
            step=step,
            tol=tol,
        )

    report_progress(progress, 1.0, "Måttändringen klar")
    return ResizeResult(
        mesh=out,
        axes=[entry],
        original_extents_mm=original_extents,
        target_extents_mm=tuple(target_mm if index == axis else None for index in range(3)),
    )


def _axis_word(axis: int) -> str:
    return {0: "bredden", 1: "djupet", 2: "höjden"}[int(axis)]


def resize(
    mesh: trimesh.Trimesh,
    target_xyz: Sequence[float | None],
    mode: str = "preserve",
    span_selection: str = "longest",
    span_index: int | None = None,
    step: float = DEFAULT_STEP_MM,
    tol: float = DEFAULT_TOL,
    min_span_mm: float = MIN_SPAN_LENGTH_MM,
    validate: bool = True,
    progress=None,
) -> ResizeResult:
    """Ändra flera mått. Axlarna tas i tur och ordning med ny analys emellan.

    `target_xyz` har tre värden; `None` betyder att måttet lämnas som det är.
    """
    if len(target_xyz) != 3:
        raise ValueError("target_xyz ska ha tre värden (X, Y, Z); None lämnar måttet.")

    original_extents = tuple(float(v) for v in mesh.extents)
    axes_to_do = [
        index
        for index, target in enumerate(target_xyz)
        if target is not None and abs(float(target) - float(mesh.extents[index])) > 1e-6
    ]

    current = mesh
    entries: list[AxisResize] = []
    for position, axis in enumerate(axes_to_do):
        def axis_progress(fraction: float, message: str, _p=position) -> None:
            report_progress(
                progress,
                (_p + fraction) / max(len(axes_to_do), 1),
                message,
            )

        step_result = resize_axis(
            current,
            axis,
            float(target_xyz[axis]),
            mode=mode,
            span_selection=span_selection,
            span_index=span_index,
            step=step,
            tol=tol,
            min_span_mm=min_span_mm,
            validate=validate,
            progress=axis_progress if progress is not None else None,
        )
        current = step_result.mesh
        entries.extend(step_result.axes)

    return ResizeResult(
        mesh=current if axes_to_do else mesh.copy(),
        axes=entries,
        original_extents_mm=original_extents,
        target_extents_mm=tuple(
            None if target is None else float(target) for target in target_xyz
        ),
    )


# --------------------------------------------------------------------------
# Validering
# --------------------------------------------------------------------------


def _validate_bbox(mesh: trimesh.Trimesh, axis: int, target_mm: float) -> None:
    actual = float(mesh.extents[axis])
    if abs(actual - target_mm) > BBOX_TOLERANCE_MM:
        raise ValidationError(
            "bbox_mismatch",
            f"Måttet längs {AXIS_NAMES[axis]} blev {actual:.2f} mm i stället för "
            f"{target_mm:.2f} mm.",
            "Måttändringen levereras inte - resultatet hade blivit fel.",
            actual_mm=actual,
            target_mm=target_mm,
        )


def _map_position(position: float, operations: Iterable[_Operation]) -> float | None:
    """Var hamnar ett ursprungsläge efter ändringarna? None = i en ändrad zon."""
    shift = 0.0
    for operation in operations:
        if operation.span_start <= position <= operation.span_end:
            return None
        if operation.cut_low < position:
            shift += operation.delta
    return position + shift


def _validate(
    mesh: trimesh.Trimesh,
    axis: int,
    target_mm: float,
    expected_change: float,
    actual_change: float,
    before: list[SectionSignature],
    operations: list[_Operation],
    step: float,
    tol: float,
) -> None:
    """Alla obligatoriska kontroller. Kastar `ValidationError` vid fel."""
    if not mesh.is_watertight:
        raise ValidationError(
            "not_watertight",
            "Den ändrade modellen är inte sluten (watertight).",
            "Måttändringen levereras inte. Reparera modellen och försök igen.",
        )
    if not mesh.is_winding_consistent:
        raise ValidationError(
            "inconsistent_winding",
            "Den ändrade modellen har inkonsekventa normalriktningar.",
            "Måttändringen levereras inte. Reparera modellen och försök igen.",
        )

    _validate_bbox(mesh, axis, target_mm)

    tolerance = max(
        VOLUME_TOLERANCE * abs(expected_change), VOLUME_ABSOLUTE_FLOOR_MM3
    )
    if abs(actual_change - expected_change) > tolerance:
        raise ValidationError(
            "volume_mismatch",
            f"Volymen ändrades {actual_change:.1f} mm³ men skulle ha ändrats "
            f"{expected_change:.1f} mm³ (tvärsnittsarea × delta).",
            "Något har gått fel i booleanerna - måttändringen levereras inte.",
            actual_mm3=actual_change,
            expected_mm3=expected_change,
        )

    _validate_untouched(mesh, axis, before, operations, tol)


def _validate_untouched(
    mesh: trimesh.Trimesh,
    axis: int,
    before: list[SectionSignature],
    operations: list[_Operation],
    tol: float,
) -> None:
    """Kontrollera att tvärsnitt utanför de ändrade zonerna är oförändrade."""
    checked = 0
    for signature in before:
        if signature.empty:
            continue
        mapped = _map_position(signature.position, operations)
        if mapped is None:
            continue
        after = section_signature(mesh, axis, mapped)
        # Tvärsnittet ligger på samma plats i planet, men har flyttats längs
        # axeln - jämförelsen görs i 2D och är därför oberoende av det.
        if not after.matches(signature, tol=max(tol, DEFAULT_TOL)):
            raise ValidationError(
                "section_changed",
                f"Tvärsnittet vid {signature.position:.1f} mm ligger utanför de "
                f"ändrade zonerna men har ändå förändrats "
                f"({signature.area:.1f} → {after.area:.1f} mm²).",
                "Måttändringen levereras inte - den hade deformerat modellen.",
                position_mm=signature.position,
            )
        checked += 1
    log.debug("Kontrollerade %d oförändrade tvärsnitt.", checked)


# --------------------------------------------------------------------------
# Rapport
# --------------------------------------------------------------------------


REPORT_NAME = "resize_report.json"


@dataclass
class ResizeReport:
    """Innehållet i `resize_report.json`."""

    result: ResizeResult
    source: object = None

    def to_dict(self) -> dict:
        return {
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": str(self.source) if self.source else None,
            **self.result.to_dict(),
        }


def write_resize_report(result: ResizeResult, out_dir, source=None, name: str = REPORT_NAME):
    """Skriv `resize_report.json` och returnera sökvägen."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    payload = ResizeReport(result=result, source=source).to_dict()
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Skrev %s", path.name)
    return path
