"""Foggeometri.

`build_joint()` är ingången: den väljer rätt byggare, kontrollerar resultatet
och faller tillbaka på enklare fogtyper om något går fel. Den kraschar aldrig
utan resultat - i värsta fall returneras delarna oförändrade med ett plant
snitt och en varning.
"""

from __future__ import annotations

import logging

import trimesh

from .base import (
    JointBuilder,
    JointError,
    JointParams,
    JointResult,
    PlaneFrame,
    check_mesh,
    contact_region,
    engine_name,
    validate_parts,
)
from .dovetail import DovetailJoint
from .pins import PinsJoint
from .puzzle import PuzzleJoint
from .screw import ScrewJoint

log = logging.getLogger(__name__)


class NoJoint(JointBuilder):
    """Plan limfog - delarna lämnas som de är."""

    joint_type = "none"
    fallback = None

    def build(self, mesh_a, mesh_b, plane, params, offset=(0.0, 0.0)):
        return mesh_a, mesh_b


BUILDERS: dict[str, type[JointBuilder]] = {
    "none": NoJoint,
    "pins": PinsJoint,
    "dovetail": DovetailJoint,
    "puzzle": PuzzleJoint,
    "screw": ScrewJoint,
}

#: Hur mycket den sammanlagda volymen får ändras av en fog.
VOLUME_UPPER = 1.02
VOLUME_LOWER = 0.50

#: Hur mycket EN del får ändra volym. Fogar flyttar bara små mängder material
#: mellan delarna; en större förändring betyder att något gått fel - till
#: exempel att en del svalt en bit av grannen.
PART_VOLUME_SHIFT = 0.25

#: Förskjutning som provas om fogen inte går att bygga i sitt första läge.
RETRY_OFFSET_MM = 0.5

__all__ = [
    "BUILDERS",
    "DovetailJoint",
    "JointBuilder",
    "JointError",
    "JointParams",
    "JointResult",
    "NoJoint",
    "PinsJoint",
    "PlaneFrame",
    "PuzzleJoint",
    "ScrewJoint",
    "build_joint",
    "check_mesh",
    "contact_region",
    "engine_name",
    "validate_parts",
]


def get_builder(joint_type: str) -> JointBuilder:
    builder = BUILDERS.get(joint_type)
    if builder is None:
        raise JointError(f"Okänd fogtyp {joint_type!r}. Kända: {', '.join(BUILDERS)}")
    return builder()


def _validate(
    out_a: trimesh.Trimesh,
    out_b: trimesh.Trimesh,
    volume_a: float,
    volume_b: float,
) -> list[str]:
    """Kontrollera resultatet av ett fogbygge."""
    problems = check_mesh(out_a, "del A") + check_mesh(out_b, "del B")
    if problems:
        return problems

    original_volume = volume_a + volume_b
    total = float(abs(out_a.volume) + abs(out_b.volume))
    if original_volume > 0:
        ratio = total / original_volume
        if ratio > VOLUME_UPPER:
            problems.append(f"volymen ökade {(ratio - 1) * 100:.1f} %")
        elif ratio < VOLUME_LOWER:
            problems.append(f"volymen minskade {(1 - ratio) * 100:.1f} %")

    for label, before, after in (
        ("del A", volume_a, float(abs(out_a.volume))),
        ("del B", volume_b, float(abs(out_b.volume))),
    ):
        if before <= 0:
            continue
        shift = abs(after - before) / before
        if shift > PART_VOLUME_SHIFT:
            problems.append(f"{label} ändrade volym {shift * 100:.0f} %")
    return problems


def _attempt(
    builder: JointBuilder,
    mesh_a: trimesh.Trimesh,
    mesh_b: trimesh.Trimesh,
    plane,
    params: JointParams,
    offset: tuple[float, float],
    volumes: tuple[float, float],
) -> tuple[trimesh.Trimesh, trimesh.Trimesh]:
    out_a, out_b = builder.build(mesh_a, mesh_b, plane, params, offset=offset)
    problems = _validate(out_a, out_b, *volumes)
    if problems:
        raise JointError("; ".join(problems))
    return out_a, out_b


