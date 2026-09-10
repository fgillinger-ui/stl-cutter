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

log = logging.getLogger(__name__)

SUPPORTED_INPUT = (".stl", ".3mf")


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

    @property
    def face_count(self) -> int:
        return int(len(self.mesh.faces))

    def summary(self) -> str:
        x, y, z = self.extents_mm
        state = "hel (watertight)" if self.watertight else "INTE hel - hål i ytan"
        return (
            f"{self.path.name}: {x:.1f} x {y:.1f} x {z:.1f} mm, "
            f"{self.face_count} trianglar, volym {self.volume_mm3 / 1000.0:.1f} cm3, {state}"
        )


def _as_single_mesh(loaded) -> trimesh.Trimesh:
    """Slå ihop en Scene eller en lista av meshar till en enda Trimesh."""
    if isinstance(loaded, trimesh.Trimesh):
        return loaded
    if isinstance(loaded, trimesh.Scene):
        geometries = [g for g in loaded.geometry.values() if isinstance(g, trimesh.Trimesh)]
        if not geometries:
            raise ValueError("Filen innehåller ingen triangelgeometri.")
        return trimesh.util.concatenate(loaded.dump())
    if isinstance(loaded, Iterable):
        geometries = [g for g in loaded if isinstance(g, trimesh.Trimesh)]
        if not geometries:
            raise ValueError("Filen innehåller ingen triangelgeometri.")
        return trimesh.util.concatenate(geometries)
    raise ValueError(f"Kan inte tolka inläst geometri av typen {type(loaded)!r}.")


def repair_mesh(mesh: trimesh.Trimesh) -> list[str]:
    """Städa en mesh på plats. Returnerar en lista över vad som gjordes."""
    actions: list[str] = []

    before_vertices = len(mesh.vertices)
    mesh.merge_vertices()
    if len(mesh.vertices) < before_vertices:
        actions.append(f"slog ihop {before_vertices - len(mesh.vertices)} dubblerade vertices")

    before_faces = len(mesh.faces)
    mesh.update_faces(mesh.unique_faces())
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.remove_unreferenced_vertices()
    if len(mesh.faces) < before_faces:
        actions.append(f"tog bort {before_faces - len(mesh.faces)} dubblerade/degenererade trianglar")

    if not mesh.is_watertight:
        try:
            if mesh.fill_holes():
                actions.append("fyllde hål i ytan")
        except Exception as exc:  # pragma: no cover - beror på indata
            log.warning("Hålfyllning misslyckades: %s", exc)

    if not mesh.is_winding_consistent or mesh.volume < 0:
        mesh.fix_normals()
        actions.append("rättade normalriktningar")

    return actions


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

    repairs = repair_mesh(mesh) if repair else []

    if not mesh.is_watertight:
        log.warning("Meshen %s är inte watertight - snitten kan bli oförutsägbara.", path.name)

    return MeshInfo(
        path=path,
        mesh=mesh,
        watertight=bool(mesh.is_watertight),
        winding_consistent=bool(mesh.is_winding_consistent),
        volume_mm3=float(abs(mesh.volume)),
        extents_mm=tuple(float(v) for v in mesh.extents),
        repairs=repairs,
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
