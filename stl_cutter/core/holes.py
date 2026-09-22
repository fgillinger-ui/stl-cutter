"""Borra hål i en modell.

Ett hål är en solid som dras bort ur modellen: en cylinder, och för ett
skruvhål dessutom en försänkning i ytan så att skallen går i jämnt. Samma
booleanmotor som fogarna använder gör jobbet (`manifold3d`).

Ordning i pipelinen: ``ladda -> resize -> borra -> planera snitt -> kapa``.
Hålen borras alltså **före** snitten, av två skäl: ett hål kan hamna tvärs över
ett snitt och ska då finnas i båda delarna, och snittplaneringen ska se
modellen som den faktiskt blir.

**Riktningen.** Ett hål har en punkt och en riktning. Klickar man i 3D-vyn blir
riktningen ytans normal *inåt*; skriver man in siffror väljer man en axel.
Riktningen normeras alltid - en nollvektor är inget hål utan ett fel.

**Djupet.** ``depth_mm = 0`` betyder genomgående: cylindern görs då lika lång
som modellens diagonal, så att den går igenom oavsett var den börjar. Ett
angivet djup mäts från ytan och nedåt.

**Vad modulen inte gör.** Den kontrollerar inte att hålet lämnar tillräckligt
med gods runt sig. Det beror på material, last och skruv, och ett tal som ser
beräknat ut men inte är det är sämre än inget - se `core.load`. Däremot sägs
det rakt ut när ett hål inte träffar något material alls, för då är det med
säkerhet fel.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace

import numpy as np
import trimesh

from .joints.base import difference, union

log = logging.getLogger(__name__)

__all__ = [
    "SCREWS",
    "Hole",
    "HoleError",
    "Screw",
    "drill",
    "hole_solid",
    "screw_hole",
    "surface_hole",
]

#: Så många sidor får cylindern. 48 ger ett hål som känns runt i handen utan
#: att göra meshen onödigt tung.
CYLINDER_SECTIONS = 48

#: Hålet börjar en bit ovanför ytan, så att booleanen aldrig möts exakt
#: kant-i-kant - då blir resultatet opålitligt.
OVERSHOOT_MM = 0.5

#: Minsta hål som är meningsfullt att skriva ut på en FDM-skrivare.
MIN_DIAMETER_MM = 1.0


class HoleError(ValueError):
    """Hålet gick inte att borra, med ett skäl som går att åtgärda."""


@dataclass(frozen=True)
class Screw:
    """Mått för en metrisk skruv, i mm.

    Värdena är standardmått: fri passage (medel) enligt ISO 273, och skallen
    enligt ISO 10642 för försänkt insexskruv respektive ISO 4762 för cylindrisk
    insexskruv. De är avskrivna, inte uträknade.
    """

    name: str
    clearance_mm: float
    #: Försänkt skalle: diameter vid ytan och konvinkel (90° totalt).
    head_diameter_mm: float
    head_angle_deg: float = 90.0
    #: Cylindrisk skalle, för den som hellre vill ha planforsänkning.
    socket_diameter_mm: float = 0.0
    socket_depth_mm: float = 0.0

    @property
    def head_depth_mm(self) -> float:
        """Hur djup konen blir när den ska rymma hela skallen."""
        half = math.radians(self.head_angle_deg / 2.0)
        return (self.head_diameter_mm - self.clearance_mm) / 2.0 / math.tan(half)


#: Skruvarna som går att välja. M3-M6 täcker det mesta i en utskriven hylla.
SCREWS: dict[str, Screw] = {
    "M3": Screw("M3", 3.4, 6.0, socket_diameter_mm=5.5, socket_depth_mm=3.0),
    "M4": Screw("M4", 4.5, 8.0, socket_diameter_mm=7.0, socket_depth_mm=4.0),
    "M5": Screw("M5", 5.5, 10.0, socket_diameter_mm=8.5, socket_depth_mm=5.0),
    "M6": Screw("M6", 6.6, 12.0, socket_diameter_mm=10.0, socket_depth_mm=6.0),
}


@dataclass
class Hole:
    """Ett hål: var det sitter, åt vilket håll det går och hur det ser ut.

    `point` ligger på ytan där hålet börjar och `direction` pekar in i
    materialet. `depth_mm = 0` betyder genomgående.
    """

    point: tuple[float, float, float]
    direction: tuple[float, float, float] = (0.0, 0.0, -1.0)
    diameter_mm: float = 4.5
    depth_mm: float = 0.0
    #: Försänkning för en skruvskalle. Tom sträng = rakt hål.
    screw: str = ""
    #: "countersink" (konisk, skallen går i jämnt) eller "counterbore"
    #: (cylindrisk ficka för en insexskalle).
    head: str = "countersink"
    note: str = ""

    @property
    def through(self) -> bool:
        return self.depth_mm <= 0.0

    @property
    def unit_direction(self) -> np.ndarray:
        vector = np.asarray(self.direction, dtype=float)
        length = float(np.linalg.norm(vector))
        if length < 1e-9:
            raise HoleError(
                "Hålet saknar riktning. Klicka på modellens yta eller välj en axel."
            )
        return vector / length

    def describe(self) -> str:
        x, y, z = self.point
        what = f"Ø {self.diameter_mm:.1f} mm"
        if self.screw:
            what = f"{self.screw} ({what})"
        how = "genomgående" if self.through else f"{self.depth_mm:.1f} mm djupt"
        head = ""
        if self.screw and self.head == "countersink":
            head = ", försänkt"
        elif self.screw and self.head == "counterbore":
            head = ", planförsänkt"
        return f"{what} vid ({x:.1f}, {y:.1f}, {z:.1f}), {how}{head}"

    def to_dict(self) -> dict:
        return {
            "point": [float(v) for v in self.point],
            "direction": [float(v) for v in self.direction],
            "diameter_mm": float(self.diameter_mm),
            "depth_mm": float(self.depth_mm),
            "screw": self.screw,
            "head": self.head,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Hole":
        return cls(
            point=tuple(float(v) for v in data["point"]),
            direction=tuple(float(v) for v in data.get("direction", (0.0, 0.0, -1.0))),
            diameter_mm=float(data.get("diameter_mm", 4.5)),
            depth_mm=float(data.get("depth_mm", 0.0)),
            screw=str(data.get("screw", "")),
            head=str(data.get("head", "countersink")),
            note=str(data.get("note", "")),
        )


def screw_hole(
    point,
    direction,
    screw: str,
    depth_mm: float = 0.0,
    head: str = "countersink",
) -> Hole:
    """Ett hål med rätt mått för en metrisk skruv."""
    if screw not in SCREWS:
        raise HoleError(
            f"Okänd skruv {screw!r}. Kända: {', '.join(SCREWS)}."
        )
    spec = SCREWS[screw]
    return Hole(
        point=tuple(float(v) for v in point),
        direction=tuple(float(v) for v in direction),
        diameter_mm=spec.clearance_mm,
        depth_mm=float(depth_mm),
        screw=screw,
        head=head,
    )


def _frame(direction: np.ndarray) -> np.ndarray:
    """En 4x4-matris som lägger +z längs `direction`."""
    return trimesh.geometry.align_vectors([0.0, 0.0, 1.0], direction)


def hole_solid(hole: Hole, span_mm: float) -> trimesh.Trimesh:
    """Soliden som ska dras bort ur modellen.

    `span_mm` är hur långt ett genomgående hål behöver vara - modellens
    diagonal räcker alltid, oavsett var hålet börjar och åt vilket håll det
    går.
    """
    if hole.diameter_mm < MIN_DIAMETER_MM:
        raise HoleError(
            f"Ø {hole.diameter_mm:.1f} mm är för litet för att skriva ut. "
            f"Minst {MIN_DIAMETER_MM:.1f} mm."
        )
    direction = hole.unit_direction
    length = float(span_mm if hole.through else hole.depth_mm) + OVERSHOOT_MM
    if length <= OVERSHOOT_MM:
        raise HoleError("Djupet måste vara större än noll.")

    # Cylindern byggs längs +z och vrids sedan på plats. Den börjar en bit
    # ovanför ytan, annars ligger boolean-ytorna exakt på varandra.
    shaft = trimesh.creation.cylinder(
        radius=hole.diameter_mm / 2.0, height=length, sections=CYLINDER_SECTIONS
    )
    shaft.apply_translation([0.0, 0.0, length / 2.0 - OVERSHOOT_MM])

    recess = _head_solid(hole)
    # Axel och försänkning överlappar varandra. De måste slås ihop med en
    # riktig union - läggs de bara i samma mesh blir den självskärande, och
    # booleanmotorn räknar då överlappet två gånger och tar bort för mycket.
    solid = shaft if recess is None else union([shaft, recess])
    solid.apply_transform(_frame(direction))
    solid.apply_translation(np.asarray(hole.point, dtype=float))
    return solid


def _head_solid(hole: Hole) -> trimesh.Trimesh | None:
    """Försänkningen för skruvskallen, i det lokala systemet (+z inåt)."""
    if not hole.screw:
        return None
    spec = SCREWS.get(hole.screw)
    if spec is None:
        raise HoleError(f"Okänd skruv {hole.screw!r}.")

    if hole.head == "counterbore":
        if spec.socket_diameter_mm <= 0:
            return None
        # Cylindrisk ficka: skallen sitter under ytan och kan gripas av nyckeln.
        depth = spec.socket_depth_mm
        bore = trimesh.creation.cylinder(
            radius=spec.socket_diameter_mm / 2.0,
            height=depth + OVERSHOOT_MM,
            sections=CYLINDER_SECTIONS,
        )
        bore.apply_translation([0.0, 0.0, (depth + OVERSHOOT_MM) / 2.0 - OVERSHOOT_MM])
        return bore

    if hole.head != "countersink":
        raise HoleError(
            f"Okänd typ av försänkning {hole.head!r}. Välj countersink eller counterbore."
        )

    # Konisk försänkning: bred vid ytan, smal nere vid hålet. Den byggs som ett
    # konvext hölje av två cirklar - en form som inte kan bli trasig.
    depth = spec.head_depth_mm
    angles = np.linspace(0.0, 2.0 * np.pi, CYLINDER_SECTIONS, endpoint=False)
    circle = np.column_stack([np.cos(angles), np.sin(angles)])
    top_radius = spec.head_diameter_mm / 2.0
    # Konen fortsätter en bit ovanför ytan, annars ligger kanten exakt i ytan.
    over = top_radius + OVERSHOOT_MM
    points = np.vstack(
        [
            np.column_stack(
                [circle[:, 0] * over, circle[:, 1] * over, np.full(len(circle), -OVERSHOOT_MM)]
            ),
            np.column_stack(
                [
                    circle[:, 0] * hole.diameter_mm / 2.0,
                    circle[:, 1] * hole.diameter_mm / 2.0,
                    np.full(len(circle), depth),
                ]
            ),
        ]
    )
    return trimesh.convex.convex_hull(points)


def surface_hole(
    mesh: trimesh.Trimesh,
    origin,
    ray_direction,
    diameter_mm: float = 4.5,
    depth_mm: float = 0.0,
    screw: str = "",
    head: str = "countersink",
) -> Hole:
    """Hålet som ett klick i 3D-vyn ger: träffpunkten och ytans normal inåt.

    Strålen kommer från kameran. Den första ytan den träffar är den man ser och
    pekar på; hålet borras vinkelrätt in i just den ytan, vilket är vad man
    menar när man pekar på en modell.
    """
    origin = np.asarray(origin, dtype=float).reshape(1, 3)
    ray_direction = np.asarray(ray_direction, dtype=float).reshape(1, 3)

    points, _, faces = mesh.ray.intersects_location(
        ray_origins=origin, ray_directions=ray_direction, multiple_hits=False
    )
    if len(points) == 0:
        raise HoleError("Strålen träffade inte modellen - klicka på en yta.")

    point = np.asarray(points[0], dtype=float)
    normal = np.asarray(mesh.face_normals[int(faces[0])], dtype=float)
    # Normalen pekar ut ur modellen; hålet ska gå in i den.
    inward = -normal
    if float(np.dot(inward, np.asarray(ray_direction).ravel())) < 0:
        inward = normal
    if screw:
        # Skruven bestämmer diametern - annars vore det inte ett M4-hål.
        return screw_hole(point, inward, screw, depth_mm=depth_mm, head=head)
    return Hole(
        point=tuple(point),
        direction=tuple(inward),
        diameter_mm=float(diameter_mm),
        depth_mm=float(depth_mm),
        screw="",
        head=head,
    )


def drill(mesh: trimesh.Trimesh, holes, progress=None) -> trimesh.Trimesh:
    """Borra alla hål och returnera den nya modellen.

    Hålen dras bort i ett svep. Ett hål som inte tar bort något material är
    alltid ett misstag - det ligger bredvid modellen, eller åt fel håll - och
    det sägs rakt ut i stället för att tigande ge tillbaka samma modell.
    """
    from .progress import report

    holes = list(holes)
    if not holes:
        return mesh

    span = float(np.linalg.norm(mesh.extents)) + 2.0 * OVERSHOOT_MM
    solids = []
    for number, hole in enumerate(holes, start=1):
        report(progress, 0.8 * (number - 1) / len(holes), f"Bygger hål {number} av {len(holes)}")
        if not _goes_into_material(mesh, hole):
            raise HoleError(
                f"Hål {number} ({hole.describe()}) går inte in i modellen. "
                "Punkten ligger utanför materialet, eller riktningen pekar ut "
                "ur det i stället för in."
            )
        solids.append(hole_solid(hole, span))

    report(progress, 0.85, "Borrar")
    before = float(abs(mesh.volume))
    try:
        drilled = difference([mesh, *solids])
    except Exception as error:  # boolean-motorn ger olika fel per backend
        raise HoleError(f"Hålen gick inte att borra: {error}") from error

    after = float(abs(drilled.volume))
    if before - after <= 1e-6:
        raise HoleError(
            "Inget material togs bort. Hålen ligger utanför modellen eller "
            "pekar åt fel håll - kontrollera punkt och riktning."
        )
    report(progress, 1.0, f"Klar - {len(holes)} hål")
    log.info(
        "Borrade %d hål: volymen minskade %.1f mm³ (%.2f %%)",
        len(holes),
        before - after,
        100.0 * (before - after) / before if before else 0.0,
    )
    return drilled


#: Hur långt in i materialet hålets riktning provas. Kort, så att även ett
#: grunt blindhål hinner vara inne i godset.
PROBE_MM = 0.5


def _goes_into_material(mesh: trimesh.Trimesh, hole: Hole) -> bool:
    """Leder hålet in i material, eller bort från det?

    Ett hål som pekar ut ur modellen tar bara bort en hårfin skiva vid ytan,
    eftersom nyckeln börjar en aning ovanför den. Det syns knappt i volymen men
    är alltid ett misstag, så det provas per hål: ligger en punkt strax innanför
    startpunkten inuti modellen?

    En modell med hål i ytan går inte att svara på - då får booleanen avgöra,
    och den totala volymkontrollen fångar det uppenbara.
    """
    step = PROBE_MM if hole.through else min(PROBE_MM, hole.depth_mm / 2.0)
    probe = np.asarray(hole.point, dtype=float) + hole.unit_direction * step
    if not mesh.is_watertight:
        return True
    try:
        return bool(mesh.contains(probe.reshape(1, 3))[0])
    except Exception as error:  # pragma: no cover - beror på backend
        log.debug("Kunde inte prova hålets riktning (%s) - låter booleanen avgöra.", error)
        return True


def axis_direction(axis: int, positive: bool = False) -> tuple[float, float, float]:
    """Riktningen för ett hål som borras längs en axel."""
    vector = [0.0, 0.0, 0.0]
    vector[int(axis)] = 1.0 if positive else -1.0
    return tuple(vector)


def with_screw(hole: Hole, screw: str, head: str | None = None) -> Hole:
    """Samma hål, men med måtten för en annan skruv."""
    if not screw:
        return replace(hole, screw="")
    if screw not in SCREWS:
        raise HoleError(f"Okänd skruv {screw!r}. Kända: {', '.join(SCREWS)}.")
    return replace(
        hole,
        screw=screw,
        diameter_mm=SCREWS[screw].clearance_mm,
        head=head or hole.head,
    )
