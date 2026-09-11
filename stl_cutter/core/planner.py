"""Beräkning av snittplan.

Fas 1 gav raka, axelparallella snitt i ett jämnt rutnät. Fas 2 lägger till
kandidatplan: runt varje nödvändig snittposition provas alternativa lägen som
poängsätts mot snittytans mått (`analysis`), och det bästa läget väljs. Antalet
delar hålls konstant - kandidatfönstret begränsas så att alla skivor fortfarande
får plats i byggvolymen.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np
import trimesh

from .analysis import SectionAnalysis, analyse_section
from .printers import PrinterProfile
from .progress import report
from .recommender import AssemblyIntent, JointRecommendation, recommend_joint

log = logging.getLogger(__name__)

AXIS_NAMES = ("X", "Y", "Z")

#: Vikter för poängsättningen av kandidatplan. Alla är straff (högre = sämre)
#: och kan justeras av anroparen via `score_config`.
SCORE_WEIGHTS = {
    # Viktigast: en kandidat som skulle öka antalet delar väljs aldrig.
    "part_count": 1000.0,
    # Snitt genom tunna väggar ger svaga fogar.
    "thin_wall": 25.0,
    # Flera öar i snittet betyder flera fogar att passa ihop.
    "contours": 8.0,
    # För liten area = svag fog, onödigt stor = svår passning.
    "area": 10.0,
    # En del som blir nästan tom är sällan meningsfull.
    "small_part": 20.0,
    # Håll snittet nära den jämnt fördelade positionen om inget annat talar emot.
    "offset": 4.0,
    # En del som fyller byggplattan helt lämnar ingen plats för fogen.
    "joint_room": 15.0,
}

#: Konfiguration för kandidatsökningen.
SCORE_CONFIG = {
    "search_fraction": 0.15,  # ±15 % av modellens längd i axeln
    "step_mm": 2.0,
    "max_candidates": 120,
    "thin_wall_target_mm": 4.0,
    "area_min_mm2": 200.0,
    "area_max_mm2": 20000.0,
    "min_part_fraction": 0.05,  # skiva tunnare än 5 % av axeln straffas
    "joint_room_mm": 12.0,  # utrymme som helst ska finnas kvar till fogen
}


@dataclass(frozen=True)
class Plane:
    """Ett snittplan i modellens koordinatsystem (efter orientering)."""

    origin: tuple[float, float, float]
    normal: tuple[float, float, float]
    axis: int  # 0=X, 1=Y, 2=Z

    @property
    def position(self) -> float:
        return float(self.origin[self.axis])

    def to_dict(self) -> dict:
        return {
            "origin": [round(v, 4) for v in self.origin],
            "normal": [round(v, 4) for v in self.normal],
            "axis": AXIS_NAMES[self.axis],
            "position_mm": round(self.position, 4),
        }


@dataclass
class CandidateScore:
    """Poäng för ett kandidatplan. Lägre `total` är bättre (poängen är straff)."""

    position_mm: float
    total: float
    penalties: dict[str, float] = field(default_factory=dict)
    feasible: bool = True

    def to_dict(self) -> dict:
        return {
            "position_mm": round(self.position_mm, 3),
            "total": round(self.total, 3),
            "feasible": self.feasible,
            "penalties": {k: round(v, 3) for k, v in self.penalties.items()},
        }


@dataclass
class CutInfo:
    """Ett snitt med allt som är känt om det."""

    index: int
    plane: Plane
    analysis: SectionAnalysis | None = None
    score: CandidateScore | None = None
    recommendation: JointRecommendation | None = None
    alternatives: list[JointRecommendation] = field(default_factory=list)
    nominal_position_mm: float | None = None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "plane": self.plane.to_dict(),
            "nominal_position_mm": (
                round(self.nominal_position_mm, 3)
                if self.nominal_position_mm is not None
                else None
            ),
            "analysis": self.analysis.to_dict() if self.analysis else None,
            "score": self.score.to_dict() if self.score else None,
            "recommendation": (
                self.recommendation.to_dict() if self.recommendation else None
            ),
            "alternatives": [a.to_dict() for a in self.alternatives],
        }


@dataclass
class PartBox:
    """Förväntad bounding box för en del, före själva snittet."""

    index: int
    grid: tuple[int, int, int]
    size_mm: tuple[float, float, float]

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "grid": list(self.grid),
            "size_mm": [round(v, 3) for v in self.size_mm],
        }


@dataclass
class SplitPlan:
    """Resultatet av planeringen."""

    cuts: list[CutInfo]
    part_count: int
    part_boxes: list[PartBox]
    transform: np.ndarray = field(default_factory=lambda: np.eye(4))
    orientation_name: str = "original"
    divisions: tuple[int, int, int] = (1, 1, 1)
    bounds: np.ndarray = field(default_factory=lambda: np.zeros((2, 3)))
    printer_name: str = ""
    assembly_intent: str = "glue"

    @property
    def planes(self) -> list[Plane]:
        return [cut.plane for cut in self.cuts]

    @property
    def needs_cutting(self) -> bool:
        return len(self.cuts) > 0

    @property
    def analysed(self) -> bool:
        return any(cut.analysis is not None for cut in self.cuts)

    def to_dict(self) -> dict:
        return {
            "printer": self.printer_name,
            "orientation": self.orientation_name,
            "assembly_intent": self.assembly_intent,
            "transform": [
                [round(v, 6) for v in row] for row in np.asarray(self.transform).tolist()
            ],
            "divisions": {"X": self.divisions[0], "Y": self.divisions[1], "Z": self.divisions[2]},
            "part_count": self.part_count,
            "planes": [p.to_dict() for p in self.planes],
            "cuts": [c.to_dict() for c in self.cuts],
            "part_boxes": [b.to_dict() for b in self.part_boxes],
            "bounds_mm": [[round(v, 3) for v in row] for row in np.asarray(self.bounds).tolist()],
        }

    def describe(self) -> str:
        lines = [
            f"Skrivare: {self.printer_name or 'okänd'}",
            f"Orientering: {self.orientation_name}",
            f"Uppdelning: {self.divisions[0]} x {self.divisions[1]} x {self.divisions[2]} "
            f"= {self.part_count} delar",
        ]
        if not self.cuts:
            lines.append("Modellen får plats som den är - inga snitt behövs.")
        for cut in self.cuts:
            plane = cut.plane
            line = f"  Snitt {cut.index}: {AXIS_NAMES[plane.axis]} = {plane.position:.2f} mm"
            if cut.recommendation:
                line += f"  -> {cut.recommendation.joint_type}"
            lines.append(line)
        for box in self.part_boxes:
            x, y, z = box.size_mm
            lines.append(f"  Del {box.index:02d}: {x:.1f} x {y:.1f} x {z:.1f} mm")
        return "\n".join(lines)

    def explain(self) -> str:
        """Läsbar svensk text om snittytorna och fogvalen (`--explain`)."""
        from .recommender import explain as explain_cut

        if not self.analysed:
            return "Ingen analys gjord - kör med analys påslagen för att se motiveringar."
        blocks = [f"Monteringsavsikt: {self.assembly_intent}"]
        for cut in self.cuts:
            if cut.analysis is None or cut.recommendation is None:
                continue
            blocks.append(explain_cut(cut.analysis, cut.recommendation, cut.index))
            if len(cut.alternatives) > 1:
                others = ", ".join(
                    f"{a.joint_type} ({a.confidence * 100:.0f} %)" for a in cut.alternatives[1:]
                )
                blocks.append(f"  Alternativ: {others}")
        return "\n\n".join(blocks)


# --------------------------------------------------------------------------
# Grundläggande uppdelning
# --------------------------------------------------------------------------


def divisions_for(extents, printer: PrinterProfile) -> tuple[int, int, int]:
    """Antal delar per axel: ceil(storlek / (byggmått - 2*marginal))."""
    usable = printer.usable
    return tuple(max(1, math.ceil(float(e) / u - 1e-9)) for e, u in zip(extents, usable))


def part_count_for(extents, printer: PrinterProfile) -> int:
    nx, ny, nz = divisions_for(extents, printer)
    return nx * ny * nz


def _rotation(axis: int, degrees: float) -> np.ndarray:
    direction = np.zeros(3)
    direction[axis] = 1.0
    return trimesh.transformations.rotation_matrix(math.radians(degrees), direction)


def _pca_transform(mesh: trimesh.Trimesh) -> np.ndarray:
    """Rotation som lägger modellens huvudaxlar längs X, Y och Z."""
    points = np.asarray(mesh.vertices, dtype=float)
    centered = points - points.mean(axis=0)
    covariance = np.cov(centered, rowvar=False)
    _, vectors = np.linalg.eigh(covariance)
    basis = vectors[:, ::-1]
    if np.linalg.det(basis) < 0:
        basis[:, 2] *= -1.0
    transform = np.eye(4)
    transform[:3, :3] = basis.T
    return transform


def _extents_after(mesh: trimesh.Trimesh, transform: np.ndarray):
    points = trimesh.transform_points(np.asarray(mesh.vertices, dtype=float), transform)
    return points.max(axis=0) - points.min(axis=0)


def best_fit_orientation(
    mesh: trimesh.Trimesh, printer: PrinterProfile, step_deg: float = 15.0
) -> tuple[np.ndarray, str, int]:
    """Testa rotationer runt X/Y/Z samt PCA och välj den som ger minst antal delar."""
    candidates: list[tuple[str, np.ndarray]] = [("original", np.eye(4))]
    steps = int(round(180.0 / step_deg))
    for axis in range(3):
        for i in range(1, steps):
            angle = i * step_deg
            candidates.append((f"rotation {AXIS_NAMES[axis]} {angle:g}°", _rotation(axis, angle)))
    try:
        candidates.append(("PCA", _pca_transform(mesh)))
    except np.linalg.LinAlgError as exc:  # pragma: no cover - degenererad geometri
        log.warning("PCA-orientering misslyckades: %s", exc)

    best = None
    for name, transform in candidates:
        extents = _extents_after(mesh, transform)
        count = part_count_for(extents, printer)
        waste = float(np.prod(extents))
        key = (count, waste)
        if best is None or key < best[0]:
            best = (key, name, transform, count)

    _, name, transform, count = best
    log.info("Vald orientering: %s (%d delar)", name, count)
    return transform, name, count


# --------------------------------------------------------------------------
# Kandidatplan och poängsättning
# --------------------------------------------------------------------------


def _make_plane(bounds: np.ndarray, axis: int, position: float) -> Plane:
    origin = list((bounds[0] + bounds[1]) / 2.0)
    origin[axis] = float(position)
    normal = [0.0, 0.0, 0.0]
    normal[axis] = 1.0
    return Plane(
        origin=tuple(float(v) for v in origin), normal=tuple(normal), axis=axis
    )


def candidate_positions(
    nominal: float,
    axis_length: float,
    window: tuple[float, float],
    config: dict | None = None,
) -> list[float]:
    """Kandidatlägen i ett intervall runt `nominal`, i steg om `step_mm`.

    `window` är det tillåtna intervallet (lägsta, högsta) som håller antalet
    delar oförändrat. Nominalpositionen är alltid med.
    """
    cfg = {**SCORE_CONFIG, **(config or {})}
    reach = cfg["search_fraction"] * axis_length
    step = cfg["step_mm"]

    low = max(window[0], nominal - reach)
    high = min(window[1], nominal + reach)
    if high < low:
        return [float(np.clip(nominal, window[0], window[1]))]

    span = high - low
    count = int(span / step) + 1
    if count > cfg["max_candidates"]:
        step = span / (cfg["max_candidates"] - 1)

    positions = list(np.arange(low, high + 1e-9, step))
    if not any(abs(p - nominal) < 1e-6 for p in positions) and low <= nominal <= high:
        positions.append(nominal)
    return sorted(float(p) for p in positions)


def score_candidate(
    analysis: SectionAnalysis,
    position: float,
    nominal: float,
    slab_fractions: tuple[float, float],
    axis_length: float,
    weights: dict | None = None,
    config: dict | None = None,
    slabs_mm: tuple[float, float] | None = None,
    usable_mm: float | None = None,
) -> CandidateScore:
    """Poängsätt ett kandidatplan. Poängen är straff - lägre är bättre.

    `slab_fractions` är de två angränsande skivornas tjocklek som andel av
    axelns längd, och används som billig approximation av delvolymen.
    `slabs_mm` och `usable_mm` används för att hålla delarna en bit från
    byggvolymens gräns, så att fogen får plats att sticka ut.
    """
    w = {**SCORE_WEIGHTS, **(weights or {})}
    cfg = {**SCORE_CONFIG, **(config or {})}
    penalties: dict[str, float] = {}

    if analysis.empty:
        # Ett tomt snitt delar ingenting - oanvändbart.
        return CandidateScore(position, w["part_count"], {"empty": w["part_count"]}, False)

    # Tunn vägg i snittet: linjärt straff under målvärdet.
    target = cfg["thin_wall_target_mm"]
    if analysis.min_wall_mm < target:
        deficit = (target - analysis.min_wall_mm) / target
        penalties["thin_wall"] = w["thin_wall"] * deficit

    # Fler öar än en: varje extra kontur är en extra fog.
    if analysis.contour_count > 1:
        penalties["contours"] = w["contours"] * (analysis.contour_count - 1)

    # Area: straffa både för liten och onödigt stor yta (logaritmiskt avstånd
    # till det önskade intervallet).
    area = max(analysis.area_mm2, 1e-6)
    if area < cfg["area_min_mm2"]:
        penalties["area"] = w["area"] * math.log10(cfg["area_min_mm2"] / area)
    elif area > cfg["area_max_mm2"]:
        penalties["area"] = w["area"] * math.log10(area / cfg["area_max_mm2"])

    # Väldigt tunn skiva på någon sida av snittet.
    thinnest = min(slab_fractions)
    if thinnest < cfg["min_part_fraction"]:
        deficit = (cfg["min_part_fraction"] - thinnest) / cfg["min_part_fraction"]
        penalties["small_part"] = w["small_part"] * deficit

    # En skiva som fyller byggplattan helt lämnar ingen plats för fogens
    # nyckel, som måste sticka ut några millimeter.
    if slabs_mm is not None and usable_mm:
        wanted = cfg["joint_room_mm"]
        tightest = min(usable_mm - float(slab) for slab in slabs_mm)
        if tightest < wanted:
            penalties["joint_room"] = w["joint_room"] * (wanted - max(tightest, 0.0)) / wanted

    # Avvikelse från jämn fördelning.
    reach = max(cfg["search_fraction"] * axis_length, 1e-6)
    penalties["offset"] = w["offset"] * abs(position - nominal) / reach

    return CandidateScore(position, float(sum(penalties.values())), penalties, True)


def _optimise_axis(
    mesh: trimesh.Trimesh,
    bounds: np.ndarray,
    axis: int,
    divisions: int,
    usable: float,
    weights: dict | None,
    config: dict | None,
    progress=None,
    progress_span: tuple[float, float] = (0.0, 1.0),
) -> list[tuple[Plane, SectionAnalysis, CandidateScore, float]]:
    """Välj snittlägen längs en axel, ett i taget, med bibehållet antal delar."""
    low, high = float(bounds[0][axis]), float(bounds[1][axis])
    length = high - low
    span = length / divisions

    chosen: list[tuple[Plane, SectionAnalysis, CandidateScore, float]] = []
    previous = low
    for i in range(1, divisions):
        nominal = low + i * span
        # Fönstret som garanterar att alla skivor får plats i byggvolymen.
        window = (
            max(low + 1e-3, high - (divisions - i) * usable),
            min(high - 1e-3, previous + usable),
        )
        positions = candidate_positions(nominal, length, window, config)

        best = None
        start, end = progress_span
        for step, position in enumerate(positions):
            report(
                progress,
                start + (end - start) * ((i - 1) + step / max(len(positions), 1)) / max(divisions - 1, 1),
                f"Analyserar snittläge {position:.0f} mm längs {AXIS_NAMES[axis]}",
            )
            plane = _make_plane(bounds, axis, position)
            analysis = analyse_section(mesh, plane.origin, plane.normal, axis=axis)
            remaining = divisions - i
            slab_fractions = (
                (position - previous) / length,
                (high - position) / (remaining * length),
            )
            score = score_candidate(
                analysis,
                position,
                nominal,
                slab_fractions,
                length,
                weights,
                config,
                slabs_mm=(position - previous, (high - position) / max(remaining, 1)),
                usable_mm=usable,
            )
            if best is None or score.total < best[2].total:
                best = (plane, analysis, score, nominal)

        chosen.append(best)
        previous = best[0].position
        log.debug(
            "Axel %s snitt %d: %.2f mm (nominellt %.2f, straff %.2f)",
            AXIS_NAMES[axis],
            i,
            best[0].position,
            best[3],
            best[2].total,
        )
    return chosen


def _part_boxes(bounds: np.ndarray, positions: dict[int, list[float]]) -> list[PartBox]:
    """Faktiska dellådor utifrån valda snittlägen (kan vara olika stora)."""
    edges = []
    for axis in range(3):
        cuts = sorted(positions.get(axis, []))
        edges.append([float(bounds[0][axis]), *cuts, float(bounds[1][axis])])

    boxes: list[PartBox] = []
    index = 0
    for ix in range(len(edges[0]) - 1):
        for iy in range(len(edges[1]) - 1):
            for iz in range(len(edges[2]) - 1):
                index += 1
                boxes.append(
                    PartBox(
                        index=index,
                        grid=(ix, iy, iz),
                        size_mm=(
                            edges[0][ix + 1] - edges[0][ix],
                            edges[1][iy + 1] - edges[1][iy],
                            edges[2][iz + 1] - edges[2][iz],
                        ),
                    )
                )
    return boxes


def plan_splits(
    mesh: trimesh.Trimesh,
    printer: PrinterProfile,
    auto_orient: bool = True,
    step_deg: float = 15.0,
    analyse: bool = True,
    assembly_intent: AssemblyIntent = "glue",
    weights: dict | None = None,
    score_config: dict | None = None,
    progress=None,
) -> SplitPlan:
    """Ta fram en `SplitPlan`.

    Med `analyse=True` (standard) poängsätts kandidatplan och varje snitt får
    en analys och en fogrekommendation. Med `analyse=False` läggs snitten
    jämnt fördelade utan analys - snabbt, och det som fas 1 gjorde.
    """
    report(progress, 0.0, "Beräknar bästa orientering")
    if auto_orient:
        transform, orientation_name, _ = best_fit_orientation(mesh, printer, step_deg=step_deg)
    else:
        transform, orientation_name = np.eye(4), "original"

    oriented = mesh.copy()
    oriented.apply_transform(transform)
    bounds = np.asarray(oriented.bounds, dtype=float)
    extents = bounds[1] - bounds[0]
    divisions = divisions_for(extents, printer)

    cuts: list[CutInfo] = []
    positions: dict[int, list[float]] = {}
    index = 0
    axes_to_cut = [a for a, c in enumerate(divisions) if c > 1]
    for order, axis in enumerate(axes_to_cut):
        count = divisions[axis]
        span = (
            0.05 + 0.9 * order / len(axes_to_cut),
            0.05 + 0.9 * (order + 1) / len(axes_to_cut),
        )
        if analyse:
            picked = _optimise_axis(
                oriented,
                bounds,
                axis,
                count,
                printer.usable[axis],
                weights,
                score_config,
                progress=progress,
                progress_span=span,
            )
        else:
            span = extents[axis] / count
            picked = [
                (_make_plane(bounds, axis, bounds[0][axis] + i * span), None, None,
                 float(bounds[0][axis] + i * span))
                for i in range(1, count)
            ]

        positions[axis] = [p[0].position for p in picked]
        for plane, analysis, score, nominal in picked:
            index += 1
            info = CutInfo(
                index=index,
                plane=plane,
                analysis=analysis,
                score=score,
                nominal_position_mm=nominal,
            )
            if analysis is not None:
                best, alternatives = recommend_joint(
                    analysis, intent=assembly_intent, printer=printer
                )
                info.recommendation = best
                info.alternatives = alternatives
            cuts.append(info)

    report(progress, 1.0, "Planen är klar")
    nx, ny, nz = divisions
    return SplitPlan(
        cuts=cuts,
        part_count=nx * ny * nz,
        part_boxes=_part_boxes(bounds, positions),
        transform=transform,
        orientation_name=orientation_name,
        divisions=divisions,
        bounds=bounds,
        printer_name=printer.name,
        assembly_intent=assembly_intent,
    )
