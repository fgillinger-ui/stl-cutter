"""Skruvfog med M3-mutter.

Del A får ett genomgående hål med försänkning för skruvskallen. Del B får en
sexkantsficka för muttern vid snittytan plus ett hål för skruvspetsen - muttern
läggs i fickan innan delarna sätts ihop. Två styrpinnar centrerar delarna så att
hålen möts.

Detta är den enda fogtypen som är avsedd att kunna tas isär igen.
"""

from __future__ import annotations

import logging
import math

import numpy as np
import trimesh
from shapely.geometry import Polygon

from .base import (
    OVERLAP_MM,
    JointBuilder,
    JointError,
    JointParams,
    aligned_frame,
    chamfered_cylinder,
    contact_region,
    difference,
    largest_polygon,
    union,
)
from .pins import placement_points

log = logging.getLogger(__name__)

#: Hur långt förbi muttern skruvspetsen får sticka.
SCREW_RUNOUT_MM = 6.0


def cylinder_along_n(radius: float, z_min: float, z_max: float, sections: int = 32):
    """Cylinder längs n mellan två nivåer, i lokala koordinater."""
    solid = trimesh.creation.cylinder(
        radius=float(radius), height=float(z_max - z_min), sections=sections
    )
    solid.apply_translation([0.0, 0.0, float((z_min + z_max) / 2.0)])
    return solid


def hexagon(across_flats: float) -> Polygon:
    """Sexkant definierad av nyckelvidden (avstånd mellan motstående sidor)."""
    radius = across_flats / (2.0 * math.cos(math.radians(30.0)))
    angles = np.radians(np.arange(6) * 60.0 + 30.0)
    return Polygon(np.column_stack([radius * np.cos(angles), radius * np.sin(angles)]))


class ScrewJoint(JointBuilder):
    joint_type = "screw"
    fallback = "pins"

    def build(
        self,
        mesh_a: trimesh.Trimesh,
        mesh_b: trimesh.Trimesh,
        plane,
        params: JointParams,
        offset: tuple[float, float] = (0.0, 0.0),
    ):
        region, frame = contact_region(mesh_a, mesh_b, plane.origin, plane.normal)
        if region is None:
            raise JointError("Delarna har ingen gemensam kontaktyta.")
        frame, region = aligned_frame(region, frame)
        region = largest_polygon(region)
        if region is None or region.area <= 1e-6:
            raise JointError("Kontaktytan är för liten för en skruvfog.")

        local_a = np.asarray(mesh_a.copy().apply_transform(frame.to_local).bounds)
        local_b = np.asarray(mesh_b.copy().apply_transform(frame.to_local).bounds)
        a_depth = abs(float(local_a[0][2]))
        b_depth = abs(float(local_b[1][2]))

        needed = params.nut_depth_mm + params.clearance_mm + 2.0
        if b_depth < needed or a_depth < params.counterbore_depth_mm + 2.0:
            raise JointError(
                f"Delarna är för korta ({a_depth:.1f}/{b_depth:.1f} mm) för en M3-skruv."
            )

        screws = max(1, int(params.count))
        pins = max(0, int(params.guide_pins))
        # Varva skruvar och styrpinnar längs kontaktytans långa riktning.
        radius = max(params.counterbore_diameter_mm, params.nut_across_flats_mm) / 2.0
        points = placement_points(
            region, screws + pins, radius, params.edge_margin_mm, offset
        )
        screw_points = points[0::2][:screws]
        pin_points = points[1::2][:pins]
        if not screw_points:
            raise JointError("Ingen plats för skruvar i kontaktytan.")

        clearance = params.clearance_mm
        cuts_a, cuts_b, keys = [], [], []

        for x, y in screw_points:
            through = cylinder_along_n(
                params.hole_diameter_mm / 2.0 + clearance,
                -a_depth - OVERLAP_MM,
                OVERLAP_MM,
            )
            counterbore = cylinder_along_n(
                params.counterbore_diameter_mm / 2.0 + clearance,
                -a_depth - OVERLAP_MM,
                -a_depth + params.counterbore_depth_mm,
            )
            for solid in (through, counterbore):
                solid.apply_translation([x, y, 0.0])
                cuts_a.append(solid)

            nut = trimesh.creation.extrude_polygon(
                hexagon(params.nut_across_flats_mm + 2.0 * clearance),
                height=params.nut_depth_mm + clearance,
            )
            nut.apply_translation([x, y, -OVERLAP_MM])
            tip = cylinder_along_n(
                params.hole_diameter_mm / 2.0 + clearance,
                -OVERLAP_MM,
                min(params.nut_depth_mm + SCREW_RUNOUT_MM, b_depth - 0.5),
            )
            tip.apply_translation([x, y, 0.0])
            cuts_b.extend([nut, tip])

        pin_radius = params.guide_pin_diameter_mm / 2.0
        pin_length = min(2.5 * params.guide_pin_diameter_mm, b_depth - 1.0)
        for x, y in pin_points:
            pin = chamfered_cylinder(pin_radius, OVERLAP_MM + pin_length, 0.5)
            pin.apply_translation([x, y, -OVERLAP_MM])
            keys.append(pin)

            hole = chamfered_cylinder(
                pin_radius + clearance,
                OVERLAP_MM + pin_length + params.extra_hole_depth_mm,
                0.0,
            )
            hole.apply_translation([x, y, -OVERLAP_MM])
            cuts_b.append(hole)

        out_a = difference([mesh_a, union([frame.place(c) for c in cuts_a])])
        if keys:
            out_a = union([out_a, union([frame.place(k) for k in keys])])
        out_b = difference([mesh_b, union([frame.place(c) for c in cuts_b])])
        return out_a, out_b
