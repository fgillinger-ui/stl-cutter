"""Utför de plana snitten från en `SplitPlan`."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import trimesh

from .planner import Plane, SplitPlan

log = logging.getLogger(__name__)

#: Största tillåtna volymavvikelse mellan original och summan av delarna.
VOLUME_TOLERANCE = 0.005  # 0.5 %

#: Delar mindre än så här (mm3) betraktas som skräp från snittet och kastas.
MIN_PART_VOLUME_MM3 = 1e-3


def preferred_engine() -> str | None:
    """`manifold3d` om det finns installerat, annars trimesh inbyggda motor."""
    try:
        available = trimesh.boolean.engines_available
    except Exception:  # pragma: no cover - äldre trimesh
        return None
    return "manifold" if "manifold" in available else None


@dataclass
class Part:
    """En färdig del efter snittning."""

    index: int
    mesh: trimesh.Trimesh

    @property
    def volume_mm3(self) -> float:
        return float(abs(self.mesh.volume))

    @property
    def extents_mm(self) -> tuple[float, float, float]:
        return tuple(float(v) for v in self.mesh.extents)

    @property
    def watertight(self) -> bool:
        return bool(self.mesh.is_watertight)

    def to_dict(self) -> dict:
        x, y, z = self.extents_mm
        return {
            "index": self.index,
            "size_mm": [round(x, 3), round(y, 3), round(z, 3)],
            "volume_mm3": round(self.volume_mm3, 3),
            "watertight": self.watertight,
            "faces": int(len(self.mesh.faces)),
        }


@dataclass
class CutResult:
    """Delarna plus kvalitetskontroll av snittet."""

    parts: list[Part]
    original_volume_mm3: float
    plan: SplitPlan
    warnings: list[str] = field(default_factory=list)

    @property
    def total_volume_mm3(self) -> float:
        return float(sum(p.volume_mm3 for p in self.parts))

    @property
    def volume_error(self) -> float:
        if self.original_volume_mm3 <= 0:
            return 0.0
        return abs(self.total_volume_mm3 - self.original_volume_mm3) / self.original_volume_mm3

    @property
    def all_watertight(self) -> bool:
        return all(p.watertight for p in self.parts)

    def to_dict(self) -> dict:
        return {
            "original_volume_mm3": round(self.original_volume_mm3, 3),
            "total_part_volume_mm3": round(self.total_volume_mm3, 3),
            "volume_error_percent": round(self.volume_error * 100.0, 4),
            "all_watertight": self.all_watertight,
            "warnings": list(self.warnings),
            "parts": [p.to_dict() for p in self.parts],
        }


def _slice(mesh: trimesh.Trimesh, normal, origin, engine: str | None):
    """En sida av ett plansnitt, med lock. Returnerar None om inget blev kvar."""
    try:
        piece = trimesh.intersections.slice_mesh_plane(
            mesh,
            plane_normal=np.asarray(normal, dtype=float),
            plane_origin=np.asarray(origin, dtype=float),
            cap=True,
            engine=engine,
        )
    except Exception as exc:
        log.warning("Snitt misslyckades med motorn %r (%s) - försöker utan motor.", engine, exc)
        piece = trimesh.intersections.slice_mesh_plane(
            mesh,
            plane_normal=np.asarray(normal, dtype=float),
            plane_origin=np.asarray(origin, dtype=float),
            cap=True,
        )
    if piece is None or len(piece.faces) == 0:
        return None
    piece.merge_vertices()
    if abs(piece.volume) < MIN_PART_VOLUME_MM3:
        return None
    return piece


def apply_plane(
    meshes: list[trimesh.Trimesh], plane: Plane, engine: str | None = None
) -> list[trimesh.Trimesh]:
    """Dela varje mesh i listan med ett plan; delar som inte skärs följer med orörda."""
    normal = np.asarray(plane.normal, dtype=float)
    origin = np.asarray(plane.origin, dtype=float)
    result: list[trimesh.Trimesh] = []
    for mesh in meshes:
        below = _slice(mesh, -normal, origin, engine)
        above = _slice(mesh, normal, origin, engine)
        pieces = [p for p in (below, above) if p is not None]
        result.extend(pieces if pieces else [mesh])
    return result


def cut_mesh(
    mesh: trimesh.Trimesh, plan: SplitPlan, engine: str | None = None
) -> CutResult:
    """Applicera planens orientering och snitt och returnera delarna."""
    engine = engine if engine is not None else preferred_engine()
    original_volume = float(abs(mesh.volume))

    oriented = mesh.copy()
    oriented.apply_transform(plan.transform)

    warnings: list[str] = []
    pieces: list[trimesh.Trimesh] = [oriented]
    for i, plane in enumerate(plan.planes, start=1):
        pieces = apply_plane(pieces, plane, engine=engine)
        log.debug("Efter snitt %d: %d delar", i, len(pieces))

    parts: list[Part] = []
    for index, piece in enumerate(sorted(pieces, key=lambda m: tuple(m.bounds[0])), start=1):
        piece.merge_vertices()
        if not piece.is_watertight:
            piece.fill_holes()
        if not piece.is_winding_consistent or piece.volume < 0:
            piece.fix_normals()
        if not piece.is_watertight:
            warnings.append(f"Del {index:02d} är inte watertight efter snittet.")
        parts.append(Part(index=index, mesh=piece))

    result = CutResult(
        parts=parts, original_volume_mm3=original_volume, plan=plan, warnings=warnings
    )

    if result.volume_error > VOLUME_TOLERANCE:
        message = (
            f"Volymavvikelse {result.volume_error * 100:.2f} % överstiger "
            f"{VOLUME_TOLERANCE * 100:.1f} % - kontrollera modellen."
        )
        warnings.append(message)
        log.warning(message)
    else:
        log.info("Volymavvikelse efter snitt: %.3f %%", result.volume_error * 100)

    if len(parts) != plan.part_count:
        log.info(
            "Antal delar (%d) skiljer sig från planens %d - modellen fyller inte hela rutnätet.",
            len(parts),
            plan.part_count,
        )

    return result


def parts_fit(result: CutResult, printer) -> list[int]:
    """Index på delar som inte får plats i skrivarens användbara volym."""
    return [p.index for p in result.parts if not printer.fits(sorted(p.extents_mm))]
