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
    "Insertion",
    "PrismaticSpan",
    "ResizeError",
    "ResizeReport",
    "ResizeResult",
    "SectionSignature",
    "ValidationError",
    "SPAN_SELECTIONS",
    "describe_placement",
    "detect_mirror_symmetry",
    "find_prismatic_spans",
    "mirror_plane",
    "plan_insertions",
    "resize",
    "resize_axis",
    "write_resize_report",
]

AXIS_NAMES = ("X", "Y", "Z")

#: Standardavstånd mellan provade tvärsnitt, i mm.
DEFAULT_STEP_MM = 1.0

#: Relativ tolerans när två tvärsnitt jämförs. Tajt med flit: en lös tolerans
#: låter ett svagt koniskt parti passera som prismatiskt, och då extruderas
#: mellanstycket från ett tvärsnitt som inte finns vid snittplanet - materialet
#: skjuter ut som en buckla.
DEFAULT_TOL = 0.005

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
SECTION_DEVIATION_MM = 0.02

#: Marginal till partiets ändar när snittplanet väljs, i mm. Ett snitt närmare
#: än så riskerar att hamna i övergången till detaljen som avslutar partiet.
SPAN_END_MARGIN_MM = 2.0

#: Så här långt på var sida om snittplanet kontrolleras att tvärsnittet är
#: oförändrat innan mellanstycket extruderas därifrån.
SECTION_PROBE_MM = 0.5

#: Hur långt snittplanet flyttas per försök när tvärsnittet inte är stabilt.
PROBE_RETRY_STEP_MM = 1.0

#: Så många lägen provas innan ett parti ges upp som instabilt.
MAX_PROBE_ATTEMPTS = 12

#: Största avvikelse i mm när modellen speglas i sitt mittplan och jämförs med
#: sig själv. 0,2 mm ligger under vad en 3D-skrivare kan återge.
MIRROR_TOLERANCE_MM = 0.2

#: Så många vertices provas som mest i spegeltestet.
MIRROR_SAMPLE_LIMIT = 4000

#: Hur nära två partier måste ligga varandras spegelbild för att räknas som ett
#: par, i mm. Samplingen är grovkornig, så toleransen följer `step`.
MIRROR_PAIR_TOLERANCE_MM = 1.5

#: Hur kort ett parti får vara i förhållande till det längsta och ändå räknas
#: som en del av samma upprepade mönster. Skillnaden mellan en hylla med fyra
#: lika stora fack och en låda med två tunna gavlar är just den: i det första
#: fallet är partierna jämnstora och ska växa tillsammans, i det andra
#: dominerar ett enda och de korta är detaljer som ska lämnas i fred.
REPEAT_LENGTH_RATIO = 0.75

#: Giltiga värden för `span_selection`. `auto` är standard - se docs/RESIZE.md.
SPAN_SELECTIONS = ("auto", "longest", "distribute", "manual")
DEFAULT_SPAN_SELECTION = "auto"

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
# Spegelsymmetri
# --------------------------------------------------------------------------


def mirror_plane(mesh: trimesh.Trimesh, axis: int) -> float:
    """Mittplanet vinkelrätt mot `axis`, i modellens koordinater."""
    axis = int(axis)
    return float((mesh.bounds[0][axis] + mesh.bounds[1][axis]) / 2.0)


def _mirror_sample(mesh: trimesh.Trimesh, limit: int = MIRROR_SAMPLE_LIMIT) -> np.ndarray:
    """Ett jämnt utspritt urval av modellens vertices.

    Urvalet är avsiktligt deterministiskt: ett slumpat sampel gör att samma
    modell ibland räknas som symmetrisk och ibland inte, och då går det inte
    att lita på kontrollen efter måttändringen.
    """
    vertices = np.asarray(mesh.vertices, dtype=float)
    if len(vertices) <= limit:
        return vertices
    stride = int(np.ceil(len(vertices) / limit))
    return vertices[::stride]


