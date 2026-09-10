"""Pusselfog.

En 2D-profil längs snittkonturens långa riktning, extruderad genom hela
tjockleken. Till skillnad från övriga fogtyper adderas ingen nyckel - i stället
ersätts det plana snittet av ett prisma vars ovansida följer profilen:

    del A = originalet ∩ prismat
    del B = originalet − (prismat uppförstorat med clearance)

Två profiler finns: `sine` (vågform) och `keyhole` (klassisk pusselbit med
undersnitt som låser delarna mot dragkraft).
"""

from __future__ import annotations

import logging
import numpy as np
import shapely
import trimesh
from shapely.geometry import Point, Polygon

from .base import (
    OVERLAP_MM,
    JointBuilder,
    JointError,
    JointParams,
    _clean,
    aligned_frame,
    contact_region,
    difference,
    intersection,
    largest_polygon,
    union,
)

log = logging.getLogger(__name__)

#: Antal punkter per våglängd när sinusprofilen samplas.
SAMPLES_PER_PERIOD = 24


def sine_profile(
    u_min: float, u_max: float, amplitude: float, period: float, depth: float
) -> Polygon:
    """Vågprofil i (u, n)-planet, sluten nedåt till n = -depth."""
    period = max(period, 2.0)
    count = max(8, int(SAMPLES_PER_PERIOD * (u_max - u_min) / period))
    us = np.linspace(u_min, u_max, count)
    ns = amplitude * np.sin(2.0 * np.pi * (us - u_min) / period)
    top = list(zip(us.tolist(), ns.tolist()))
    return Polygon([*top, (u_max, -depth), (u_min, -depth)])


def keyhole_profile(
    u_min: float, u_max: float, amplitude: float, period: float, depth: float
) -> Polygon:
    """Pusselbitsprofil: runda tappar som växlar sida, med undersnitt."""
    period = max(period, 4.0)
    base = shapely.box(u_min, -depth, u_max, 0.0)
    radius = max(amplitude, 1.0)
    span = u_max - u_min
    count = max(1, int(span / period))
    step = span / (count + 1)

    tabs, notches = [], []
    for i in range(count):
        centre_u = u_min + step * (i + 1)
        # Centrum en bit ovanför linjen ger en hals som är smalare än huvudet.
        head = Point(centre_u, 0.35 * radius).buffer(radius, quad_segs=16)
        (tabs if i % 2 == 0 else notches).append(head)

    profile = base
    if tabs:
        profile = shapely.union_all([profile, *tabs])
    for notch in notches:
        profile = profile.difference(
            shapely.affinity.translate(notch, yoff=-0.7 * radius)
        )
    result = largest_polygon(profile)
    if result is None:
        raise JointError("Pusselprofilen gick inte att bygga.")
    return result


def prism_along_v(polygon: Polygon, v_min: float, v_max: float) -> trimesh.Trimesh:
    """Extrudera en (u, n)-polygon längs v i det lokala systemet."""
    solid = trimesh.creation.extrude_polygon(polygon, height=float(v_max - v_min))
    # Prismat byggs i (x, y, z) = (u, n, v) och roteras till (u, v, n).
    transform = np.array(
        [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, float(v_min)],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
    )
    solid.apply_transform(transform)
    return _clean(solid)


class PuzzleJoint(JointBuilder):
    joint_type = "puzzle"
    fallback = "pins"

    def profile(self, u_min, u_max, params: JointParams, depth: float) -> Polygon:
        if params.profile == "keyhole":
            return keyhole_profile(u_min, u_max, params.amplitude_mm, params.period_mm, depth)
        return sine_profile(u_min, u_max, params.amplitude_mm, params.period_mm, depth)

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
        if largest_polygon(region) is None or region.area <= 1e-6:
            raise JointError("Kontaktytan är för liten för en pusselfog.")

        # Prismat ersätter hela snittet, så det måste täcka ALLA öar i
        # kontaktytan. Räknar vi bara på den största lämnas resten av del A
        # kvar inuti del B.
        minx, miny, maxx, maxy = region.bounds
        # Hur långt in i respektive del profilen får sträcka sig.
        local_a = np.asarray(mesh_a.copy().apply_transform(frame.to_local).bounds)
        local_b = np.asarray(mesh_b.copy().apply_transform(frame.to_local).bounds)
        reach_a = abs(float(local_a[0][2]))
        reach_b = abs(float(local_b[1][2]))
        amplitude = min(params.amplitude_mm, reach_a / 3.0, reach_b / 3.0)
        if amplitude < 0.5:
            raise JointError("Delarna är för korta för en pusselprofil.")
        params = JointParams(**{**params.__dict__, "amplitude_mm": amplitude})

        depth = reach_a + OVERLAP_MM
        profile = self.profile(minx - 1.0 + offset[0], maxx + 1.0 + offset[0], params, depth)
        grown = profile.buffer(params.clearance_mm, join_style=1)

        margin = OVERLAP_MM + params.clearance_mm
        solid = prism_along_v(profile, miny - margin, maxy + margin)
        solid_grown = prism_along_v(grown, miny - margin, maxy + margin)

        whole = union([mesh_a, mesh_b])
        out_a = intersection([whole, frame.place(solid)])
        out_b = difference([whole, frame.place(solid_grown)])
        return out_a, out_b