def _add_guide_pins(
    mesh_a: trimesh.Trimesh,
    mesh_b: trimesh.Trimesh,
    plane,
    params: JointParams,
    volumes: tuple[float, float],
) -> tuple[trimesh.Trimesh, trimesh.Trimesh, str | None]:
    """Komplettera en fog med styrpinnar på den kvarvarande plana ytan."""
    pin_params = JointParams(
        joint_type="pins",
        clearance_mm=params.clearance_mm,
        count=params.guide_pins,
        diameter_mm=params.guide_pin_diameter_mm,
        length_mm=min(2.5 * params.guide_pin_diameter_mm, 15.0),
        edge_margin_mm=params.edge_margin_mm,
    )
    try:
        out_a, out_b = _attempt(
            PinsJoint(), mesh_a, mesh_b, plane, pin_params, (0.0, 0.0), volumes
        )
        return out_a, out_b, None
    except (JointError, ValueError) as exc:
        return mesh_a, mesh_b, f"styrpinnarna kunde inte läggas till ({exc})"


def build_joint(
    mesh_a: trimesh.Trimesh,
    mesh_b: trimesh.Trimesh,
    plane,
    params: JointParams,
) -> JointResult:
    """Bygg en fog mellan två delar, med fallback-kedja.

    Ordning vid problem:
    1. städa indata (`merge_vertices`/`process`) och försök igen,
    2. förskjut fogen 0,5 mm,
    3. fall tillbaka på en enklare fogtyp och logga en varning.

    Delarna returneras alltid - i värsta fall oförändrade.
    """
    volumes = (float(abs(mesh_a.volume)), float(abs(mesh_b.volume)))
    warnings: list[str] = []
    attempts: list[str] = []
    requested = params.joint_type
    joint_type = requested

    while joint_type is not None:
        if joint_type == "none":
            return JointResult(
                mesh_a, mesh_b, "none", requested, applied=False,
                warnings=warnings, attempts=attempts,
            )

        builder = get_builder(joint_type)
        active = JointParams(**{**params.__dict__, "joint_type": joint_type})

        strategies = [
            ("direkt", mesh_a, mesh_b, (0.0, 0.0)),
            ("städade meshar", mesh_a.copy().process(validate=True),
             mesh_b.copy().process(validate=True), (0.0, 0.0)),
            ("förskjuten 0,5 mm", mesh_a, mesh_b, (RETRY_OFFSET_MM, RETRY_OFFSET_MM)),
        ]

        for label, a_in, b_in, offset in strategies:
            try:
                out_a, out_b = _attempt(
                    builder, a_in, b_in, plane, active, offset, volumes
                )
            except (JointError, ValueError, IndexError, ZeroDivisionError) as exc:
                attempts.append(f"{joint_type} ({label}): {exc}")
                log.debug("Fog %s misslyckades (%s): %s", joint_type, label, exc)
                continue

            attempts.append(f"{joint_type} ({label}): OK")
            if label != "direkt":
                warnings.append(f"{joint_type} byggdes först efter försök: {label}")

            # Pinnar och skruvfogar har redan styrning, och pusselfogens vågiga
            # skarv lämnar ingen plan yta att sätta pinnar i.
            if active.guide_pins > 0 and joint_type not in ("pins", "screw", "puzzle"):
                out_a, out_b, pin_warning = _add_guide_pins(
                    out_a, out_b, plane, active, (float(abs(out_a.volume)), float(abs(out_b.volume)))
                )
                if pin_warning:
                    warnings.append(pin_warning)

            return JointResult(
                out_a, out_b, joint_type, requested, applied=True,
                warnings=warnings, attempts=attempts,
            )

        fallback = builder.fallback
        if fallback is None:
            warnings.append(
                f"Fogen {joint_type!r} gick inte att bygga - snittet lämnas plant."
            )
            log.warning(warnings[-1])
            return JointResult(
                mesh_a, mesh_b, "none", requested, applied=False,
                warnings=warnings, attempts=attempts,
            )

        warnings.append(f"Fogen {joint_type!r} gick inte att bygga - provar {fallback!r}.")
        log.warning(warnings[-1])
        joint_type = fallback

    return JointResult(
        mesh_a, mesh_b, "none", requested, applied=False, warnings=warnings, attempts=attempts
    )
