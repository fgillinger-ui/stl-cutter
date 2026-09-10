"""Gemensam grund för foggeometri.

Alla fogtyper följer samma metod: fogens "nyckel" byggs som en solid, adderas
till del A och subtraheras - uppförstorad med `clearance_mm` per sida - från
del B. `manifold3d` används som boolean-motor genomgående.

Koordinatsystem: varje fog byggs i ett lokalt system där snittplanet är z = 0,
`u` och `v` ligger i planet och `n` (z) pekar från del A mot del B. `PlaneFrame`
sköter översättningen till och från världskoordinater.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

import numpy as np
import shapely
import trimesh
from shapely.geometry import MultiPolygon, Polygon

log = logging.getLogger(__name__)

#: Överlapp in i den egna delen så att booleaner aldrig möts exakt kant-i-kant.
OVERLAP_MM = 1.0

#: Hur långt in i respektive del snittytan mäts, för att undvika att
#: sektionera exakt i en yta.
SECTION_EPS_MM = 0.05

#: Tillåten volymavvikelse vid kontroll efter en boolean.
VOLUME_SLACK = 0.02

#: Hörn närmare varandra än så här slås ihop innan en polygon extruderas.
#: Sektioner av booleanbearbetade meshar innehåller ofta nästan sammanfallande
#: punkter, som annars ger degenererade trianglar och ogiltiga solider.
POLYGON_SIMPLIFY_MM = 0.001


@dataclass
class JointParams:
    """Parametrar för foggeometrin. Alla mått i mm."""

    joint_type: str = "none"
    clearance_mm: float = 0.15

    # dovetail
    count: int = 2
    width_mm: float = 12.0
    depth_mm: float = 10.0
    angle_deg: float = 8.0
    chamfer_mm: float = 0.4

    # pins
    diameter_mm: float = 6.0
    length_mm: float = 12.0
    edge_margin_mm: float = 3.0
    extra_hole_depth_mm: float = 0.3

    # puzzle
    period_mm: float = 20.0
    amplitude_mm: float = 4.0
    profile: str = "sine"

    # screw
    hole_diameter_mm: float = 3.4
    counterbore_diameter_mm: float = 6.0
    counterbore_depth_mm: float = 3.0
    nut_across_flats_mm: float = 5.5
    nut_depth_mm: float = 2.6

    # styrpinnar som komplement till en annan fog
    guide_pins: int = 0
    guide_pin_diameter_mm: float = 5.0

    @classmethod
    def from_recommendation(cls, recommendation, clearance_mm: float | None = None):
        """Bygg parametrar från en `JointRecommendation` (fas 2)."""
        params = cls(joint_type=recommendation.joint_type)
        known = {f.name for f in params.__dataclass_fields__.values()}
        updates = {k: v for k, v in (recommendation.params or {}).items() if k in known}
        params = replace(params, **updates)
        if clearance_mm is not None:
            params = replace(params, clearance_mm=clearance_mm)
        return params

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class JointResult:
    """Resultatet av ett fogbygge."""

    mesh_a: trimesh.Trimesh
    mesh_b: trimesh.Trimesh
    joint_type: str
    requested_type: str
    applied: bool
    warnings: list[str] = field(default_factory=list)
    attempts: list[str] = field(default_factory=list)

    @property
    def fell_back(self) -> bool:
        return self.joint_type != self.requested_type

    def to_dict(self) -> dict:
        return {
            "joint_type": self.joint_type,
            "requested_type": self.requested_type,
            "applied": self.applied,
            "fell_back": self.fell_back,
            "warnings": list(self.warnings),
            "attempts": list(self.attempts),
        }


class JointError(RuntimeError):
    """Fogen gick inte att bygga med de givna parametrarna."""


# --------------------------------------------------------------------------
# Plan och kontaktyta
# --------------------------------------------------------------------------


@dataclass
class PlaneFrame:
    """Lokalt koordinatsystem för ett snittplan.

    `n` pekar från del A mot del B. `u` är kontaktytans långa riktning och `v`
    den korta - fogar som ska glida gör det längs `v`.
    """

    origin: np.ndarray
    u: np.ndarray
    v: np.ndarray
    n: np.ndarray

    @property
    def to_world(self) -> np.ndarray:
        matrix = np.eye(4)
        matrix[:3, 0] = self.u
        matrix[:3, 1] = self.v
        matrix[:3, 2] = self.n
        matrix[:3, 3] = self.origin
        return matrix

    @property
    def to_local(self) -> np.ndarray:
        return np.linalg.inv(self.to_world)

    def place(self, mesh: trimesh.Trimesh) -> trimesh.Trimesh:
        """Flytta en mesh från lokalt system till världskoordinater."""
        out = mesh.copy()
        out.apply_transform(self.to_world)
        return out


def _orthonormal_basis(normal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Två vektorer vinkelräta mot `normal` och mot varandra."""
    normal = normal / np.linalg.norm(normal)
    helper = np.array([1.0, 0.0, 0.0])
    if abs(float(np.dot(helper, normal))) > 0.9:
        helper = np.array([0.0, 1.0, 0.0])
    u = np.cross(normal, helper)
    u /= np.linalg.norm(u)
    v = np.cross(normal, u)
    return u, v


