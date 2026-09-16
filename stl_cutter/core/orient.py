"""Vända en kapad del så att den ligger platt på byggplattan.

En kapad del hamnar i den orientering den råkade ha i modellen, och den är
nästan aldrig rätt för skrivaren. Två saker blir fel:

**Stöd.** En del som står på högkant har överhäng överallt. I ett verkligt
fall gick 23,7 g av 89,4 g filament till stöd - en fjärdedel av plasten - som
sedan skulle brytas bort.

**Hållfastheten.** Lagren läggs vågrätt. En list som står upp får alla lager
tvärs sin längd, och böjs den drar lasten isär lager från lager - den riktning
där FDM är svagast. Samma list liggande får lagren längs sig och blir flera
gånger starkare. För en hylla som ska bära något är det inte en detalj.

Båda botas av samma sak: lägg delen platt, alltså med sitt minsta mått uppåt.

**Vad modulen medvetet inte gör.** Den provar bara de sex axelriktade lägena,
inte godtyckliga vinklar. Det är en begränsning med skäl. Ett försök med fria
riktningar från konvexa höljet mätte "överhängsyta" och valde snedställda
lägen där delen balanserar på en spets: formellt noll överhäng, i praktiken
oskrivbart. Måttet kunde inte skilja ett vågrätt tak - som skrivaren
**bryggar** utan stöd - från en 60-gradig lutning som hänger. För delar kapade
ur en CAD-modell är svaret ändå alltid ett av de sex lägena, och bland dem går
det att välja rätt utan att gissa. En organisk modell kan ha ett bättre snett
läge; det får slicerns egen auto-orientering hitta.

Ordlista:

``bygghöjd``
    Delens mått uppåt i ett givet läge. Lägst bygghöjd = plattast.
``anliggning``
    Hur stor yta som vilar mot plattan. Skiljer två lägen med samma höjd.
"""

from __future__ import annotations

import logging

import numpy as np
import trimesh

log = logging.getLogger(__name__)

__all__ = [
    "AXIS_POSES",
    "contact_area",
    "flat_transform",
    "lay_flat",
    "describe_orientation",
]

#: Hur nära plattan en punkt måste ligga för att räknas som anliggande, i mm.
#: Ungefär en lagertjocklek - närmare än så går inte att mäta meningsfullt.
CONTACT_TOLERANCE_MM = 0.2

#: De sex axelriktade lägena, som (axel att lägga nedåt, tecken). Det första
#: är delen som den redan ligger, så ett oförändrat läge vinner alla lika.
AXIS_POSES = ((2, -1.0), (2, 1.0), (0, -1.0), (0, 1.0), (1, -1.0), (1, 1.0))


def _direction(axis: int, sign: float) -> np.ndarray:
    unit = np.zeros(3)
    unit[axis] = sign
    return unit


def _transform_for(axis: int, sign: float) -> np.ndarray:
    """Transform som vrider den valda riktningen till att peka rakt ned.

    Rena 90-graderssteg, så delens mått byter bara plats med varandra och
    ingen geometri förvrids av flyttalsbrus.
    """
    if axis == 2 and sign < 0:
        return np.eye(4)
    return trimesh.geometry.align_vectors(_direction(axis, sign), [0.0, 0.0, -1.0])


def contact_area(vertices: np.ndarray) -> float:
    """Anliggningens area, i mm².

    Punkterna närmast plattan projiceras och deras konvexa hölje mäts. En del
    som vilar på flera ribbor får därmed hela rutan räknad, vilket är rätt:
    det är ytterkonturen som avgör om delen står stadigt.
    """
    lowest = float(vertices[:, 2].min())
    resting = vertices[vertices[:, 2] <= lowest + CONTACT_TOLERANCE_MM][:, :2]
    if len(resting) < 3:
        return 0.0
    try:
        from scipy.spatial import ConvexHull

        return float(ConvexHull(resting).volume)  # 2D: volume är arean
    except Exception:  # pragma: no cover - kolinjära punkter
        low = resting.min(axis=0)
        high = resting.max(axis=0)
        return float(np.prod(high - low))


#: Internt namn sedan tidigare. `contact_area` är samma sak, men behövs även
#: utifrån: planeraren måste kunna se att ett läge bara vilar på en smal kant.
_footprint = contact_area


def flat_transform(mesh: trimesh.Trimesh) -> tuple[np.ndarray, float]:
    """Hitta det axelriktade läge som lägger delen plattast.

    Returnerar (transform, bygghöjd i mm). Transformen lägger också delen mot
    z = 0 och i första kvadranten, som en slicer vill ha den.

    Lägst bygghöjd vinner; vid lika höjd den största anliggningen. Delens
    nuvarande läge provas först, så en del som redan ligger rätt lämnas orörd.
    """
    vertices = np.asarray(mesh.vertices, dtype=float)
    if len(vertices) == 0:
        return np.eye(4), 0.0

    best = None
    for axis, sign in AXIS_POSES:
        transform = _transform_for(axis, sign)
        moved = trimesh.transform_points(vertices, transform)
        height = float(moved[:, 2].max() - moved[:, 2].min())
        key = (round(height, 3), -_footprint(moved))
        if best is None or key < best[0]:
            best = (key, transform, height)

    _, transform, height = best

    moved = trimesh.transform_points(vertices, transform)
    shift = np.eye(4)
    shift[:3, 3] = -moved.min(axis=0)
    return shift @ transform, height


def lay_flat(mesh: trimesh.Trimesh) -> tuple[trimesh.Trimesh, float]:
    """Delen vänd till sitt plattaste axelriktade läge, plus bygghöjden."""
    transform, height = flat_transform(mesh)
    out = mesh.copy()
    out.apply_transform(transform)
    return out, height


def describe_orientation(before: trimesh.Trimesh, after: trimesh.Trimesh) -> str:
    """Vad vändningen gav, på svenska."""
    high = float(before.extents[2])
    low = float(after.extents[2])
    if abs(high - low) < 0.05:
        return "låg redan platt"
    return f"bygghöjd {high:.1f} → {low:.1f} mm"
