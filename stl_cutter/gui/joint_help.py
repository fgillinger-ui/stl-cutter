"""Fönstret som visar alla fogtyper med bild och förklaring."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.recommender import JOINT_TYPES
from . import joint_images

TITLE = "Fogtyper - vad är vad?"

INTRO = (
    "Programmet väljer fogtyp åt dig utifrån hur snittytan ser ut, men du kan "
    "alltid byta i listan. Den blå delen har hanen, den orange har honan."
)


class JointHelpDialog(QDialog):
    """En sida per fogtyp: bild, namn och när den passar."""

    def __init__(self, labels: dict[str, str], parent=None, image_width: int = 420):
        super().__init__(parent)
        self.setWindowTitle(TITLE)
        self.resize(520, 720)

        content = QWidget()
        layout = QVBoxLayout(content)

        intro = QLabel(INTRO)
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #555; padding-bottom: 6px;")
        layout.addWidget(intro)

        self.shown: list[str] = []
        for joint_type in JOINT_TYPES:
            heading = QLabel(f"<b>{labels.get(joint_type, joint_type)}</b>")
            layout.addWidget(heading)

            picture = joint_images.pixmap(joint_type, width=image_width)
            if picture is not None:
                image_label = QLabel()
                image_label.setPixmap(picture)
                image_label.setAlignment(Qt.AlignLeft)
                layout.addWidget(image_label)
                self.shown.append(joint_type)

            description = QLabel(joint_images.DESCRIPTIONS.get(joint_type, ""))
            description.setWordWrap(True)
            description.setStyleSheet("padding-bottom: 12px;")
            layout.addWidget(description)

        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("Stäng")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        outer = QVBoxLayout(self)
        outer.addWidget(scroll, 1)
        outer.addWidget(buttons)
