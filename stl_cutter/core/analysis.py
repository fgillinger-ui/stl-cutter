"""Analys av en snittyta.

För ett givet plan tas ett tvärsnitt fram med `trimesh.section` och mäts:
area, antal öar, minsta väggtjocklek, rundhet och längd/bredd-förhållande.
Måtten används av `planner` för att poängsätta kandidatplan och av
`recommender` för att välja fogtyp.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np
import shapely
from scipy import ndimage
from shapely.geometry import Polygon

import trimesh

log = logging.getLogger(__name__)

#: Under den här väggtjockleken räknas snittet som att det skär tunna detaljer.
THIN_WALL_MM = 3.0

#: Rastreringen som mäter väggtjocklek. Upplösningen styrs av konturens
#: KORTA sida, annars mäts tunna plattor för grovt.
RASTER_LONG_SIDE_PIXELS = 512
RASTER_SHORT_SIDE_PIXELS = 64
RASTER_MIN_PIXEL_MM = 0.05
RASTER_MAX_CELLS = 4_000_000


@dataclass
class ContourInfo:
    """En sammanhängande ö i snittytan."""

    area_mm2: float
    perimeter_mm: float
    #: Diametern på den största inskrivna cirkeln - konturens lokala tjocklek.
    thickness_mm: float
    bbox_mm: tuple[float, float]

    def to_dict(self) -> dict:
        return {
            "area_mm2": round(self.area_mm2, 3),
            "perimeter_mm": round(self.perimeter_mm, 3),
            "thickness_mm": round(self.thickness_mm, 3),
            "bbox_mm": [round(v, 3) for v in self.bbox_mm],
        }


@dataclass
class SectionAnalysis:
    """Mått på hela snittytan."""

    position_mm: float
    axis: int
    area_mm2: float
    perimeter_mm: float
    contour_count: int
    min_wall_mm: float
    roundness: float
    aspect_ratio: float
    bbox_mm: tuple[float, float]
    contours: list[ContourInfo] = field(default_factory=list)
    empty: bool = False

    @property
    def main_wall_mm(self) -> float:
        """Tjockleken på snittytans största ö.

        `min_wall_mm` är minsta värdet över alla öar, och en enda tunn flik gör
        att hela snittet bedöms som tunt. För att välja fogtyp är det den ö som
        bär fogen som räknas - alltså den största.
        """
        if not self.contours:
            return self.min_wall_mm
        biggest = max(self.contours, key=lambda c: c.area_mm2)
        return float(biggest.thickness_mm)

    @property
    def has_thinner_islands(self) -> bool:
        """Finns det tunnare öar än den som bär fogen?"""
        return self.min_wall_mm < self.main_wall_mm - 1e-6

    @property
    def cuts_thin_detail(self) -> bool:
        """Skär snittet genom detaljer tunnare än 3 mm?"""
        return self.min_wall_mm < THIN_WALL_MM

    @property
    def is_flat(self) -> bool:
        """Platt snitt: tydligt avlångt eller kantigt snarare än cirkulärt."""
        return self.roundness < 0.6

    @property
    def is_round(self) -> bool:
        return self.roundness >= 0.6

    @property
    def is_elongated(self) -> bool:
        """Avlångt snitt med en tydlig glidriktning."""
        return self.aspect_ratio >= 2.0

    def to_dict(self) -> dict:
        return {
            "position_mm": round(self.position_mm, 3),
            "axis": "XYZ"[self.axis],
            "area_mm2": round(self.area_mm2, 2),
            "perimeter_mm": round(self.perimeter_mm, 2),
            "contour_count": self.contour_count,
            "min_wall_mm": round(self.min_wall_mm, 3),
            "main_wall_mm": round(self.main_wall_mm, 3),
            "roundness": round(self.roundness, 4),
            "aspect_ratio": round(self.aspect_ratio, 3),
            "bbox_mm": [round(v, 3) for v in self.bbox_mm],
            "cuts_thin_detail": self.cuts_thin_detail,
            "empty": self.empty,
            "contours": [c.to_dict() for c in self.contours],
        }

    def describe(self) -> str:
        if self.empty:
            return "Tomt snitt - planet träffar ingen geometri."
        w, h = self.bbox_mm
        return (
            f"area {self.area_mm2:.0f} mm², {self.contour_count} "
            f"{'ö' if self.contour_count == 1 else 'öar'}, minsta tjocklek "
            f"{self.min_wall_mm:.1f} mm, rundhet {self.roundness:.2f}, "
            f"{w:.0f} x {h:.0f} mm"
        )


def _empty_analysis(position: float, axis: int) -> SectionAnalysis:
    return SectionAnalysis(
        position_mm=position,
        axis=axis,
        area_mm2=0.0,
        perimeter_mm=0.0,
        contour_count=0,
        min_wall_mm=0.0,
        roundness=0.0,
        aspect_ratio=1.0,
        bbox_mm=(0.0, 0.0),
        contours=[],
        empty=True,
    )


def _to_2d(section):
    """`Path3D.to_2D()` i nyare trimesh, `to_planar()` i äldre."""
    if hasattr(section, "to_2D"):
        return section.to_2D()
    return section.to_planar()


def largest_inscribed_diameter(polygon: Polygon) -> float:
    """Diametern på största inskrivna cirkeln, via avståndstransform på ett raster.

    Ger ett robust mått på konturens lokala tjocklek: en tunn platta får ett
    litet värde även om arean är stor.
    """
    minx, miny, maxx, maxy = polygon.bounds
    width, height = maxx - minx, maxy - miny
    if width <= 0 or height <= 0:
        return 0.0

    long_side, short_side = max(width, height), min(width, height)
    pixel = max(
        min(long_side / RASTER_LONG_SIDE_PIXELS, short_side / RASTER_SHORT_SIDE_PIXELS),
        RASTER_MIN_PIXEL_MM,
    )
    while (width / pixel + 2) * (height / pixel + 2) > RASTER_MAX_CELLS:
        pixel *= 2.0
    nx = max(3, int(math.ceil(width / pixel)) + 2)
    ny = max(3, int(math.ceil(height / pixel)) + 2)

    xs = minx - pixel + (np.arange(nx) + 0.5) * pixel
    ys = miny - pixel + (np.arange(ny) + 0.5) * pixel
    grid_x, grid_y = np.meshgrid(xs, ys, indexing="ij")

    inside = shapely.contains_xy(polygon, grid_x.ravel(), grid_y.ravel()).reshape(nx, ny)
    if not inside.any():
        # Konturen är smalare än ett pixelsteg.
        return float(min(width, height))

    # Avstånd till närmaste pixel utanför konturen, i pixlar. Halva pixeln dras
    # av: avståndet mäts mellan pixelcentrum, inte till själva kanten.
    distance = ndimage.distance_transform_edt(inside)
    return float(2.0 * max(distance.max() - 0.5, 0.0) * pixel)


def analyse_polygon(polygon: Polygon) -> ContourInfo:
    minx, miny, maxx, maxy = polygon.bounds
    return ContourInfo(
        area_mm2=float(polygon.area),
        perimeter_mm=float(polygon.length),
        thickness_mm=largest_inscribed_diameter(polygon),
        bbox_mm=(float(maxx - minx), float(maxy - miny)),
    )


def analyse_section(
    mesh: trimesh.Trimesh, origin, normal, axis: int = 0
) -> SectionAnalysis:
    """Mät snittytan där `mesh` skärs av planet (origin, normal)."""
    position = float(np.asarray(origin, dtype=float)[axis])
    try:
        section = mesh.section(
            plane_origin=np.asarray(origin, dtype=float),
            plane_normal=np.asarray(normal, dtype=float),
        )
    except Exception as exc:  # pragma: no cover - beror på indata
        log.warning("Kunde inte ta fram snittyta vid %.2f mm: %s", position, exc)
        return _empty_analysis(position, axis)

    if section is None:
        return _empty_analysis(position, axis)

    try:
        planar, _ = _to_2d(section)
        polygons = [p for p in planar.polygons_full if p.area > 1e-9]
    except Exception as exc:  # pragma: no cover - trasiga konturer
        log.warning("Kunde inte platta ut snittytan vid %.2f mm: %s", position, exc)
        return _empty_analysis(position, axis)

    if not polygons:
        return _empty_analysis(position, axis)

    contours = [analyse_polygon(p) for p in polygons]
    area = float(sum(c.area_mm2 for c in contours))
    perimeter = float(sum(c.perimeter_mm for c in contours))

    bounds = np.array([p.bounds for p in polygons], dtype=float)
    width = float(bounds[:, 2].max() - bounds[:, 0].min())
    height = float(bounds[:, 3].max() - bounds[:, 1].min())
    long_side, short_side = max(width, height), min(width, height)

    # Normaliserad rundhet 4*pi*A / P^2: 1.0 för en cirkel, mindre för platta
    # eller taggiga snitt.
    roundness = float(4.0 * math.pi * area / (perimeter**2)) if perimeter > 0 else 0.0

    return SectionAnalysis(
        position_mm=position,
        axis=axis,
        area_mm2=area,
        perimeter_mm=perimeter,
        contour_count=len(contours),
        min_wall_mm=float(min(c.thickness_mm for c in contours)),
        roundness=min(roundness, 1.0),
        aspect_ratio=float(long_side / short_side) if short_side > 1e-9 else 1.0,
        bbox_mm=(width, height),
        contours=contours,
    )


def analyse_plane(mesh: trimesh.Trimesh, plane) -> SectionAnalysis:
    """Bekvämlighet: analysera ett `planner.Plane`."""
    return analyse_section(mesh, plane.origin, plane.normal, axis=plane.axis)