def clean_polygon(polygon):
    """Städa en polygon: ta bort nästan sammanfallande hörn och laga topologin."""
    if polygon is None or polygon.is_empty:
        return polygon
    cleaned = polygon.simplify(POLYGON_SIMPLIFY_MM, preserve_topology=True)
    if not cleaned.is_valid:
        cleaned = cleaned.buffer(0)
    if cleaned.is_empty or cleaned.area <= 1e-9:
        return polygon
    return cleaned


def _section_polygon(
    mesh: trimesh.Trimesh, origin: np.ndarray, normal: np.ndarray, to_local: np.ndarray
):
    """Tvärsnittet som en shapely-polygon i det lokala systemet."""
    section = mesh.section(plane_origin=origin, plane_normal=normal)
    if section is None:
        return None
    try:
        planar, _ = (
            section.to_2D(to_2D=to_local)
            if hasattr(section, "to_2D")
            else section.to_planar(to_2D=to_local)
        )
    except Exception as exc:
        log.debug("Kunde inte platta ut tvärsnitt: %s", exc)
        return None
    polygons = [p for p in planar.polygons_full if p.area > 1e-9]
    if not polygons:
        return None
    merged = shapely.union_all(polygons)
    return merged if merged.is_valid else merged.buffer(0)


def contact_region(
    mesh_a: trimesh.Trimesh,
    mesh_b: trimesh.Trimesh,
    origin,
    normal,
    eps: float = SECTION_EPS_MM,
):
    """Den yta där två delar faktiskt möts, som shapely-geometri i planet.

    Snitten tas en aning in i respektive del, så att sektioneringen inte
    hamnar exakt i en plan yta där resultatet blir opålitligt.
    """
    origin = np.asarray(origin, dtype=float)
    normal = np.asarray(normal, dtype=float)
    normal = normal / np.linalg.norm(normal)
    u, v = _orthonormal_basis(normal)
    frame = PlaneFrame(origin=origin, u=u, v=v, n=normal)
    to_local = frame.to_local

    poly_a = _section_polygon(mesh_a, origin - normal * eps, normal, to_local)
    poly_b = _section_polygon(mesh_b, origin + normal * eps, normal, to_local)
    if poly_a is None or poly_b is None:
        return None, frame

    overlap = poly_a.intersection(poly_b)
    if overlap.is_empty or overlap.area <= 1e-9:
        return None, frame
    if not overlap.is_valid:
        overlap = overlap.buffer(0)
    return clean_polygon(overlap), frame


def aligned_frame(region, frame: PlaneFrame) -> tuple[PlaneFrame, Polygon]:
    """Rikta `u` längs kontaktytans långa riktning och `v` längs den korta.

    Returnerar det nya systemet och kontaktytan uttryckt i det.
    """
    largest = largest_polygon(region)
    if largest is None:
        return frame, largest

    rectangle = largest.minimum_rotated_rectangle
    coords = np.asarray(rectangle.exterior.coords)[:4]
    edges = coords[1:] - coords[:-1]
    lengths = np.linalg.norm(edges, axis=1)
    long_edge = edges[int(np.argmax(lengths))]
    angle = float(np.arctan2(long_edge[1], long_edge[0]))

    cos_a, sin_a = float(np.cos(angle)), float(np.sin(angle))
    new_u = cos_a * frame.u + sin_a * frame.v
    new_v = -sin_a * frame.u + cos_a * frame.v
    rotated = PlaneFrame(origin=frame.origin, u=new_u, v=new_v, n=frame.n)

    region_rotated = shapely.affinity.rotate(
        region, -np.degrees(angle), origin=(0.0, 0.0), use_radians=False
    )
    return rotated, region_rotated


