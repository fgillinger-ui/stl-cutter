"""Bilder på fogtyperna.

Bilderna genereras av `tools/render_joints.py` från den riktiga foggeometrin
och ligger som SVG i `assets/joints/`. Saknas de - till exempel efter en
ofullständig installation - ska gränssnittet fungera ändå, bara utan bild.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

log = logging.getLogger(__name__)

#: Var bilderna kan ligga: i repot bredvid paketet, eller inuti det.
_CANDIDATES = (
    Path(__file__).resolve().parents[2] / "assets" / "joints",
    Path(__file__).resolve().parents[1] / "assets" / "joints",
)

#: Kort förklaring per fogtyp, till bildvisningen.
DESCRIPTIONS = {
    "none": "Snittet lämnas plant och delarna limmas. Används när materialet är "
    "tunnare än 4 mm - då får ingen fog plats.",
    "puzzle": "En vågig skarv genom hela tjockleken, som en pusselbit. Låser "
    "delarna i sidled. Passar plattor på 4-8 mm.",
    "dovetail": "Ett spår som är bredare längst ut än vid halsen. Delarna skjuts "
    "ihop i sidled och kan inte dras isär rakt ut. Kräver minst 8 mm.",
    "pins": "Tappar på ena delen som passar i hål i den andra. Centrerar delarna "
    "vid limning. Kräver minst 6 mm.",
    "screw": "M3-skruv genom ena delen ner i en mutter som ligger i en "
    "sexkantsficka i den andra. Den enda fogen som går att ta isär igen.",
}


def image_path(joint_type: str, plain: bool = True) -> Path | None:
    """Sökvägen till bilden för en fogtyp, eller None om den saknas.

    `plain` väljer varianten utan inbränd rubrik, för lägen där namnet redan
    står bredvid bilden. Saknas den används den textade som reserv.
    """
    names = [f"{joint_type}-plain.svg", f"{joint_type}.svg"]
    if not plain:
        names.reverse()
    for folder in _CANDIDATES:
        for name in names:
            candidate = folder / name
            if candidate.exists():
                return candidate
    return None


def pixmap(joint_type: str, width: int = 320, plain: bool = True) -> QPixmap | None:
    """Rendera fogtypens bild i angiven bredd."""
    path = image_path(joint_type, plain=plain)
    if path is None:
        log.debug("Ingen bild för fogtypen %r.", joint_type)
        return None

    renderer = QSvgRenderer(str(path))
    if not renderer.isValid():  # pragma: no cover - trasig fil
        log.warning("Bilden %s gick inte att läsa.", path)
        return None

    size = renderer.defaultSize()
    height = max(1, round(width * size.height() / max(size.width(), 1)))
    image = QImage(width, height, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    return QPixmap.fromImage(image)
