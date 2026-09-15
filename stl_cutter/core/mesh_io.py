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


#: Hur stor glipa som får finnas mellan två kroppar och de ändå räknas som
#: mötande, i mm. Måttet är alltså på *avståndet*, inte på överlappet: två
#: kroppar som ligger yta mot yta överlappar inte alls, och det är just de som
#: ger fyra trianglar per kant om de inte slås ihop. Marginalen finns för att
#: en CAD-export sällan träffar exakt noll.
TOUCH_TOLERANCE_MM = 0.05


def bodies_touch(a: trimesh.Trimesh, b: trimesh.Trimesh) -> bool:
    """Möts eller överlappar kropparnas bounding boxar?

    Grovt med flit. Två kroppar som verkligen hör ihop har boxar utan glipa
    emellan; två skilda objekt i samma fil står isär. Ett falskt ja kostar
    bara en boolean som ändå ger rätt svar.
    """
    low = np.maximum(a.bounds[0], b.bounds[0])
    high = np.minimum(a.bounds[1], b.bounds[1])
    # Negativt värde = glipa längs den axeln. Noll = yta mot yta.
    return bool(np.all(high - low >= -TOUCH_TOLERANCE_MM))


def group_touching(bodies: list[trimesh.Trimesh]) -> list[list[trimesh.Trimesh]]:
    """Gruppera kroppar som nuddar varandra (union-find).

    Kroppar i samma grupp är samma föremål och ska slås ihop till en solid.
    Grupperna emellan är skilda objekt och ska förbli det.
    """
    parent = list(range(len(bodies)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(bodies)):
        for j in range(i + 1, len(bodies)):
            if bodies_touch(bodies[i], bodies[j]):
                parent[find(i)] = find(j)

    buckets: dict[int, list[trimesh.Trimesh]] = {}
    for i, body in enumerate(bodies):
        buckets.setdefault(find(i), []).append(body)
    # Ordna objekten som de ligger, vänster till höger.
    return sorted(buckets.values(), key=lambda g: min(float(m.bounds[0][0]) for m in g))


def merge_bodies(meshes: list[trimesh.Trimesh]) -> trimesh.Trimesh:
    """Slå ihop flera solida kroppar till en mesh.

    En CAD-fil (särskilt 3MF) innehåller ofta flera separata kroppar. Att bara
    lägga deras trianglar i samma mesh (`concatenate`) ger en mesh som ser hel
    ut men inte är det: där två kroppar **möts** delar kanterna fyra trianglar
    i stället för två, och slicern rapporterar "non-manifold edges". En riktig
    boolean-union tar bort de inre väggarna och ger en enda solid.

    Kroppar som ligger *isär* är däremot skilda föremål - en hylla och dess
    bakplatta i samma fil - och unionas inte. De läggs sida vid sida, vilket
    är korrekt: varje kant delas fortfarande av exakt två trianglar. Att
    boolea ihop dem vore både onödigt och riskabelt på en stor modell.
    """
    meshes = [m for m in meshes if m is not None and len(m.faces) > 0]
    if not meshes:
        raise ValueError("Filen innehåller ingen triangelgeometri.")
    if len(meshes) == 1:
        return meshes[0]

    groups = group_touching(meshes)
    if len(groups) > 1:
        merged = [merge_bodies(group) for group in groups]
        log.info(
            "Filen innehåller %d separata objekt - de hålls isär.", len(merged)
        )
        return trimesh.util.concatenate(merged)

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
                # Kroppar som ligger isär är skilda objekt och unionas inte -
                # då är det bara trianglar som lagts sida vid sida, och det ska
                # inte rapporteras som en reparation av något.
                groups = len(group_touching(bodies))
                if groups < len(bodies):
                    actions.append(
                        f"slog ihop {len(bodies) - groups + 1} kroppar som möttes "
                        "till en solid"
                    )
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


def _stl_safe(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Förbered en mesh för STL:s 32-bitars precision.

    STL lagrar koordinater som 32-bitars flyttal. Vid 280 mm är upplösningen
    ungefär 0,00003 mm, så två hörn som ligger närmare varandra än så blir
    *samma* punkt i filen. När meshen sedan läses in slås de ihop, trianglarna
    mellan dem blir platta, och kanterna får fler än två grannar - det slicern
    rapporterar som non-manifold edges. En måttändrad platta som var hel i
    minnet kom tillbaka med fyra sådana kanter.

    Lösningen är att göra precis det STL-formatet kommer att göra - avrunda
    till float32 - men göra det här, där de platta trianglarna kan städas bort
    efteråt. Att i stället svetsa med en tolerans "med marginal" är fel väg:
    en marginal på 0,0001 mm slog ihop ytor som skulle vara skilda och öppnade
    meshen.

    Blir meshen ändå sämre lämnas den orörd och en varning loggas. Då är felet
    i geometrin och inte i exporten, och det ska synas.
    """
    if not len(mesh.vertices):
        return mesh

    try:
        rounded = np.asarray(mesh.vertices, dtype=np.float32).astype(np.float64)
        out = trimesh.Trimesh(vertices=rounded, faces=mesh.faces, process=False)
        out.merge_vertices()
        out.update_faces(out.nondegenerate_faces())
        out.update_faces(out.unique_faces())
        out.remove_unreferenced_vertices()
        if mesh.is_watertight and not out.is_watertight:
            # Avrundningen har dragit ihop hörn som bar upp ytan. Samma
            # reparation som körs vid inläsning stänger springan igen.
            out, actions = repair_mesh(out)
            if out.is_watertight and actions:
                log.info("STL-export: %s", "; ".join(actions))
    except Exception:  # pragma: no cover - försvar mot udda geometri
        log.exception("Kunde inte förbereda meshen för STL - skriver den som den är")
        return mesh

    if mesh.is_watertight and not out.is_watertight:
        log.warning(
            "Meshen går inte att skriva som STL utan att öppna sig - skriver "
            "den som den är. Kontrollera modellen i slicern."
        )
        return mesh
    removed = len(mesh.faces) - len(out.faces)
    if removed:
        log.info(
            "Tog bort %d trianglar som blir platta i STL:s precision.", removed
        )
    return out


def save_stl(mesh: trimesh.Trimesh, path: str | Path) -> Path:
    """Skriv en mesh som binär STL. Detta är alltid tillgängligt."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(trimesh.exchange.stl.export_stl(_stl_safe(mesh)))
    return path


def save_3mf(mesh: trimesh.Trimesh, path: str | Path) -> Path:
    """Skriv 3MF om biblioteksstöd finns, annars STL med tydlig varning.

    Returnerar sökvägen till filen som faktiskt skrevs.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    scene = trimesh.Scene(mesh)
    try:
        # `file_obj` är positionellt i trimesh och måste skickas med som None
        # för att få tillbaka bytes i stället för att skriva till en fil.
        data = trimesh.exchange.export.export_scene(scene, None, file_type="3mf")
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