def _mirrored_sections_match(
    mesh: trimesh.Trimesh, axis: int, tol: float, step: float
) -> bool:
    """Är tvärsnittet lika långt på båda sidor om mittplanet?

    En spegling i mittplanet ändrar bara koordinaten längs axeln, så
    tvärsnittet vid ``mitt + d`` ska vara *identiskt* med det vid ``mitt - d``
    - inte spegelvänt. Det är kontrollen som avgör om modellen är symmetrisk
    som **kropp**, inte bara om dess hörn råkar ligga symmetriskt.
    """
    mid = mirror_plane(mesh, axis)
    half = (float(mesh.bounds[1][axis]) - float(mesh.bounds[0][axis])) / 2.0
    step = max(float(step), 1e-3)
    offsets = np.arange(step / 2.0, half, step)
    if len(offsets) == 0:
        return True
    for offset in offsets:
        above = section_signature(mesh, axis, mid + float(offset))
        below = section_signature(mesh, axis, mid - float(offset))
        if above.empty and below.empty:
            continue
        if above.empty != below.empty:
            return False
        if not above.matches(below, tol=DEFAULT_TOL, max_deviation_mm=tol):
            return False
    return True


def _mirrored_vertices_lie_on_the_surface(
    mesh: trimesh.Trimesh, axis: int, tol: float
) -> bool:
    """Hamnar modellens speglade hörn på modellens egen yta?

    Kompletterar tvärsnittsjämförelsen: en detalj som är smalare än
    provavståndet kan slinka mellan två tvärsnitt, men dess hörn gör det inte.
    """
    points = _mirror_sample(mesh).copy()
    mid = mirror_plane(mesh, axis)
    points[:, axis] = 2.0 * mid - points[:, axis]
    try:
        _, distance, _ = trimesh.proximity.closest_point(mesh, points)
    except Exception as exc:  # pragma: no cover - beror på valfria beroenden
        log.debug("Spegeltestet längs %s gick inte att köra: %s", AXIS_NAMES[axis], exc)
        return False
    if not len(distance):  # pragma: no cover - tomma meshar fångas tidigare
        return False
    deviation = float(np.max(distance))
    log.debug(
        "Spegeltest längs %s: största hörnavvikelse %.3f mm (tolerans %.3f mm).",
        AXIS_NAMES[axis],
        deviation,
        tol,
    )
    return deviation <= float(tol)


def detect_mirror_symmetry(
    mesh: trimesh.Trimesh,
    axis: int,
    tol: float = MIRROR_TOLERANCE_MM,
    step: float = DEFAULT_STEP_MM,
) -> bool:
    """Är modellen spegelsymmetrisk kring sitt mittplan längs `axis`?

    Modellen speglas i mittplanet och jämförs med sig själv på två sätt: dels
    tvärsnitt för tvärsnitt, dels genom att mäta hur långt de speglade hörnen
    hamnar från modellens yta. Håller båda sig under `tol` mm är modellen
    symmetrisk.

    Båda behövs. Ett genomgående hål har alla sina hörn på över- och
    undersidan, så de ligger kvar på ytan även speglade - bara
    tvärsnittsjämförelsen ser att hålet flyttat sig. Och en detalj som är
    smalare än `step` kan slinka mellan två tvärsnitt - bara hörnen ser den.

    Det avgör var materialet får läggas: en symmetrisk modell som blir
    osymmetrisk av en måttändring ser trasig ut även när måttet stämmer.
    """
    axis = int(axis)
    if axis not in (0, 1, 2):
        raise ValueError(f"Axeln måste vara 0, 1 eller 2 - fick {axis!r}.")
    if len(mesh.vertices) == 0:
        return False
    if not _mirrored_sections_match(mesh, axis, tol=tol, step=step):
        return False
    return _mirrored_vertices_lie_on_the_surface(mesh, axis, tol=tol)