def reach(
    mesh_a: trimesh.Trimesh, mesh_b: trimesh.Trimesh, frame: PlaneFrame
) -> tuple[float, float]:
    """Hur långt varje del sträcker sig från snittytan längs n."""
    local_a = np.asarray(mesh_a.copy().apply_transform(frame.to_local).bounds)
    local_b = np.asarray(mesh_b.copy().apply_transform(frame.to_local).bounds)
    return abs(float(local_a[0][2])), abs(float(local_b[1][2]))


def largest_polygon(region) -> Polygon | None:
    """Största sammanhängande ytan i en shapely-geometri."""
    if region is None or region.is_empty:
        return None
    if isinstance(region, Polygon):
        return region
    if isinstance(region, MultiPolygon):
        return max(region.geoms, key=lambda g: g.area)
    polygons = [g for g in getattr(region, "geoms", []) if isinstance(g, Polygon)]
    return max(polygons, key=lambda g: g.area) if polygons else None


# --------------------------------------------------------------------------
# Booleaner med kontroll
# --------------------------------------------------------------------------


def engine_name() -> str | None:
    """`manifold` när det finns installerat, annars trimesh inbyggda motor."""
    try:
        return "manifold" if "manifold" in trimesh.boolean.engines_available else None
    except Exception:  # pragma: no cover - äldre trimesh
        return None


