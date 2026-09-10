"""Beräkning av snittplan.

Fas 1 gör endast raka, axelparallella snitt i ett rutnät. Modellen kan först
roteras (`best_fit_orientation`) för att minska antalet delar.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np
import trimesh

from .printers import PrinterProfile

log = logging.getLogger(__name__)

AXIS_NAMES = ("X", "Y", "Z")


@dataclass(frozen=True)
class Plane:
    """Ett snittplan i modellens koordinatsystem (efter orientering)."""

    origin: tuple[float, float, float]
    normal: tuple[float, float, float]
    axis: int  # 0=X, 1=Y, 2=Z

    def to_dict(self) -> dict:
        return {
            "origin": [round(v, 4) for v in self.origin],
            "normal": [round(v, 4) for v in self.normal],
            "axis": AXIS_NAMES[self.axis],
            "position_mm": round(self.origin[self.axis], 4),
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

    planes: list[Plane]
    part_count: int
    part_boxes: list[PartBox]
    transform: np.ndarray = field(default_factory=lambda: np.eye(4))
    orientation_name: str = "original"
    divisions: tuple[int, int, int] = (1, 1, 1)
    bounds: np.ndarray = field(default_factory=lambda: np.zeros((2, 3)))
    printer_name: str = ""

    @property
    def needs_cutting(self) -> bool:
        return len(self.planes) > 0

    def to_dict(self) -> dict:
        return {
            "printer": self.printer_name,
            "orientation": self.orientation_name,
            "transform": [[round(v, 6) for v in row] for row in np.asarray(self.transform).tolist()],
            "divisions": {"X": self.divisions[0], "Y": self.divisions[1], "Z": self.divisions[2]},
            "part_count": self.part_count,
            "planes": [p.to_dict() for p in self.planes],
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
        if not self.planes:
            lines.append("Modellen får plats som den är - inga snitt behövs.")
        for i, plane in enumerate(self.planes, start=1):
            lines.append(
                f"  Snitt {i}: {AXIS_NAMES[plane.axis]} = {plane.origin[plane.axis]:.2f} mm"
            )
        for box in self.part_boxes:
            x, y, z = box.size_mm
            lines.append(f"  Del {box.index:02d}: {x:.1f} x {y:.1f} x {z:.1f} mm")
        return "\n".join(lines)


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
    # eigh ger stigande egenvärden; störst varians först ger längsta axeln längs X.
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
    """Testa rotationer runt X/Y/Z samt PCA och välj den som ger minst antal delar.

    Returnerar (transform, namn, antal delar). Vid lika antal delar vinner den
    orientering som ger minst spill i den största delen.
    """
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
        divisions = divisions_for(extents, printer)
        # Tiebreak: minsta bounding box-volym ger jämnare delar och mindre spill.
        waste = float(np.prod(extents))
        key = (count, waste)
        if best is None or key < best[0]:
            best = (key, name, transform, count, divisions)

    key, name, transform, count, _ = best
    log.info("Vald orientering: %s (%d delar)", name, count)
    return transform, name, count


def plan_splits(
    mesh: trimesh.Trimesh,
    printer: PrinterProfile,
    auto_orient: bool = True,
    step_deg: float = 15.0,
) -> SplitPlan:
    """Ta fram en `SplitPlan` med axelparallella snitt i ett jämnt rutnät."""
    if auto_orient:
        transform, orientation_name, _ = best_fit_orientation(mesh, printer, step_deg=step_deg)
    else:
        transform, orientation_name = np.eye(4), "original"

    oriented = mesh.copy()
    oriented.apply_transform(transform)
    bounds = np.asarray(oriented.bounds, dtype=float)
    extents = bounds[1] - bounds[0]
    divisions = divisions_for(extents, printer)

    planes: list[Plane] = []
    for axis, n in enumerate(divisions):
        if n < 2:
            continue
        span = extents[axis] / n
        normal = [0.0, 0.0, 0.0]
        normal[axis] = 1.0
        for i in range(1, n):
            origin = list(bounds[0] + extents / 2.0)
            origin[axis] = float(bounds[0][axis] + i * span)
            planes.append(
                Plane(origin=tuple(float(v) for v in origin), normal=tuple(normal), axis=axis)
            )

    part_boxes: list[PartBox] = []
    index = 0
    nx, ny, nz = divisions
    for ix in range(nx):
        for iy in range(ny):
            for iz in range(nz):
                index += 1
                part_boxes.append(
                    PartBox(
                        index=index,
                        grid=(ix, iy, iz),
                        size_mm=(
                            float(extents[0] / nx),
                            float(extents[1] / ny),
                            float(extents[2] / nz),
                        ),
                    )
                )

    return SplitPlan(
        planes=planes,
        part_count=nx * ny * nz,
        part_boxes=part_boxes,
        transform=transform,
        orientation_name=orientation_name,
        divisions=divisions,
        bounds=bounds,
        printer_name=printer.name,
    )