def _mirror_buckets(
    spans: Sequence[PrismaticSpan],
    mid: float,
    tolerance: float = MIRROR_PAIR_TOLERANCE_MM,
) -> list[list[PrismaticSpan]]:
    """Gruppera partierna i spegelpar kring `mid`.

    Varje grupp är antingen ett parti som själv ligger symmetriskt över
    mittplanet, eller två partier som är varandras spegelbild. Partier utan
    spegelbild hör inte hemma i en symmetrisk fördelning och utelämnas - att ta
    med dem hade gjort resultatet osymmetriskt.
    """
    remaining = list(spans)
    buckets: list[list[PrismaticSpan]] = []

    def is_self_mirrored(span: PrismaticSpan) -> bool:
        return (
            abs((span.start + span.end) / 2.0 - mid) <= tolerance
            and span.start < mid < span.end
        )

    for span in list(remaining):
        if is_self_mirrored(span):
            buckets.append([span])
            remaining.remove(span)

    while remaining:
        span = remaining.pop(0)
        wanted = (2.0 * mid - span.end, 2.0 * mid - span.start)
        partner = None
        best = tolerance
        for candidate in remaining:
            distance = max(
                abs(candidate.start - wanted[0]), abs(candidate.end - wanted[1])
            )
            if distance <= best:
                best = distance
                partner = candidate
        if partner is None:
            log.debug("Partiet %s saknar spegelbild - utelämnas.", span.describe())
            continue
        remaining.remove(partner)
        buckets.append(sorted([span, partner], key=lambda item: item.start))

    buckets.sort(key=lambda bucket: -sum(item.length for item in bucket))
    return buckets


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
    insertions: list[Insertion] = field(default_factory=list)
    #: Var modellen spegelsymmetrisk längs axeln före respektive efter?
    #: `symmetric_after` mäts bara när `symmetric_before` är sant - det är då
    #: det är ett krav, och spegeltestet är för dyrt att köra i onödan.
    symmetric_before: bool = False
    symmetric_after: bool = False
    resolved_selection: str = ""
    expected_volume_change_mm3: float = 0.0
    actual_volume_change_mm3: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def delta_mm(self) -> float:
        return float(self.to_mm - self.from_mm)

    @property
    def chosen(self) -> list[tuple[PrismaticSpan, float]]:
        """Partierna och deras andel - kvar för rapporter och äldre anrop."""
        return [(item.span, item.delta) for item in self.insertions]

    @property
    def placement(self) -> str:
        """Var materialet hamnar, i klartext. Se `describe_placement`."""
        return describe_placement(self)

    def to_dict(self) -> dict:
        return {
            "axis": AXIS_NAMES[self.axis],
            "from_mm": round(self.from_mm, 3),
            "to_mm": round(self.to_mm, 3),
            "delta_mm": round(self.delta_mm, 3),
            "mode": self.mode,
            "span_selection": self.span_selection,
            "resolved_selection": self.resolved_selection or self.span_selection,
            "mirror_symmetric_before": bool(self.symmetric_before),
            "mirror_symmetric_after": bool(self.symmetric_after),
            "spans_found": [span.to_dict() for span in self.spans],
            "spans_used": [item.to_dict() for item in self.insertions],
            "placement": self.placement,
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


def _half_space(mesh: trimesh.Trimesh, axis: int, position: float, keep_above: bool):
    """En låda som täcker allt på ena sidan om planet, med god marginal."""
    size = np.asarray(mesh.extents, dtype=float)
    reach = float(np.max(size)) * 4.0 + 10.0
    extents = np.array([reach, reach, reach])
    box = trimesh.creation.box(extents=extents)
    centre = (np.asarray(mesh.bounds[0]) + np.asarray(mesh.bounds[1])) / 2.0
    centre[axis] = float(position) + (reach / 2.0 if keep_above else -reach / 2.0)
    box.apply_translation(centre)
    return box


def _slice_half(mesh: trimesh.Trimesh, axis: int, position: float, keep_above: bool):
    """Ena halvan av ett plansnitt, med lock. None när inget blev kvar.

    Ett plansnitt är snabbt men känsligt: en enda degenererad triangel någon
    annanstans i modellen räcker för att locket ska få ett hål, och då faller
    nästa boolean. Blir halvan inte sluten görs snittet om som en boolean mot
    ett halvrymdsblock i stället - långsammare, men det tål en sliten mesh.
    Det märks först när flera insättningar körs efter varandra på samma modell.
    """
    _, _, normal = axis_frame(axis)
    direction = normal if keep_above else -normal
    origin = normal * float(position)
    engine = engine_name()
    piece = None
    try:
        piece = trimesh.intersections.slice_mesh_plane(
            mesh, plane_normal=direction, plane_origin=origin, cap=True, engine=engine
        )
    except Exception as exc:
        log.debug("Snitt vid %.3f misslyckades med %r (%s) - provar utan motor.", position, engine, exc)
        try:
            piece = trimesh.intersections.slice_mesh_plane(
                mesh, plane_normal=direction, plane_origin=origin, cap=True
            )
        except Exception as second:
            log.debug("Snitt vid %.3f misslyckades även utan motor: %s", position, second)
            piece = None

    if piece is not None and len(piece.faces):
        piece.merge_vertices()
        if piece.is_watertight:
            return piece
        log.debug(
            "Halvan vid %.3f blev inte sluten - gör om snittet som boolean.", position
        )

    try:
        cut = trimesh.boolean.intersection(
            [mesh, _half_space(mesh, axis, position, keep_above)], engine=engine
        )
    except Exception as exc:
        log.debug("Boolean-snittet vid %.3f misslyckades: %s", position, exc)
        cut = None
    if cut is not None and len(cut.faces):
        cut.merge_vertices()
        return cut

    if piece is None or len(piece.faces) == 0:
        return None
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
    mesh: trimesh.Trimesh,
    axis: int,
    polygon,
    delta: float,
    cut_at: float,
) -> trimesh.Trimesh:
    """Skjut in (delta > 0) eller ta bort (delta < 0) material vid `cut_at`.

    `polygon` är tvärsnittet **vid snittplanet**, inte partiets representant.
    Ett mellanstycke som extruderas från ett tvärsnitt som inte finns vid
    snittet skjuter ut som en buckla i stället för att fylla igen skarven.
    """
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
        filler = _filler(polygon, axis, cut_at - half, cut_at + delta + half)
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
    filler = _filler(polygon, axis, low - half, low + half)
    return union([lower, upper, filler])


