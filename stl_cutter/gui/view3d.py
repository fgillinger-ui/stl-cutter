"""3D-vyn.

Geometrin räknas ut av rena funktioner som går att testa utan grafikkort;
själva ritandet sköts av `ModelView`, som bygger på pyqtgraph.opengl.
"""

from __future__ import annotations

import colorsys
import logging

import numpy as np
import pyqtgraph.opengl as gl
import trimesh

log = logging.getLogger(__name__)

MODEL_COLOR = (0.68, 0.72, 0.78, 1.0)
PLANE_COLOR = (0.95, 0.55, 0.15, 0.28)
BED_COLOR = (0.45, 0.5, 0.55, 0.6)

#: Hur långt planen ritas utanför modellen, som andel av modellens storlek.
PLANE_MARGIN = 0.08


def part_colors(count: int) -> list[tuple[float, float, float, float]]:
    """Tydligt skilda färger, en per del."""
    if count <= 0:
        return []
    colors = []
    for i in range(count):
        hue = (i * 0.618033988749895) % 1.0  # gyllene snittet sprider färgerna
        r, g, b = colorsys.hsv_to_rgb(hue, 0.55, 0.95)
        colors.append((r, g, b, 1.0))
    return colors


def explode_offsets(centres: np.ndarray, distance_mm: float) -> np.ndarray:
    """Förskjutning per del när delarna sprängs isär.

    Varje del flyttas rakt ut från modellens mitt. Delar som ligger i mitten
    flyttas minst - riktningen normeras mot den del som ligger längst ut.
    """
    centres = np.atleast_2d(np.asarray(centres, dtype=float))
    if len(centres) == 0 or distance_mm == 0:
        return np.zeros_like(centres)
    middle = centres.mean(axis=0)
    directions = centres - middle
    longest = float(np.linalg.norm(directions, axis=1).max())
    if longest < 1e-9:
        return np.zeros_like(centres)
    return directions / longest * float(distance_mm)


def plane_quad(plane, bounds: np.ndarray, margin: float = PLANE_MARGIN):
    """Fyrkanten som visar ett snittplan, som (hörn, trianglar)."""
    bounds = np.asarray(bounds, dtype=float)
    extents = bounds[1] - bounds[0]
    pad = extents * margin
    low, high = bounds[0] - pad, bounds[1] + pad

    axis = plane.axis
    others = [a for a in range(3) if a != axis]
    position = float(plane.origin[axis])

    corners = []
    for first, second in ((0, 0), (1, 0), (1, 1), (0, 1)):
        point = np.zeros(3)
        point[axis] = position
        point[others[0]] = low[others[0]] if first == 0 else high[others[0]]
        point[others[1]] = low[others[1]] if second == 0 else high[others[1]]
        corners.append(point)

    vertices = np.array(corners, dtype=float)
    faces = np.array([[0, 1, 2], [0, 2, 3]], dtype=int)
    return vertices, faces


def bed_grid(printer, spacing_mm: float = 20.0):
    """Rutnät som visar byggplattan: (storlek, avstånd)."""
    size = (float(printer.bed_x), float(printer.bed_y), 1.0)
    return size, float(spacing_mm)


def mesh_data(mesh: trimesh.Trimesh) -> gl.MeshData:
    return gl.MeshData(
        vertexes=np.asarray(mesh.vertices, dtype=float),
        faces=np.asarray(mesh.faces, dtype=int),
    )


class ModelView(gl.GLViewWidget):
    """Visar modellen, snittplanen och de färdiga delarna."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBackgroundColor((32, 34, 38))
        self._model_item = None
        self._plane_items: list = []
        self._part_items: list = []
        self._part_centres = np.zeros((0, 3))
        self._bed_item = None
        self._explode_mm = 0.0
        self.opts["distance"] = 600

    # -- modellen ---------------------------------------------------------

    def show_model(self, mesh: trimesh.Trimesh) -> None:
        self.clear_parts()
        self.clear_planes()
        if self._model_item is not None:
            self.removeItem(self._model_item)
        self._model_item = gl.GLMeshItem(
            meshdata=mesh_data(mesh),
            smooth=False,
            shader="shaded",
            color=MODEL_COLOR,
            drawEdges=False,
        )
        self.addItem(self._model_item)
        self.frame_on(mesh.bounds)

    def frame_on(self, bounds) -> None:
        """Centrera kameran på en bounding box."""
        bounds = np.asarray(bounds, dtype=float)
        centre = (bounds[0] + bounds[1]) / 2.0
        from pyqtgraph import Vector

        self.opts["center"] = Vector(*centre)
        self.opts["distance"] = float(max(np.linalg.norm(bounds[1] - bounds[0]), 50.0) * 1.6)
        self.update()

    # -- snittplan --------------------------------------------------------

    def show_planes(self, planes, bounds) -> None:
        self.clear_planes()
        for plane in planes:
            vertices, faces = plane_quad(plane, bounds)
            item = gl.GLMeshItem(
                meshdata=gl.MeshData(vertexes=vertices, faces=faces),
                smooth=False,
                color=PLANE_COLOR,
                glOptions="additive",
                drawEdges=True,
                edgeColor=(1.0, 0.6, 0.2, 0.9),
            )
            self.addItem(item)
            self._plane_items.append(item)

    def clear_planes(self) -> None:
        for item in self._plane_items:
            self.removeItem(item)
        self._plane_items = []

    # -- delarna ----------------------------------------------------------

    def show_parts(self, parts) -> None:
        """Visa de kapade delarna i olika färger och dölj originalmodellen."""
        self.clear_parts()
        self.clear_planes()
        if self._model_item is not None:
            self._model_item.setVisible(False)

        colors = part_colors(len(parts))
        centres = []
        for part, color in zip(parts, colors):
            mesh = getattr(part, "mesh", part)
            item = gl.GLMeshItem(
                meshdata=mesh_data(mesh), smooth=False, shader="shaded", color=color
            )
            self.addItem(item)
            self._part_items.append(item)
            centres.append((mesh.bounds[0] + mesh.bounds[1]) / 2.0)

        self._part_centres = np.array(centres, dtype=float) if centres else np.zeros((0, 3))
        self.set_explode(self._explode_mm)

    def clear_parts(self) -> None:
        for item in self._part_items:
            self.removeItem(item)
        self._part_items = []
        self._part_centres = np.zeros((0, 3))
        if self._model_item is not None:
            self._model_item.setVisible(True)

    def set_explode(self, distance_mm: float) -> None:
        """Spräng isär delarna så att fogarna syns."""
        self._explode_mm = float(distance_mm)
        if not self._part_items:
            return
        offsets = explode_offsets(self._part_centres, self._explode_mm)
        for item, offset in zip(self._part_items, offsets):
            item.resetTransform()
            item.translate(*offset)

    # -- byggplattan ------------------------------------------------------

    def set_bed(self, printer, visible: bool) -> None:
        if self._bed_item is not None:
            self.removeItem(self._bed_item)
            self._bed_item = None
        if not visible or printer is None:
            return
        size, spacing = bed_grid(printer)
        grid = gl.GLGridItem()
        grid.setSize(*size)
        grid.setSpacing(spacing, spacing, spacing)
        grid.setColor(tuple(int(c * 255) for c in BED_COLOR))
        self._bed_item = grid
        self.addItem(grid)

    # -- allt -------------------------------------------------------------

    def clear_all(self) -> None:
        self.clear_parts()
        self.clear_planes()
        if self._model_item is not None:
            self.removeItem(self._model_item)
            self._model_item = None
