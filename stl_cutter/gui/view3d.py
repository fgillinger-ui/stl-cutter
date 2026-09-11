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
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu

log = logging.getLogger(__name__)

#: Bakgrund och modellfärg hör ihop - en ljus modell försvinner mot vitt.
DARK_BACKGROUND = (32, 34, 38)
LIGHT_BACKGROUND = (238, 240, 244)
MODEL_COLOR_ON_DARK = (0.68, 0.72, 0.78, 1.0)
MODEL_COLOR_ON_LIGHT = (0.42, 0.48, 0.58, 1.0)

PLANE_COLOR = (0.95, 0.55, 0.15, 0.28)
BED_COLOR_ON_DARK = (0.45, 0.5, 0.55, 0.6)
BED_COLOR_ON_LIGHT = (0.30, 0.34, 0.40, 0.7)

#: Hur långt planen ritas utanför modellen, som andel av modellens storlek.
PLANE_MARGIN = 0.08

#: Färdiga kameravinklar: (azimut, elevation) i grader.
STANDARD_VIEWS = {
    "Snett framifrån": (-60.0, 30.0),
    "Framifrån": (-90.0, 0.0),
    "Bakifrån": (90.0, 0.0),
    "Från vänster": (180.0, 0.0),
    "Från höger": (0.0, 0.0),
    "Ovanifrån": (-90.0, 89.9),
    "Underifrån": (-90.0, -89.9),
}

#: Rör sig musen mindre än så här räknas det som ett klick, inte ett drag.
CLICK_SLOP_PX = 4

#: Hur nära ett plan man måste klicka för att ta tag i det.
PLANE_GRAB_TOLERANCE = 0.0