# --------------------------------------------------------------------------
# Snittplan: var materialet faktiskt läggs in
# --------------------------------------------------------------------------


@dataclass
class Insertion:
    """En planerad in- eller urskjutning, i **originalets** koordinater.

    `cut_at` är snittplanet och `polygon` tvärsnittet exakt där - inte partiets
    representantsnitt. `delta` är positivt när material läggs till.
    """

    span: PrismaticSpan
    delta: float
    cut_at: float
    polygon: object = field(repr=False, default=None)
    section_area: float = 0.0

    @property
    def axis(self) -> int:
        return int(self.span.axis)

    @property
    def cut_low(self) -> float:
        return float(self.cut_at - (abs(self.delta) / 2.0 if self.delta < 0 else 0.0))

    @property
    def cut_high(self) -> float:
        return float(self.cut_at + (abs(self.delta) / 2.0 if self.delta < 0 else 0.0))

    def to_dict(self) -> dict:
        return {
            **self.span.to_dict(),
            "applied_delta_mm": round(float(self.delta), 3),
            "cut_at_mm": round(float(self.cut_at), 3),
            "cut_section_area_mm2": round(float(self.section_area), 3),
        }


def _section_is_stable(
    mesh: trimesh.Trimesh,
    axis: int,
    position: float,
    tol: float,
    probe: float = SECTION_PROBE_MM,
) -> bool:
    """Är tvärsnittet oförändrat ±`probe` mm kring `position`?

    Det är kontrollen som avslöjar ett snittplan som ligger för nära en detalj:
    tvärsnittet *vid* planet kan se ut som partiets, men en halv millimeter bort
    har en fasning eller ett hål redan börjat.
    """
    here = section_signature(mesh, axis, position)
    if here.empty:
        return False
    for offset in (-probe, probe):
        neighbour = section_signature(mesh, axis, position + offset)
        if neighbour.empty or not here.matches(neighbour, tol=tol):
            return False
    return True


