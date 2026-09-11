"""Laxstjärt (dovetail).

Ett trapetsprisma som är bredare längst ut än vid basen, så att delarna inte
kan dras isär vinkelrätt mot snittet - bara skjutas ihop i planet.

Riktningar i det lokala systemet: `u` är kontaktytans långa riktning och
fördelningsriktning för flera laxstjärtar, `v` är den korta riktningen och
tillika glidriktningen, `n` (z) pekar in i del B.
"""

from __future__ import annotations

import logging
import math

import numpy as np
from shapely.geometry import Polygon

from .base import (
    OVERLAP_MM,
    JointBuilder,
    JointError,
    JointParams,
    intersection,
    largest_polygon,
    prism_from_polygon,
)

log = logging.getLogger(__name__)

#: Minsta halsbredd som är meningsfull att skriva ut.
MIN_WIDTH_MM = 4.0

#: Under den här tjockleken på snittet är en laxstjärt meningslös.
MIN_THICKNESS_MM = 6.0

#: Stoppkanten får ta högst så här stor del av inskjutningslängden.
MAX_STOP_FRACTION = 0.4

#: Kortare än så här går laxstjärten inte att skjuta in.
MIN_SLIDE_LENGTH_MM = 4.0


def _trapezoid(centre_u: float, width: float, depth: float, angle_deg: float) -> Polygon:
    """Laxstjärtens tvärsnitt i (u, n)-planet. Bredare vid n = depth."""
    flare = depth * math.tan(math.radians(angle_deg))
    half = width / 2.0
    return Polygon(
        [
            (centre_u - half, 0.0),
            (centre_u + half, 0.0),
            (centre_u + half + flare, depth),
            (centre_u - half - flare, depth),
        ]
    )


def fit_dovetails(
    u_span: float, count: int, width: float, depth: float, angle_deg: float
) -> tuple[int, float]:
    """Minska antal och bredd tills laxstjärtarna får plats längs `u`."""
    flare = depth * math.tan(math.radians(angle_deg))
    count = max(1, int(count))
    while count > 1 and count * (width + 2 * flare) > 0.8 * u_span:
        count -= 1
    footprint = width + 2 * flare
    if count * footprint > 0.8 * u_span:
        width = max(MIN_WIDTH_MM, 0.8 * u_span / count - 2 * flare)
    if width < MIN_WIDTH_MM:
        raise JointError(
            f"Kontaktytan är för smal ({u_span:.1f} mm) för en laxstjärt."
        )
    return count, width


class DovetailJoint(JointBuilder):
    joint_type = "dovetail"
    fallback = "pins"

    def keys(
        self,
        region: Polygon,
        params: JointParams,
        grow: float = 0.0,
        offset: tuple[float, float] = (0.0, 0.0),
    ):
        minx, miny, maxx, maxy = region.bounds
        u_span, v_span = maxx - minx, maxy - miny
        if v_span < MIN_THICKNESS_MM:
            raise JointError(
                f"Snittet är bara {v_span:.1f} mm tjockt - för tunt för en laxstjärt."
            )
        # Laxstjärten får inte sticka ut genom del B, gröpa ur del A, eller
        # göra delen för stor för byggplattan.
        depth = min(
            params.depth_mm,
            max(u_span, v_span),
            0.5 * self.reach_b,
            params.max_protrusion_mm,
        )
        depth = self.material_depth(region, depth)
        if depth < 2.0:
            raise JointError(
                f"Bara {depth:.1f} mm material att fästa i - för lite för en laxstjärt."
            )
        count, width = fit_dovetails(
            u_span, params.count, params.width_mm, depth, params.angle_deg
        )

        chamfer = min(params.chamfer_mm, width / 4.0, depth / 4.0)
        # Honan öppnas mot sidorna så att laxstjärten går att skjuta in.
        clip = region.buffer(2.0) if grow > 0 else region
        clip = largest_polygon(clip)
        if clip is None:
            raise JointError("Kontaktytan gick inte att tolka.")

        # Stoppkant: spåret stängs i bortre änden, och laxstjärten görs lika
        # mycket kortare. Då glider den in och tar emot mot material i stället
        # för att bara hållas på plats av friktion.
        stop = min(max(params.stop_mm, 0.0), MAX_STOP_FRACTION * v_span)
        v_start = miny + offset[1]
        v_end = maxy + offset[1] - stop
        if grow > 0:
            # Honan öppnas bara i den ände laxstjärten skjuts in från.
            v_start -= OVERLAP_MM
            v_end += grow
        if v_end - v_start < MIN_SLIDE_LENGTH_MM:
            raise JointError(
                f"Bara {max(v_end - v_start, 0.0):.1f} mm att skjuta in laxstjärten på."
            )

        solids = []
        for i in range(count):
            centre_u = minx + u_span * (i + 1) / (count + 1) + offset[0]
            profile = _trapezoid(centre_u, width + 2 * grow, depth, params.angle_deg)
            if grow > 0:
                profile = profile.buffer(grow, join_style=2)
            solid = self._prism_along_v(
                profile,
                v_start,
                v_end,
                chamfer if grow == 0 else 0.0,
                chamfer_both_ends=stop <= 0.0,
            )
            solid = intersection([solid, prism_from_polygon(clip, -OVERLAP_MM, depth + 1.0)])
            solids.append(solid)
        return solids

    @staticmethod
    def _prism_along_v(
        profile: Polygon,
        v_min: float,
        v_max: float,
        chamfer: float,
        chamfer_both_ends: bool = True,
    ) -> "np.ndarray":
        """Extrudera tvärsnittet längs v, med fas i ingångsänden.

        Tvärsnittet är konvext, så prismat byggs som ett konvext hölje av
        nivåerna - det kan inte bli en trasig mesh. Med en stoppkant fasas bara
        den ände laxstjärten skjuts in från; den bortre ska ta emot plant.
        """
        import trimesh

        from .base import _clean

        levels: list[tuple[Polygon, float]] = []
        if chamfer > 0:
            inset = profile.buffer(-chamfer, join_style=2)
            if inset.is_empty or inset.area <= 1e-9:
                chamfer = 0.0
            else:
                levels.append((inset, v_min))
                levels.append((profile, v_min + chamfer))
                if chamfer_both_ends:
                    levels.append((profile, v_max - chamfer))
                    levels.append((inset, v_max))
                else:
                    levels.append((profile, v_max))
        if not levels:
            levels = [(profile, v_min), (profile, v_max)]

        points = []
        for polygon, v in levels:
            coords = np.asarray(polygon.exterior.coords)[:-1]
            # (u, n) i tvärsnittet -> (u, v, n) i det lokala systemet
            points.append(np.column_stack([coords[:, 0], np.full(len(coords), v), coords[:, 1]]))
        hull = trimesh.convex.convex_hull(np.vstack(points))
        return _clean(hull)
