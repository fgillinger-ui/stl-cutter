"""Utför de plana snitten från en `SplitPlan`."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import trimesh

from .joints import JointParams, build_joint, validate_parts
from .mesh_io import open_edge_count, repair_mesh
from .planner import Plane, SplitPlan
from .progress import report

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
class JointRecord:
    """Loggpost för en byggd fog mellan två delar."""

    cut_index: int
    part_a: int
    part_b: int
    joint_type: str
    requested_type: str
    applied: bool
    warnings: list[str] = field(default_factory=list)

    @property
    def fell_back(self) -> bool:
        return self.joint_type != self.requested_type

    def to_dict(self) -> dict:
        return {
            "cut_index": self.cut_index,
            "part_a": self.part_a,
            "part_b": self.part_b,
            "joint_type": self.joint_type,
            "requested_type": self.requested_type,
            "applied": self.applied,
            "fell_back": self.fell_back,
            "warnings": list(self.warnings),
        }


@dataclass
class CutResult:
    """Delarna plus kvalitetskontroll av snittet."""

    parts: list[Part]
    original_volume_mm3: float
    plan: SplitPlan
    warnings: list[str] = field(default_factory=list)
    joints: list[JointRecord] = field(default_factory=list)
    #: Öppna kanter i originalmodellen. > 0 betyder att delarna ärver hål.
    source_open_edges: int = 0

    @property
    def inherited_damage(self) -> bool:
        """Kommer delarnas problem från en trasig originalmodell?"""
        return self.source_open_edges > 0

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
            "source_open_edges": self.source_open_edges,
            "warnings": list(self.warnings),
            "joints": [j.to_dict() for j in self.joints],
            "parts": [p.to_dict() for p in self.parts],
        }

    def validate(self) -> dict[int, list[str]]:
        """Kontrollera alla delar före export."""
        return validate_parts(self.parts)


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
    mesh: trimesh.Trimesh,
    plan: SplitPlan,
    engine: str | None = None,
    joints: bool = False,
    printer=None,
    force_joint: str | None = None,
    progress=None,
) -> CutResult:
    """Applicera planens orientering och snitt och returnera delarna.

    Med `joints=True` byggs dessutom foggeometrin enligt planens
    rekommendationer (fas 3). `force_joint` tvingar en viss fogtyp.
    """
    engine = engine if engine is not None else preferred_engine()
    original_volume = float(abs(mesh.volume))

    oriented = mesh.copy()
    oriented.apply_transform(plan.transform)

    warnings: list[str] = []
    pieces: list[trimesh.Trimesh] = [oriented]
    total = max(len(plan.planes), 1)
    for i, plane in enumerate(plan.planes, start=1):
        report(progress, 0.5 * (i - 1) / total, f"Kapar snitt {i} av {total}")
        pieces = apply_plane(pieces, plane, engine=engine)
        log.debug("Efter snitt %d: %d delar", i, len(pieces))

    # Var originalet redan trasigt? Då ärver delarna det, och det är inte
    # snittningen som är felet.
    source_open_edges = open_edge_count(mesh)

    parts: list[Part] = []
    for index, piece in enumerate(sorted(pieces, key=lambda m: tuple(m.bounds[0])), start=1):
        piece, _ = repair_mesh(piece)
        if not piece.is_watertight:
            if source_open_edges > 0:
                warnings.append(
                    f"Del {index:02d} är inte sluten ({open_edge_count(piece)} öppna kanter). "
                    "Originalmodellen hade hål, så delen ärver dem - kapningen är inte felet."
                )
            else:
                warnings.append(
                    f"Del {index:02d} är inte sluten efter snittet "
                    f"({open_edge_count(piece)} öppna kanter)."
                )
        parts.append(Part(index=index, mesh=piece))

    result = CutResult(
        parts=parts,
        original_volume_mm3=original_volume,
        plan=plan,
        warnings=warnings,
        source_open_edges=source_open_edges,
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

    if joints:
        apply_joints(result, printer=printer, force_joint=force_joint, progress=progress)
    report(progress, 1.0, f"Klar - {len(parts)} delar")

    if len(parts) != plan.part_count:
        log.info(
            "Antal delar (%d) skiljer sig från planens %d - modellen fyller inte hela rutnätet.",
            len(parts),
            plan.part_count,
        )

    return result


def parts_fit(result: CutResult, printer) -> list[int]:
    """Index på delar som inte får plats i skrivarens användbara volym.

    Delarna jämförs sorterade mot en sorterad byggvolym: en del får vridas på
    plattan, så det är bara måtten som måste räcka till - inte vilken axel de
    råkar ligga på.
    """
    usable = sorted(printer.usable)
    return [
        part.index
        for part in result.parts
        if any(size > limit + 1e-6 for size, limit in zip(sorted(part.extents_mm), usable))
    ]


# --------------------------------------------------------------------------
# Fogar (fas 3)
# --------------------------------------------------------------------------

#: Hur nära en dels kant måste ligga snittplanet för att räknas som angränsande.
ADJACENCY_TOL_MM = 0.05

#: Minsta överlapp i planet för att två delar ska anses dela en yta.
MIN_OVERLAP_MM = 1.0


def _overlap_in_plane(a: trimesh.Trimesh, b: trimesh.Trimesh, axis: int) -> float:
    """Minsta överlapp mellan två delar i de två axlar som inte är snittaxeln."""
    overlaps = []
    for other in range(3):
        if other == axis:
            continue
        low = max(a.bounds[0][other], b.bounds[0][other])
        high = min(a.bounds[1][other], b.bounds[1][other])
        overlaps.append(high - low)
    return float(min(overlaps))


def find_pairs(parts: list[Part], plan: SplitPlan) -> list[tuple[Part, Part, object]]:
    """Hitta delar som möts vid ett snittplan.

    Del A ligger under planet och får fogens nyckel, del B ligger över och får
    urtaget. Paren tas fram innan någon fog byggs, eftersom delarnas
    bounding box ändras när nycklar läggs till.
    """
    pairs: list[tuple[Part, Part, object]] = []
    for cut in plan.cuts:
        axis = cut.plane.axis
        position = cut.plane.position
        below = [p for p in parts if abs(p.mesh.bounds[1][axis] - position) <= ADJACENCY_TOL_MM]
        above = [p for p in parts if abs(p.mesh.bounds[0][axis] - position) <= ADJACENCY_TOL_MM]
        for part_a in below:
            for part_b in above:
                if part_a is part_b:
                    continue
                if _overlap_in_plane(part_a.mesh, part_b.mesh, axis) > MIN_OVERLAP_MM:
                    pairs.append((part_a, part_b, cut))
    return pairs


def _params_for(cut, printer, force_joint: str | None) -> JointParams:
    """Fogparametrar för ett snitt: rekommendationen från fas 2, eller ett tvingat val."""
    clearance = printer.clearance_mm if printer is not None else None
    if cut.recommendation is not None:
        params = JointParams.from_recommendation(cut.recommendation, clearance)
    else:
        params = JointParams(joint_type="none")
        if clearance is not None:
            params.clearance_mm = clearance
    if force_joint:
        params.joint_type = force_joint
    return params


def apply_joints(
    result: "CutResult",
    printer=None,
    force_joint: str | None = None,
    progress=None,
) -> "CutResult":
    """Bygg fogar mellan alla angränsande delar enligt planens rekommendationer."""
    pairs = find_pairs(result.parts, result.plan)
    if not pairs:
        log.info("Inga angränsande delar att foga ihop.")
        return result

    for number, (part_a, part_b, cut) in enumerate(pairs, start=1):
        report(
            progress,
            0.5 + 0.5 * (number - 1) / len(pairs),
            f"Bygger fog {number} av {len(pairs)}",
        )
        params = _params_for(cut, printer, force_joint)
        if params.joint_type == "none":
            continue

        joint = build_joint(part_a.mesh, part_b.mesh, cut.plane, params)
        if joint.applied:
            part_a.mesh = joint.mesh_a
            part_b.mesh = joint.mesh_b

        record = JointRecord(
            cut_index=cut.index,
            part_a=part_a.index,
            part_b=part_b.index,
            joint_type=joint.joint_type,
            requested_type=joint.requested_type,
            applied=joint.applied,
            warnings=joint.warnings,
        )
        result.joints.append(record)
        for warning in joint.warnings:
            result.warnings.append(f"Snitt {cut.index}, del {part_a.index}-{part_b.index}: {warning}")
        log.info(
            "Snitt %d: fog %s mellan del %02d och %02d (%s)",
            cut.index,
            joint.joint_type,
            part_a.index,
            part_b.index,
            "byggd" if joint.applied else "ej byggd",
        )

    problems = result.validate()
    for index, issues in problems.items():
        for issue in issues:
            result.warnings.append(f"Efter fogar: {issue}")
        log.warning("Del %02d har problem efter fogbygget: %s", index, "; ".join(issues))

    return result