def _cut_candidates(span: PrismaticSpan, delta: float, preferred: float) -> list[float]:
    """Lägen att prova för snittplanet, bäst först.

    Första förslaget är `preferred` (partiets mitt, eller mittplanet när
    symmetrin kräver det). Går det inte vandrar vi mot partiets mitt och sedan
    utåt därifrån - aldrig närmare änden än `SPAN_END_MARGIN_MM`.
    """
    removed = abs(float(delta)) if delta < 0 else 0.0
    low = span.start + SPAN_END_MARGIN_MM + removed / 2.0
    high = span.end - SPAN_END_MARGIN_MM - removed / 2.0
    if high < low:
        # Partiet är så kort att marginalen inte får plats. Mitten är då det
        # enda läget som håller lika mycket material i båda ändar.
        low = high = span.middle

    def clamp(value: float) -> float:
        return float(np.clip(value, low, high))

    middle = clamp(span.middle)
    candidates = [clamp(preferred), middle]
    for index in range(1, MAX_PROBE_ATTEMPTS):
        shift = PROBE_RETRY_STEP_MM * ((index + 1) // 2) * (1 if index % 2 else -1)
        candidates.append(clamp(middle + shift))

    unique: list[float] = []
    for value in candidates:
        if all(abs(value - seen) > 1e-6 for seen in unique):
            unique.append(value)
    return unique


def _plan_insertion(
    mesh: trimesh.Trimesh,
    axis: int,
    span: PrismaticSpan,
    delta: float,
    preferred: float | None = None,
    tol: float = DEFAULT_TOL,
) -> Insertion:
    """Välj snittplan och hämta tvärsnittet exakt där."""
    wanted = span.middle if preferred is None else float(preferred)
    fallback: Insertion | None = None
    for cut_at in _cut_candidates(span, delta, wanted):
        polygon = section_polygon(mesh, axis, cut_at)
        if polygon is None:
            continue
        insertion = Insertion(
            span=span,
            delta=float(delta),
            cut_at=float(cut_at),
            polygon=polygon,
            section_area=float(polygon.area),
        )
        if _section_is_stable(mesh, axis, cut_at, tol=tol):
            return insertion
        fallback = fallback or insertion
        log.debug(
            "Tvärsnittet vid %.2f mm är inte stabilt ±%.2f mm - provar ett nytt läge.",
            cut_at,
            SECTION_PROBE_MM,
        )
    if fallback is not None:
        return fallback
    raise ResizeError(
        "unstable_section",
        f"Tvärsnittet i {span.describe()} ändrar sig kring varje snittläge som "
        "provades.",
        "Sänk --step så att partiets gränser hittas noggrannare, eller välj ett "
        "annat parti.",
    )


def plan_insertions(
    mesh: trimesh.Trimesh,
    axis: int,
    target_mm: float,
    span_selection: str = DEFAULT_SPAN_SELECTION,
    span_index: int | None = None,
    step: float = DEFAULT_STEP_MM,
    tol: float = DEFAULT_TOL,
    min_span_mm: float = MIN_SPAN_LENGTH_MM,
    spans: Sequence[PrismaticSpan] | None = None,
    symmetric: bool | None = None,
    progress=None,
) -> tuple[list[Insertion], list[PrismaticSpan], bool]:
    """Räkna ut var materialet ska läggas, utan att ändra modellen.

    Returnerar `(insättningar, partier, symmetrisk)`. Alla lägen är uttryckta i
    **originalets** koordinater och räknas ut innan den första operationen körs,
    så att en tidigare insättning inte hinner förskjuta nästa snittplan.

    Samma funktion driver både måttändringen och förhandsvisningen i
    gränssnittet - det som ritas gult är exakt det som kommer att göras.
    """
    axis = int(axis)
    delta = float(target_mm) - float(mesh.extents[axis])
    if spans is None:
        spans = find_prismatic_spans(
            mesh, axis, step=step, tol=tol, min_length_mm=min_span_mm, progress=progress
        )
    spans = list(spans)
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
    if abs(delta) < 1e-6:
        return [], spans, bool(symmetric) if symmetric is not None else False

    mid = mirror_plane(mesh, axis)
    if symmetric is None:
        symmetric = (
            detect_mirror_symmetry(mesh, axis) if span_selection == "auto" else False
        )

    chosen, centred = _allocate(
        spans, delta, span_selection, span_index, mid=mid, symmetric=bool(symmetric)
    )
    insertions = [
        _plan_insertion(
            mesh,
            axis,
            span,
            share,
            preferred=mid if id(span) in centred else None,
            tol=tol,
        )
        for span, share in chosen
    ]
    # Uppifrån och ner: varje insättning flyttar allt ovanför sig, så ett parti
    # längre ner behåller sina originalkoordinater bara om det tas sist.
    insertions.sort(key=lambda item: item.cut_at, reverse=True)
    return insertions, spans, bool(symmetric)


def _apply_operation(
    mesh: trimesh.Trimesh, axis: int, insertion: Insertion, tol: float = DEFAULT_TOL
) -> tuple[trimesh.Trimesh, _Operation, list[str]]:
    """Kör en in-/urskjutning med förskjutet snittplan som räddning."""
    warnings: list[str] = []
    span = insertion.span
    delta = insertion.delta
    removed = abs(delta) if delta < 0 else 0.0
    candidates = _cut_candidates(span, delta, insertion.cut_at)
    last_error: Exception | None = None

    for attempt in range(min(MAX_BOOLEAN_ATTEMPTS, len(candidates))):
        cut_at = candidates[attempt]
        polygon = (
            insertion.polygon
            if attempt == 0
            else section_polygon(mesh, axis, cut_at)
        )
        if polygon is None:
            continue
        try:
            result = _stretch_once(mesh, axis, polygon, delta, cut_at)
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
            section_area=float(polygon.area),
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


# --------------------------------------------------------------------------
# Fördelning
# --------------------------------------------------------------------------


def _allocate(
    spans: list[PrismaticSpan],
    delta: float,
    selection: str,
    span_index: int | None,
    mid: float = 0.0,
    symmetric: bool = False,
) -> tuple[list[tuple[PrismaticSpan, float]], set[int]]:
    """Fördela `delta` över partierna enligt vald strategi.

    Returnerar dels fördelningen, dels vilka partier (som `id()`) som ska få
    sitt snitt centrerat på mittplanet i stället för på partiets egen mitt.

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
        return [(span, float(delta))], set()

    if selection == "auto":
        return _allocate_auto(spans, delta, mid=mid, symmetric=symmetric)

    if selection == "longest":
        if delta > 0:
            return [(spans[0], float(delta))], set()
        # Prova zonerna i längdordning; den första som räcker till tar hela
        # avkortningen. Räcker ingen ensam faller vi tillbaka på att fördela.
        for span in spans:
            if span.capacity_mm >= abs(delta):
                return [(span, float(delta))], set()
        log.info("Ingen enskild zon rymmer avkortningen - fördelar över flera.")
        return _distribute(spans, delta), set()

    if selection == "distribute":
        return _distribute(spans, delta), set()

    raise ResizeError(
        "bad_selection",
        f"Okänt zonval {selection!r}.",
        f"Använd {', '.join(SPAN_SELECTIONS)}.",
    )


def _repeated_spans(spans: Sequence[PrismaticSpan]) -> list[PrismaticSpan]:
    """De partier som bildar modellens upprepade mönster.

    Ett mönster känns igen på att partierna är ungefär lika långa: fem lika
    stora mellanrum mellan sex stegpinnar, fyra lika höga fack i en hylla. Ett
    parti som är mycket kortare än det längsta är inte en del av mönstret utan
    en detalj - materialet mellan ett hål och gaveln, eller ett lock - och
    sådana ska inte växa bara för att de råkar ha konstant tvärsnitt.
    """
    spans = list(spans)
    if not spans:
        return []
    longest = max(span.length for span in spans)
    pattern = [span for span in spans if span.length >= REPEAT_LENGTH_RATIO * longest]
    return pattern or spans


def _allocate_auto(
    spans: list[PrismaticSpan], delta: float, mid: float, symmetric: bool
) -> tuple[list[tuple[PrismaticSpan, float]], set[int]]:
    """`auto`: symmetrin först, proportionerna sedan.

    1. Först plockas de partier ut som bildar modellens upprepade mönster -
       de jämnstora. Korta enstaka partier lämnas orörda.
    2. Är modellen spegelsymmetrisk längs axeln grupperas mönstrets partier i
       spegelpar, och varje par får lika mycket. Då blir resultatet
       symmetriskt vad som än händer.
    3. Finns flera partier kvar fördelas `delta` proportionellt mot deras
       längd. Det är det som håller mellanrummen mellan upprepade detaljer
       lika stora.
    4. Finns bara ett parti hamnar hela tillskottet där - centrerat på
       mittplanet när partiet ligger över det, annars mitt i partiet.
    """
    pattern = _repeated_spans(spans)
    try:
        return _allocate_pattern(pattern, delta, mid, symmetric)
    except ResizeError as error:
        if error.code != "not_enough_material" or len(pattern) == len(spans):
            raise
        # Mönstret rymmer inte avkortningen. Hellre än att vägra tar vi hjälp
        # av de korta partierna också - det syns i rapporten vilka som användes.
        log.info("Mönstret rymmer inte avkortningen - tar med alla partier.")
        return _allocate_pattern(list(spans), delta, mid, symmetric)


def _allocate_pattern(
    spans: list[PrismaticSpan], delta: float, mid: float, symmetric: bool
) -> tuple[list[tuple[PrismaticSpan, float]], set[int]]:
    if symmetric:
        buckets = _mirror_buckets(spans, mid)
        if buckets:
            return _distribute_buckets(buckets, delta, mid)
        log.info(
            "Modellen är symmetrisk men inget parti har en spegelbild - "
            "fördelar proportionellt i stället."
        )

    if len(spans) == 1:
        span = spans[0]
        centred = {id(span)} if span.start < mid < span.end else set()
        return [(span, float(delta))], centred
    return _distribute(spans, delta), set()


def _distribute_buckets(
    buckets: list[list[PrismaticSpan]], delta: float, mid: float
) -> tuple[list[tuple[PrismaticSpan, float]], set[int]]:
    """Fördela proportionellt över spegelgrupper och dela lika inom varje grupp.

    Att dela lika inom gruppen är det som gör resultatet symmetriskt: två
    partier som är varandras spegelbild ska växa lika mycket, annars flyttar
    sig modellens mitt.
    """
    if len(buckets) == 1 and len(buckets[0]) == 1:
        span = buckets[0][0]
        centred = {id(span)} if span.start < mid < span.end else set()
        return [(span, float(delta))], centred

    weights = [sum(span.length for span in bucket) for bucket in buckets]
    total = sum(weights)
    if total <= 0:  # pragma: no cover - partier har alltid längd
        raise ResizeError("no_span", "Zonerna har ingen längd.", "")

    shares = [weight / total * float(delta) for weight in weights]

    if delta < 0:
        capacity = [sum(span.capacity_mm for span in bucket) for bucket in buckets]
        if sum(capacity) < abs(delta) - 1e-6:
            raise ResizeError(
                "not_enough_material",
                f"Modellen går att korta av som mest {sum(capacity):.1f} mm längs axeln, "
                f"men {abs(delta):.1f} mm begärdes.",
                "Minska ändringen, eller använd mode=\"scale\" om proportionerna "
                "får förändras.",
            )
        shares = _fit_to_capacity(shares, capacity, delta)

    chosen: list[tuple[PrismaticSpan, float]] = []
    centred: set[int] = set()
    for bucket, share in zip(buckets, shares):
        if abs(share) <= 1e-6:
            continue
        each = share / len(bucket)
        for span in bucket:
            chosen.append((span, each))
            if len(bucket) == 1 and span.start < mid < span.end:
                centred.add(id(span))
    return chosen, centred


def _fit_to_capacity(
    shares: list[float], capacity: list[float], delta: float
) -> list[float]:
    """Klipp negativa andelar mot kapaciteten och lägg om överskottet."""
    shares = [-min(abs(share), cap) for share, cap in zip(shares, capacity)]
    for _ in range(len(shares) + 1):
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
    return shares


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
        shares = _fit_to_capacity(shares, capacity, delta)

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
    span_selection: str = DEFAULT_SPAN_SELECTION,
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

    `span_selection` är `auto` (standard - symmetrin först, proportionerna
    sedan), `longest` (allt i den längsta zonen), `distribute` (proportionellt
    över alla zoner) eller `manual` (`span_index` pekar ut zonen i listan från
    `find_prismatic_spans`).
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
        entry.resolved_selection = "scale"
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

    # Spegeltestet körs alltid, inte bara för `auto`: var modellen symmetrisk
    # innan ska den vara det efteråt oavsett hur partierna valdes.
    report_progress(progress, 0.3, "Kontrollerar spegelsymmetri")
    symmetric_before = detect_mirror_symmetry(mesh, axis)
    entry.symmetric_before = symmetric_before

    report_progress(progress, 0.4, "Väljer parti att ändra i")
    insertions, spans, _ = plan_insertions(
        mesh,
        axis,
        target_mm,
        span_selection=span_selection,
        span_index=span_index,
        step=step,
        tol=tol,
        min_span_mm=min_span_mm,
        spans=spans,
        symmetric=symmetric_before,
    )
    entry.insertions = list(insertions)
    entry.resolved_selection = _resolved_selection(span_selection, symmetric_before, insertions)

    before_signatures = sample_signatures(mesh, axis, step=step) if validate else []
    before_volume = float(abs(mesh.volume))
    before_extents = tuple(float(value) for value in mesh.extents)

    out = mesh
    operations: list[_Operation] = []
    for index, insertion in enumerate(insertions):
        report_progress(
            progress,
            0.5 + 0.3 * index / max(len(insertions), 1),
            f"Ändrar {insertion.span.describe()}",
        )
        out, operation, warnings = _apply_operation(out, axis, insertion, tol=tol)
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
            before_extents=before_extents,
            symmetric_before=symmetric_before,
            operations=operations,
            step=step,
            tol=tol,
        )
    entry.symmetric_after = (
        detect_mirror_symmetry(out, axis) if symmetric_before else False
    )

    log.info("%s", entry.placement)
    report_progress(progress, 1.0, "Måttändringen klar")
    return ResizeResult(
        mesh=out,
        axes=[entry],
        original_extents_mm=original_extents,
        target_extents_mm=tuple(target_mm if index == axis else None for index in range(3)),
    )


def _resolved_selection(
    requested: str, symmetric: bool, insertions: Sequence[Insertion]
) -> str:
    """Vilken strategi `auto` faktiskt landade i - för rapporten och loggen."""
    if requested != "auto":
        return requested
    if symmetric:
        return "symmetric" if len(insertions) > 1 else "symmetric-centered"
    return "distribute" if len(insertions) > 1 else "longest"


def _swedish(value: float, decimals: int = 1) -> str:
    """Tal med svenskt decimaltecken - loggen läses av en svensk användare."""
    return f"{value:.{decimals}f}".replace(".", ",")


def _join_swedish(parts: Sequence[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} och {parts[-1]}"


def describe_placement(entry: "AxisResize") -> str:
    """Exakt var materialet hamnar, på svenska.

    Exempel::

        Y: 240,0 → 250,0 mm. 10,0 mm fördelat på 2 partier:
        +5,0 mm vid y=78,0 och +5,0 mm vid y=162,0.

    Meningen är hela poängen med återkopplingen: den som läser loggen ska
    kunna peka på modellen och säga "där växte den".
    """
    name = AXIS_NAMES[entry.axis]
    head = (
        f"{name}: {_swedish(entry.from_mm)} → {_swedish(entry.to_mm)} mm"
    )
    if abs(entry.delta_mm) < 1e-6:
        return f"{head}. Måttet var redan rätt."
    if entry.mode == "scale":
        return f"{head}. Hela modellen skalades längs {name.lower()}."
    if not entry.insertions:
        return f"{head}."

    verb = "fördelat" if entry.delta_mm > 0 else "borttaget"
    amount = f"{_swedish(abs(entry.delta_mm))} mm"
    places = [
        f"{'+' if item.delta > 0 else '−'}{_swedish(abs(item.delta))} mm vid "
        f"{name.lower()}={_swedish(item.cut_at)}"
        for item in sorted(entry.insertions, key=lambda item: item.cut_at)
    ]
    if len(places) == 1:
        placed = "tillagt" if entry.delta_mm > 0 else "borttaget"
        return f"{head}. {amount} {placed} vid {places[0].split(' vid ')[1]}."
    return (
        f"{head}. {amount} {verb} på {len(places)} partier: "
        f"{_join_swedish(places)}."
    )



def _axis_word(axis: int) -> str:
    return {0: "bredden", 1: "djupet", 2: "höjden"}[int(axis)]


def resize(
    mesh: trimesh.Trimesh,
    target_xyz: Sequence[float | None],
    mode: str = "preserve",
    span_selection: str = DEFAULT_SPAN_SELECTION,
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


def _validate_other_axes(
    mesh: trimesh.Trimesh, axis: int, before_extents: Sequence[float]
) -> None:
    """De axlar som inte ändrades ska vara exakt kvar.

    Kontrollen finns för att ett mellanstycke som extruderas från fel tvärsnitt
    kan skjuta ut i sidled: måttet längs den ändrade axeln stämmer, men
    modellen har blivit bredare utan att något sagt ifrån.
    """
    for other in range(3):
        if other == axis:
            continue
        was = float(before_extents[other])
        now = float(mesh.extents[other])
        if abs(now - was) > BBOX_TOLERANCE_MM:
            raise ValidationError(
                "side_effect",
                f"Måttet längs {AXIS_NAMES[other]} ändrades från {was:.2f} till "
                f"{now:.2f} mm trots att bara {AXIS_NAMES[axis]} skulle ändras.",
                "Måttändringen levereras inte - materialet hamnade utanför "
                "partiets tvärsnitt.",
                axis=AXIS_NAMES[other],
                from_mm=was,
                to_mm=now,
            )


def _validate_symmetry(mesh: trimesh.Trimesh, axis: int) -> None:
    """Var modellen spegelsymmetrisk före ska den vara det efter."""
    if detect_mirror_symmetry(mesh, axis):
        return
    raise ValidationError(
        "symmetry_lost",
        f"Modellen var spegelsymmetrisk längs {AXIS_NAMES[axis]} men är det inte "
        "längre efter måttändringen.",
        "Måttändringen levereras inte - materialet hamnade bara på ena sidan.",
        axis=AXIS_NAMES[axis],
    )


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
    before_extents: Sequence[float] | None = None,
    symmetric_before: bool = False,
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

    if before_extents is not None:
        _validate_other_axes(mesh, axis, before_extents)

    if symmetric_before:
        _validate_symmetry(mesh, axis)

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