def _clean(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.remove_unreferenced_vertices()
    if not mesh.is_winding_consistent or mesh.volume < 0:
        mesh.fix_normals()
    return mesh


def _boolean(operation: str, meshes: list[trimesh.Trimesh]) -> trimesh.Trimesh:
    engine = engine_name()
    function = getattr(trimesh.boolean, operation)
    try:
        result = function(meshes, engine=engine)
    except Exception as exc:
        log.debug("Boolean %s misslyckades med %r (%s) - försöker städa indata.", operation, engine, exc)
        cleaned = [_clean(m.copy()) for m in meshes]
        result = function(cleaned, engine=engine)
    if result is None or len(result.faces) == 0:
        raise JointError(f"Boolean {operation} gav ett tomt resultat.")
    return _clean(result)


def union(meshes: list[trimesh.Trimesh]) -> trimesh.Trimesh:
    return _boolean("union", meshes)


def difference(meshes: list[trimesh.Trimesh]) -> trimesh.Trimesh:
    return _boolean("difference", meshes)


def intersection(meshes: list[trimesh.Trimesh]) -> trimesh.Trimesh:
    return _boolean("intersection", meshes)


def check_mesh(mesh: trimesh.Trimesh, label: str) -> list[str]:
    """Kontrollera en mesh efter en boolean. Returnerar problem som text."""
    problems: list[str] = []
    if mesh is None or len(mesh.faces) == 0:
        return [f"{label}: tom mesh"]
    if not mesh.is_watertight:
        problems.append(f"{label}: inte watertight")
    if not mesh.is_winding_consistent:
        problems.append(f"{label}: inkonsekventa normaler")
    if mesh.volume <= 0:
        problems.append(f"{label}: volymen är noll eller negativ")
    return problems


def validate_parts(parts) -> dict[int, list[str]]:
    """Kontrollera alla delar före export. Returnerar problem per delindex."""
    report: dict[int, list[str]] = {}
    for part in parts:
        mesh = getattr(part, "mesh", part)
        index = getattr(part, "index", len(report) + 1)
        problems = check_mesh(mesh, f"Del {index:02d}")
        if problems:
            report[index] = problems
    return report


# --------------------------------------------------------------------------
# Byggstenar
# --------------------------------------------------------------------------


def chamfered_prism(section: np.ndarray, z_values: list[float]) -> trimesh.Trimesh:
    """Konvext prisma av en konvex tvärsnittspolygon staplad på flera z-nivåer.

    `section` är en lista av (x, y, skalfaktor)-nivåer: varje z får sin egen
    skala, vilket ger en fas i änden utan att formen slutar vara konvex.
    """
    points = []
    for (scale_x, scale_y), z in zip(section, z_values):
        points.append(np.column_stack([scale_x, scale_y, np.full(len(scale_x), z)]))
    hull = trimesh.convex.convex_hull(np.vstack(points))
    return _clean(hull)


def chamfered_cylinder(
    radius: float, length: float, chamfer: float, sections: int = 48
) -> trimesh.Trimesh:
    """Cylinder längs +z med fasad topp. Basen ligger i z = 0."""
    angles = np.linspace(0.0, 2.0 * np.pi, sections, endpoint=False)
    circle = np.column_stack([np.cos(angles), np.sin(angles)])
    chamfer = float(min(max(chamfer, 0.0), radius * 0.5, length * 0.5))
    levels = [
        (circle[:, 0] * radius, circle[:, 1] * radius),
        (circle[:, 0] * radius, circle[:, 1] * radius),
        (
            circle[:, 0] * (radius - chamfer),
            circle[:, 1] * (radius - chamfer),
        ),
    ]
    return chamfered_prism(levels, [0.0, length - chamfer, length])


def prism_from_polygon(polygon: Polygon, z_min: float, z_max: float) -> trimesh.Trimesh:
    """Extrudera en (möjligen konkav) polygon mellan två z-nivåer."""
    solid = trimesh.creation.extrude_polygon(
        clean_polygon(polygon), height=float(z_max - z_min)
    )
    solid.apply_translation([0.0, 0.0, float(z_min)])
    return _clean(solid)


# --------------------------------------------------------------------------
# Gemensamt gränssnitt för fogtyper
# --------------------------------------------------------------------------


class JointBuilder:
    """Basklass för fogtyper.

    Standardmetoden: `keys()` bygger fogens nyckel i lokala koordinater, som
    sedan adderas till del A och - uppförstorad med `clearance_mm` per sida -
    subtraheras från del B. Fogtyper som inte passar i det mönstret
    (`puzzle`, `screw`) skriver över `build()`.
    """

    joint_type = "none"
    #: Enklare fogtyp att falla tillbaka på om den här inte går att bygga.
    fallback: str | None = None

    #: Hur långt delarna sträcker sig från snittytan längs n. Sätts av `build()`
    #: innan `keys()` anropas, så att fogen kan begränsas till materialet.
    reach_a: float = 1e6
    reach_b: float = 1e6

    def keys(
        self,
        region: Polygon,
        params: JointParams,
        grow: float = 0.0,
        offset: tuple[float, float] = (0.0, 0.0),
    ) -> list[trimesh.Trimesh]:
        """Nyckelsolider i lokala koordinater (planet är z = 0, z > 0 är del B).

        `grow` läggs till alla mått per sida - används för honans urtag.
        `offset` förskjuter fogen i planet, för robusthetsförsöket.
        """
        raise NotImplementedError

    def footprint(self, region: Polygon, params: JointParams) -> Polygon | None:
        """Ytan i planet som fogen upptar - används för att placera styrpinnar."""
        solids = self.keys(region, params)
        if not solids:
            return None
        polygons = []
        for solid in solids:
            minx, miny = solid.bounds[0][:2]
            maxx, maxy = solid.bounds[1][:2]
            polygons.append(shapely.box(minx, miny, maxx, maxy))
        return shapely.union_all(polygons)

    def build(
        self,
        mesh_a: trimesh.Trimesh,
        mesh_b: trimesh.Trimesh,
        plane,
        params: JointParams,
        offset: tuple[float, float] = (0.0, 0.0),
    ) -> tuple[trimesh.Trimesh, trimesh.Trimesh]:
        """Bygg fogen mellan två delar. Del A får nyckeln, del B får urtaget."""
        region, frame = contact_region(mesh_a, mesh_b, plane.origin, plane.normal)
        if region is None:
            raise JointError("Delarna har ingen gemensam kontaktyta.")
        frame, region = aligned_frame(region, frame)
        region = largest_polygon(region)
        if region is None or region.area <= 1e-6:
            raise JointError("Kontaktytan är för liten för en fog.")

        self.reach_a, self.reach_b = reach(mesh_a, mesh_b, frame)

        keys = self.keys(region, params, grow=0.0, offset=offset)
        pockets = self.keys(region, params, grow=params.clearance_mm, offset=offset)
        if not keys or not pockets:
            raise JointError(f"Ingen {self.joint_type} fick plats i kontaktytan.")

        key = union([frame.place(k) for k in keys]) if len(keys) > 1 else frame.place(keys[0])
        pocket = (
            union([frame.place(p) for p in pockets])
            if len(pockets) > 1
            else frame.place(pockets[0])
        )

        out_a = union([mesh_a, key])
        out_b = difference([mesh_b, pocket])
        return out_a, out_b
