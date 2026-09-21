"""Dialogen som frågar vad slicerprofilen ska bygga på.

Tre uppgifter behövs, och de hör ihop: vilken processprofil den ärver från,
vilken filamentprofil, och vilken temperatur filamentet brukar köras i. De
ställdes förut som tre modala frågor efter varandra, vilket både är omständligt
och lätt att avbryta halvvägs. Här är de en enda ruta med Avbryt.

Bara den första är obligatorisk: utan basprofilen vet profilen ingenting om
skrivaren. Filamentfälten kan lämnas tomma - då skrivs processprofilen ändå,
och programmet säger vad som utelämnades.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

#: En vanlig PETG-temperatur. Bara ett startvärde i rutan - det som gäller är
#: vad användaren faktiskt kör.
DEFAULT_TEMP_C = 235.0


@dataclass
class ProfileChoice:
    """Svaret från dialogen."""

    base_profile: str = ""
    filament_profile: str = ""
    filament_temp_c: float = 0.0

    @property
    def has_filament(self) -> bool:
        return bool(self.filament_profile.strip()) and self.filament_temp_c > 0


class ProfileDialog(QDialog):
    """Fråga efter basprofil och filament i ett svep."""

    def __init__(self, parent=None, choice: ProfileChoice | None = None):
        super().__init__(parent)
        self.setWindowTitle("Spara slicerprofil")
        choice = choice or ProfileChoice()

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Profilen sätter bara det som rör hållfasthet och ärver resten från "
            "en profil du redan använder. Skriv namnet precis som det står i "
            "slicerns rullgardin - stavas det fel vägrar importen utan att säga "
            "varför."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.base_edit = QLineEdit(choice.base_profile)
        self.base_edit.setPlaceholderText("0.20mm Standard @FF C5")
        form.addRow("Processprofil:", self.base_edit)

        self.filament_edit = QLineEdit(choice.filament_profile)
        self.filament_edit.setPlaceholderText("Flashforge HS PETG @FF C5 (frivilligt)")
        form.addRow("Filamentprofil:", self.filament_edit)

        self.temp_spin = QDoubleSpinBox()
        self.temp_spin.setRange(150.0, 350.0)
        self.temp_spin.setDecimals(0)
        self.temp_spin.setSuffix(" °C")
        self.temp_spin.setValue(choice.filament_temp_c or DEFAULT_TEMP_C)
        self.temp_spin.setToolTip(
            "Den temperatur du brukar köra filamentet i. Profilen lägger på "
            "några grader för bättre lagerhäftning - vad som är normalt beror "
            "på om du kör PLA, PETG eller ASA, och det kan programmet inte veta."
        )
        form.addRow("Brukar köras i:", self.temp_spin)
        layout.addLayout(form)

        note = QLabel(
            "Lämna filamentfälten tomma om du bara vill ha processinställningarna."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choice(self) -> ProfileChoice:
        """Det användaren skrev in."""
        filament = self.filament_edit.text().strip()
        return ProfileChoice(
            base_profile=self.base_edit.text().strip(),
            filament_profile=filament,
            filament_temp_c=float(self.temp_spin.value()) if filament else 0.0,
        )