MOUSE_HELP = (
    "Dra i ett snittplan för att flytta det. Shift+dra vinklar planet.\n"
    "Dra vid sidan om med vänster eller höger musknapp för att vrida modellen.\n"
    "Mittenknapp eller Ctrl+dra flyttar vyn i sidled.\n"
    "Mushjulet zoomar. Högerklicka för färdiga vinklar."
)


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
    """Visar modellen, snittplanen och de färdiga delarna.

    Snittplanen går att ta tag i och dra direkt i vyn: en dragning flyttar
    planet längs sin egen normal, Shift+dragning vinklar det.
    """

    #: (snittets index, förflyttning i mm längs normalen)
    plane_dragged = Signal(int, float)
    #: (snittets index, vridning i grader kring vyns upp- respektive högeraxel)
    plane_tilted = Signal(int, float, float)
    #: (snittets index) - dragningen är klar, dags att analysera om
    plane_released = Signal(int)

    def __init__(self, parent=None, light_background: bool = True):
        super().__init__(parent)
        self._light = bool(light_background)
        self.setBackgroundColor(LIGHT_BACKGROUND if self._light else DARK_BACKGROUND)
        self._model_item = None
        self._plane_items: list = []
        self._part_items: list = []
        self._part_centres = np.zeros((0, 3))
        self._bed_item = None
        self._printer = None
        self._explode_mm = 0.0
        self._press_pos = None
        self._planes: list = []
        self._drag = None
        self.opts["distance"] = 600
        self.setToolTip(MOUSE_HELP)

    @property
    def model_color(self):
        return MODEL_COLOR_ON_LIGHT if self._light else MODEL_COLOR_ON_DARK

    @property
    def bed_color(self):
        return BED_COLOR_ON_LIGHT if self._light else BED_COLOR_ON_DARK

    def set_light_background(self, light: bool) -> None:
        """Byt mellan ljus och mörk bakgrund utan att tappa det som visas."""
        self._light = bool(light)
        self.setBackgroundColor(LIGHT_BACKGROUND if self._light else DARK_BACKGROUND)
        if self._model_item is not None:
            self._model_item.setColor(self.model_color)
        if self._bed_item is not None:
            self.set_bed(self._printer, True)
        self.update()

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
            color=self.model_color,
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
        self._planes = list(planes)
        self._plane_bounds = np.asarray(bounds, dtype=float)
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
        self._planes = []

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
        self._printer = printer
        if self._bed_item is not None:
            self.removeItem(self._bed_item)
            self._bed_item = None
        if not visible or printer is None:
            return
        size, spacing = bed_grid(printer)
        grid = gl.GLGridItem()
        grid.setSize(*size)
        grid.setSpacing(spacing, spacing, spacing)
        grid.setColor(tuple(int(c * 255) for c in self.bed_color))
        self._bed_item = grid
        self.addItem(grid)

    # -- att peka och ta tag i ett plan ------------------------------------

    def ray_at(self, x: float, y: float) -> tuple[np.ndarray, np.ndarray]:
        """Strålen från kameran genom en punkt på skärmen, i världskoordinater."""
        width = max(self.width(), 1)
        height = max(self.height(), 1)
        ndc_x = 2.0 * float(x) / width - 1.0
        ndc_y = 1.0 - 2.0 * float(y) / height

        viewport = self.getViewport()
        combined = self.projectionMatrix(viewport, viewport) * self.viewMatrix()
        inverse, ok = combined.inverted()
        if not ok:  # pragma: no cover - degenererad kamera
            return np.zeros(3), np.array([0.0, 0.0, -1.0])

        matrix = np.array(inverse.data(), dtype=float).reshape(4, 4).T

        def unproject(depth: float) -> np.ndarray:
            point = matrix @ np.array([ndc_x, ndc_y, depth, 1.0])
            return point[:3] / point[3]

        near, far = unproject(-1.0), unproject(1.0)
        direction = far - near
        length = float(np.linalg.norm(direction))
        return near, direction / length if length > 1e-12 else direction

    def plane_at(self, x: float, y: float):
        """Vilket snittplan ligger under pekaren? Returnerar (index, träffpunkt)."""
        if not self._planes:
            return None
        origin, direction = self.ray_at(x, y)
        best = None
        for number, plane in enumerate(self._planes):
            normal = np.asarray(plane.unit_normal, dtype=float)
            denominator = float(np.dot(direction, normal))
            if abs(denominator) < 1e-9:
                continue
            distance = float(
                np.dot(np.asarray(plane.origin, dtype=float) - origin, normal) / denominator
            )
            if distance <= 0:
                continue
            point = origin + direction * distance
            corners, _ = plane_quad(plane, self._plane_bounds)
            low = corners.min(axis=0) - PLANE_GRAB_TOLERANCE
            high = corners.max(axis=0) + PLANE_GRAB_TOLERANCE
            if np.any(point < low - 1e-6) or np.any(point > high + 1e-6):
                continue
            if best is None or distance < best[0]:
                best = (distance, number, point)
        if best is None:
            return None
        return best[1], best[2]

    @staticmethod
    def _closest_on_axis(point, axis, ray_origin, ray_direction) -> float:
        """Hur långt längs `axis` från `point` strålen pekar.

        Standardlösningen för att dra något längs en given riktning: hitta den
        punkt på linjen som ligger närmast blickstrålen.
        """
        axis = np.asarray(axis, dtype=float)
        w0 = np.asarray(point, dtype=float) - np.asarray(ray_origin, dtype=float)
        a = float(np.dot(axis, axis))
        b = float(np.dot(axis, ray_direction))
        c = float(np.dot(ray_direction, ray_direction))
        d = float(np.dot(axis, w0))
        e = float(np.dot(ray_direction, w0))
        denominator = a * c - b * b
        if abs(denominator) < 1e-9:
            return 0.0
        return float((b * e - c * d) / denominator)

    # -- mus och kameravinklar --------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt-namn
        position = event.position() if hasattr(event, "position") else event.localPos()
        self._press_pos = position
        self._drag = None

        if event.button() in (Qt.LeftButton, Qt.RightButton):
            hit = self.plane_at(position.x(), position.y())
            if hit is not None:
                number, point = hit
                self._drag = {
                    "plane": number,
                    "point": np.asarray(point, dtype=float),
                    "last": position,
                    "tilt": bool(event.modifiers() & Qt.ShiftModifier),
                }
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt-namn
        """Drar användaren i ett plan flyttas det; annars vrids modellen.

        pyqtgraph använder bara vänster knapp till att rotera; många väntar sig
        att kunna dra med höger. Ctrl gör att dragningen flyttar vyn i stället.
        """
        if self._drag is not None:
            self._drag_plane(event)
            return

        if event.buttons() & Qt.RightButton:
            position = (
                event.position() if hasattr(event, "position") else event.localPos()
            )
            if not hasattr(self, "mousePos"):
                self.mousePos = position
            diff = position - self.mousePos
            self.mousePos = position
            if event.modifiers() & Qt.ControlModifier:
                self.pan(diff.x(), diff.y(), 0, relative="view")
            else:
                self.orbit(-diff.x(), diff.y())
            return
        super().mouseMoveEvent(event)

    def _drag_plane(self, event) -> None:
        """Flytta eller vinkla planet som användaren håller i."""
        position = event.position() if hasattr(event, "position") else event.localPos()
        drag = self._drag
        number = drag["plane"]
        if not (0 <= number < len(self._planes)):
            return
        plane = self._planes[number]

        if drag["tilt"] or (event.modifiers() & Qt.ShiftModifier):
            delta = position - drag["last"]
            drag["last"] = position
            self.plane_tilted.emit(number, float(delta.x()), float(delta.y()))
            return

        origin, direction = self.ray_at(position.x(), position.y())
        moved = self._closest_on_axis(drag["point"], plane.unit_normal, origin, direction)
        if abs(moved) < 1e-6:
            return
        drag["point"] = drag["point"] + np.asarray(plane.unit_normal, dtype=float) * moved
        self.plane_dragged.emit(number, float(moved))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt-namn
        """Ett högerklick utan dragning öppnar menyn med färdiga vinklar."""
        if self._drag is not None:
            number = self._drag["plane"]
            self._drag = None
            self.plane_released.emit(number)
            return

        if event.button() == Qt.RightButton and not self._was_dragged(event):
            self.show_view_menu(event.globalPosition().toPoint())
            return
        super().mouseReleaseEvent(event)

    def _was_dragged(self, event) -> bool:
        if self._press_pos is None:
            return False
        position = event.position() if hasattr(event, "position") else event.localPos()
        moved = position - self._press_pos
        return abs(moved.x()) > CLICK_SLOP_PX or abs(moved.y()) > CLICK_SLOP_PX

    def build_view_menu(self) -> QMenu:
        """Menyn med färdiga kameravinklar."""
        menu = QMenu(self)
        for name, (azimuth, elevation) in STANDARD_VIEWS.items():
            action = QAction(name, menu)
            action.triggered.connect(
                lambda _checked=False, a=azimuth, e=elevation: self.set_view(a, e)
            )
            menu.addAction(action)
        menu.addSeparator()
        fit = QAction("Anpassa till modellen", menu)
        fit.triggered.connect(self.fit_view)
        menu.addAction(fit)
        return menu

    def show_view_menu(self, global_position) -> None:
        self.build_view_menu().exec(global_position)

    def set_view(self, azimuth: float, elevation: float) -> None:
        """Ställ kameran i en given vinkel och behåll avståndet."""
        self.setCameraPosition(azimuth=float(azimuth), elevation=float(elevation))
        self.update()

    def content_bounds(self) -> np.ndarray | None:
        """Bounding box för det som visas just nu."""
        meshes = [item for item in (self._part_items or []) if item is not None]
        if not meshes and self._model_item is not None:
            meshes = [self._model_item]
        if not meshes:
            return None
        corners = []
        for item in meshes:
            data = item.opts.get("meshdata")
            if data is None:
                continue
            vertices = np.asarray(data.vertexes(), dtype=float)
            transform = np.asarray(item.transform().data(), dtype=float).reshape(4, 4).T
            moved = trimesh.transform_points(vertices, transform)
            corners.append([moved.min(axis=0), moved.max(axis=0)])
        if not corners:
            return None
        corners = np.array(corners)
        return np.array([corners[:, 0].min(axis=0), corners[:, 1].max(axis=0)])

    def fit_view(self) -> None:
        """Zooma så att allt som visas får plats i rutan."""
        bounds = self.content_bounds()
        if bounds is not None:
            self.frame_on(bounds)

    # -- allt -------------------------------------------------------------

    def clear_all(self) -> None:
        self.clear_parts()
        self.clear_planes()
        if self._model_item is not None:
            self.removeItem(self._model_item)
            self._model_item = None
