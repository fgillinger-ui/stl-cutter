"""Läsning, reparation och skrivning av meshar.

Alla meshar hanteras internt i millimeter. STL saknar enhetsinformation och
antas därför alltid vara i mm; 3MF bär enhet i sin XML och konverteras av
trimesh vid inläsning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np
import trimesh
from scipy.spatial import cKDTree

log = logging.getLogger(__name__)

SUPPORTED_INPUT = (".stl", ".3mf")

#: Toleranser som provas när sprickor i ytan ska svetsas ihop, i mm. Den
#: största ligger under vad en 3D-skrivare kan återge, så geometrin påverkas
#: inte märkbart.
WELD_TOLERANCES_MM = (0.0001, 0.001, 0.01, 0.05, 0.1)

#: En reparation som ändrar volymen mer än så här har förstört något.
MAX_REPAIR_VOLUME_CHANGE = 0.01

#: Minsta andel av volymen som får vara kvar när överlappande kroppar slås
#: ihop. En union tar bort dubbelräknat material där kroppar överlappar, så
#: volymen SKA minska - men inte hur mycket som helst.
MIN_UNION_VOLUME_FRACTION = 0.5


@dataclass
class MeshInfo:
    """Sammanfattning av en inläst mesh."""

    path: Path
    mesh: trimesh.Trimesh
    watertight: bool
    winding_consistent: bool
    volume_mm3: float
    extents_mm: tuple[float, float, float]
    repairs: list[str] = field(default_factory=list)
    #: Kanter som inte delas av exakt två trianglar efter reparationen.
    #: 0 betyder en hel mesh. Samma mått som slicers kallar non-manifold edges.
    open_edges: int = 0

    @property
    def face_count(self) -> int:
        return int(len(self.mesh.faces))

    def summary(self) -> str:
        x, y, z = self.extents_mm
        state = (
            "hel (watertight)"
            if self.watertight
            else f"INTE hel - {self.open_edges} öppna kanter"
        )
        return (
            f"{self.path.name}: {x:.1f} x {y:.1f} x {z:.1f} mm, "
            f"{self.face_count} trianglar, volym {self.volume_mm3 / 1000.0:.1f} cm3, {state}"
        )


def merge_bodies(meshes: list[trimesh.Trimesh]) -> trimesh.Trimesh:
    """Slå ihop flera solida kroppar till en.

    En CAD-fil (särskilt 3MF) innehåller ofta flera separata kroppar. Att bara
    lägga deras trianglar i samma mesh (`concatenate`) ger en mesh som ser hel
    ut men inte är det: där två kroppar möts delar kanterna fyra trianglar i
    stället för två, och slicern rapporterar "non-manifold edges". En riktig
    boolean-union tar bort de inre väggarna och ger en enda solid.
    """
    meshes = [m for m in meshes if m is not None and len(m.faces) > 0]
    if not meshes:
        raise ValueError("Filen innehåller ingen triangelgeometri.")
    if len(meshes) == 1:
        return meshes[0]

    if all(m.is_watertight for m in meshes):
        try:
            merged = trimesh.boolean.union(meshes, engine=_boolean_engine())
        except Exception as exc:
            log.warning(
                "Kunde inte slå ihop %d kroppar med en boolean (%s) - lägger ihop dem "
                "som de är. Meshen kan bli icke-manifold.",
                len(meshes),
                exc,
            )
        else:
            if merged is not None and len(merged.faces) > 0:
                log.info("Slog ihop %d separata kroppar till en solid.", len(meshes))
                return merged

    return trimesh.util.concatenate(meshes)


def _boolean_engine() -> str | None:
    try:
        return "manifold" if "manifold" in trimesh.boolean.engines_available else None
    except Exception:  # pragma: no cover - äldre trimesh
        return None


def _as_single_mesh(loaded) -> trimesh.Trimesh:
    """Slå ihop en Scene eller en lista av meshar till en enda Trimesh."""
    if isinstance(loaded, trimesh.Trimesh):
        return loaded
    if isinstance(loaded, trimesh.Scene):
        geometries = [g for g in loaded.geometry.values() if isinstance(g, trimesh.Trimesh)]
        if not geometries:
            raise ValueError("Filen innehåller ingen triangelgeometri.")
        return merge_bodies(list(loaded.dump()))
    if isinstance(loaded, Iterable):
        geometries = [g for g in loaded if isinstance(g, trimesh.Trimesh)]
        return merge_bodies(geometries)
    raise ValueError(f"Kan inte tolka inläst geometri av typen {type(loaded)!r}.")


def bad_edges(mesh: trimesh.Trimesh) -> tuple[int, int]:
    """(kanter utan granne, kanter med fler än två grannar).

    En hel mesh har exakt två trianglar per kant. Färre betyder hål, fler
    betyder att ytor ligger på varandra - typiskt två kroppar som möts.
    """
    try:
        groups = trimesh.grouping.group_rows(mesh.edges_sorted)
    except Exception:  # pragma: no cover - degenererad geometri
        return 0, 0
    boundary = sum(1 for g in groups if len(g) == 1)
    excess = sum(1 for g in groups if len(g) > 2)
    return int(boundary), int(excess)


def open_edge_count(mesh: trimesh.Trimesh) -> int:
    """Antal kanter som inte delas av exakt två trianglar.

    Det här är måttet slicers rapporterar som "non-manifold edges".
    """
    boundary, excess = bad_edges(mesh)
    return boundary + excess


def weld_vertices(mesh: trimesh.Trimesh, tolerance_mm: float) -> trimesh.Trimesh:
    """Slå ihop vertices som ligger närmare varandra än `tolerance_mm`.

    `merge_vertices()` slår bara ihop punkter som är exakt lika (eller som
    avrundas lika), och missar därför sprickor från CAD-export där hörnen
    ligger en hårsmån isär. Här grupperas punkterna i stället efter avstånd
    med en KD-trädsökning, vilket sluter den sortens springor.
    """
    vertices = np.asarray(mesh.vertices, dtype=float)
    pairs = cKDTree(vertices).query_pairs(float(tolerance_mm), output_type="ndarray")
    if len(pairs) == 0:
        return mesh

    # Union-find: närliggande punkter hamnar i samma grupp.
    parent = np.arange(len(vertices))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for first, second in pairs:
        root_a, root_b = find(int(first)), find(int(second))
        if root_a != root_b:
            parent[max(root_a, root_b)] = min(root_a, root_b)

    roots = np.array([find(i) for i in range(len(vertices))])
    _, inverse = np.unique(roots, return_inverse=True)

    # Varje grupp ersätts av sin tyngdpunkt.
    merged = np.zeros((int(inverse.max()) + 1, 3), dtype=float)
    np.add.at(merged, inverse, vertices)
    merged /= np.bincount(inverse)[:, None]

    welded = trimesh.Trimesh(vertices=merged, faces=inverse[np.asarray(mesh.faces)], process=False)
    welded.update_faces(welded.nondegenerate_faces())
    welded.update_faces(welded.unique_faces())
    welded.remove_unreferenced_vertices()
    welded.merge_vertices()
    return welded


def repair_mesh(mesh: trimesh.Trimesh, weld: bool = True) -> tuple[trimesh.Trimesh, list[str]]:
    """Laga en mesh så gott det går.

    Returnerar (mesh, lista över vad som gjordes). Meshen kan vara en ny
    instans om vertices behövde svetsas ihop, så använd alltid returvärdet.

    Ordningen är från försiktigt till mer ingripande, och varje steg görs bara
    om meshen fortfarande inte är sluten:

    1. slå ihop identiska vertices och kasta dubblerade eller platta trianglar,
    2. svetsa ihop vertices som ligger nära varandra (sprickor),
    3. fyll återstående hål,
    4. rätta normalriktningar.
    """
    actions: list[str] = []
    volume_before = float(abs(mesh.volume))

    before_vertices = len(mesh.vertices)
    mesh.merge_vertices()
    if len(mesh.vertices) < before_vertices:
        actions.append(f"slog ihop {before_vertices - len(mesh.vertices)} dubblerade vertices")

    before_faces = len(mesh.faces)
    mesh.update_faces(mesh.unique_faces())
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.remove_unreferenced_vertices()
    if len(mesh.faces) < before_faces:
        actions.append(
            f"tog bort {before_faces - len(mesh.faces)} dubblerade/degenererade trianglar"
        )

    # Ytor som ligger på varandra: två kroppar som möts inuti samma mesh.
    # Ingen svetsning i världen lagar det - kropparna måste slås ihop med en
    # boolean union.
    # Flera kroppar i samma mesh, antingen som överlappande slutna skal
    # (body_count > 1) eller som ytor som möts (kanter med fler än två
    # trianglar). Båda ger problem vid snitt och booleaner.
    needs_merge = bad_edges(mesh)[1] > 0 or int(mesh.body_count) > 1
    if needs_merge:
        bodies = [b for b in mesh.split(only_watertight=True) if len(b.faces) > 0]
        if len(bodies) > 1:
            candidate = merge_bodies(bodies)
            volume_after = float(abs(candidate.volume))
            # Volymen av en icke-manifold mesh är inte meningsfull - överlappande
            # kroppar räknas dubbelt. Unionen SKA därför kunna minska volymen,
            # och det är unionens volym som är den riktiga.
            reasonable = volume_before <= 0 or (
                MIN_UNION_VOLUME_FRACTION * volume_before
                <= volume_after
                <= (1.0 + MAX_REPAIR_VOLUME_CHANGE) * volume_before
            )
            if candidate.is_watertight and reasonable:
                actions.append(f"slog ihop {len(bodies)} separata kroppar till en solid")
                mesh = candidate
                volume_before = volume_after

    if weld and not mesh.is_watertight:
        openings = open_edge_count(mesh)
        for tolerance in WELD_TOLERANCES_MM:
            candidate = weld_vertices(mesh, tolerance)
            changed = abs(abs(candidate.volume) - volume_before)
            if volume_before > 0 and changed / volume_before > MAX_REPAIR_VOLUME_CHANGE:
                log.debug("Svetsning med %.4f mm ändrade volymen för mycket - avbryter.", tolerance)
                break
            mesh = candidate
            if mesh.is_watertight:
                actions.append(
                    f"svetsade ihop {openings} öppna kanter (tolerans {tolerance:g} mm)"
                )
                break
        else:
            if open_edge_count(mesh) < openings:
                actions.append(
                    f"svetsade ihop {openings - open_edge_count(mesh)} av {openings} öppna kanter"
                )

    if not mesh.is_watertight:
        try:
            if mesh.fill_holes():
                actions.append("fyllde hål i ytan")
        except Exception as exc:  # pragma: no cover - beror på indata
            log.warning("Hålfyllning misslyckades: %s", exc)

    if not mesh.is_winding_consistent or mesh.volume < 0:
        mesh.fix_normals()
        actions.append("rättade normalriktningar")

    return mesh, actions


def load_mesh(path: str | Path, repair: bool = True) -> MeshInfo:
    """Läs STL (binär eller ascii) eller 3MF och returnera en `MeshInfo`."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Hittar inte filen: {path}")
    if path.suffix.lower() not in SUPPORTED_INPUT:
        raise ValueError(
            f"Filformatet {path.suffix!r} stöds inte. Använd något av: {', '.join(SUPPORTED_INPUT)}"
        )

    loaded = trimesh.load(path, force="mesh" if path.suffix.lower() == ".stl" else None)
    mesh = _as_single_mesh(loaded)
    mesh.process(validate=True)

    repairs: list[str] = []
    if repair:
        mesh, repairs = repair_mesh(mesh)

    if not mesh.is_watertight:
        log.warning(
            "Meshen %s är inte sluten - %d öppna kanter kvar efter reparation.",
            path.name,
            open_edge_count(mesh),
        )

    return MeshInfo(
        path=path,
        mesh=mesh,
        watertight=bool(mesh.is_watertight),
        winding_consistent=bool(mesh.is_winding_consistent),
        volume_mm3=float(abs(mesh.volume)),
        extents_mm=tuple(float(v) for v in mesh.extents),
        repairs=repairs,
        open_edges=open_edge_count(mesh),
    )


def save_stl(mesh: trimesh.Trimesh, path: str | Path) -> Path:
    """Skriv en mesh som binär STL. Detta är alltid tillgängligt."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(trimesh.exchange.stl.export_stl(mesh))
    return path


def save_3mf(mesh: trimesh.Trimesh, path: str | Path) -> Path:
    """Skriv 3MF om biblioteksstöd finns, annars STL med tydlig varning.

    Returnerar sökvägen till filen som faktiskt skrevs.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    scene = trimesh.Scene(mesh)
    try:
        data = trimesh.exchange.export.export_scene(scene, file_type="3mf")
    except Exception as exc:
        fallback = path.with_suffix(".stl")
        log.warning(
            "3MF-export saknar biblioteksstöd (%s). Skriver STL istället: %s", exc, fallback.name
        )
        return save_stl(mesh, fallback)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    return path


def bounding_box_mm(mesh: trimesh.Trimesh) -> np.ndarray:
    """Axelparallell bounding box som (2, 3)-array i mm."""
    return np.asarray(mesh.bounds, dtype=float)
