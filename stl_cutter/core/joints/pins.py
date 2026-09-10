"""Styrpinnar (dowels).

Cylindriska pinnar med fasad topp sitter på del A; del B får hål med
`clearance_mm` per sida och 0,3 mm extra djup så att pinnen alltid bottnar
mot luft och inte mot hålets botten.
"""

from __future__ import annotations

import logging

import numpy as np
import shapely
from shapely.geometry import Point, Polygon

from .base import (
    OVERLAP_MM,
    JointBuilder,
    JointError,
    JointParams,
    chamfered_cylinder,
    largest_polygon,
)

log = logging.getLogger(__name__)


def placement_points(
    region: Polygon,
    count: int,
    radius: float,
    edge_margin: float,
    offset: tuple[float, float] = (0.0, 0.0),
) -> list[tuple[float, float]]:
    """Placera `count` punkter symmetriskt inuti konturen.

    Konturen krymps med `shapely.buffer(-(marginal + radie))` så att pinnens
    kant garanterat håller marginalen till snittytans kant.
    """
    shrunk = largest_polygon(region.buffer(-(edge_margin + radius)))
    if shrunk is None or shrunk.is_empty or shrunk.area <= 1e-9:
        # Sista försöket: bara pinnens radie, utan extra marginal.
        shrunk = largest_polygon(region.buffer(-radius))
    if shrunk is None or shrunk.is_empty or shrunk.area <= 1e-9:
        raise JointError(
            f"Ingen plats för pinnar med diameter {2 * radius:.1f} mm i kontaktytan."
        )

    rectangle = shrunk.minimum_rotated_rectangle
    coords = np.asarray(rectangle.exterior.coords)[:4]
    edges = coords[1:] - coords[:-1]
    lengths = np.linalg.norm(edges, axis=1)
    long_edge = edges[int(np.argmax(lengths))]
    direction = long_edge / max(np.linalg.norm(long_edge), 1e-9)
    span = float(lengths.max())
    centre = np.asarray(shrunk.centroid.coords[0], dtype=float)

    points: list[tuple[float, float]] = []
    for i in range(count):
        t = (i + 1) / (count + 1) - 0.5
        candidate = centre + direction * (span * t) + np.asarray(offset, dtype=float)
        point = Point(candidate)
        if not shrunk.contains(point):
            point = shapely.ops.nearest_points(shrunk, point)[0]
        xy = (float(point.x), float(point.y))
        if any(np.hypot(xy[0] - p[0], xy[1] - p[1]) < 2.0 * radius + 0.5 for p in points):
            continue  # för nära en pinne vi redan lagt - hoppa över
        points.append(xy)

    if not points:
        raise JointError("Kunde inte placera några pinnar i kontaktytan.")
    return points


class PinsJoint(JointBuilder):
    joint_type = "pins"
    fallback = None  # enklast möjliga mekaniska fog - faller tillbaka till ingen fog

    def keys(
        self,
        region: Polygon,
        params: JointParams,
        grow: float = 0.0,
        offset: tuple[float, float] = (0.0, 0.0),
    ):
        radius = params.diameter_mm / 2.0
        points = placement_points(
            region, max(1, params.count), radius, params.edge_margin_mm, offset
        )

        # Hålet blir clearance större i radie och 0,3 mm djupare än pinnen.
        extra_depth = params.extra_hole_depth_mm if grow > 0 else 0.0
        # Pinnen får inte gå igenom del B.
        pin_length = min(params.length_mm, max(self.reach_b - 1.0, 0.0))
        if pin_length < 2.0:
            raise JointError("Del B är för kort för en styrpinne.")
        length = OVERLAP_MM + pin_length + extra_depth
        chamfer = 0.0 if grow > 0 else min(0.6, radius * 0.4)

        solids = []
        for x, y in points:
            pin = chamfered_cylinder(radius + grow, length, chamfer)
            pin.apply_translation([x, y, -OVERLAP_MM])
            solids.append(pin)
        return solids
