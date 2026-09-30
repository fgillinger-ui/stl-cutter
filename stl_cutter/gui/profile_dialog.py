"""Dialogen för slicerprofilen: vad den sätter, och vad den bygger på.

Överst står inställningarna profilen kommer att sätta, med värde och skäl -
samma rader som `core.load.print_advice` ger, så det som visas är det som
skrivs. Det som inte går att lägga i en profil (orienteringen) står med i
samma tabell, markerat "ställs in manuellt".

Under tabellen väljs vad profilen ärver från. Programmet letar upp de slicers
som körts på datorn och läser deras systemprofiler, så att namnen väljs ur en
lista i stället för att skrivas av tecken för tecken - ett felstavat namn får
importen att vägra utan att säga varför. Hittas ingen slicer går det att skriva
namnen själv.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..core import load as load_core
from ..core import profile as profile_core

#: En vanlig PETG-temperatur. Bara ett startvärde i rutan - det som gäller är
#: vad användaren faktiskt kör.
DEFAULT_TEMP_C = 235.0

#: Vad kolumnen "Hamnar i" säger för varje sorts rad.
TARGET_LABELS = {
    load_core.PROCESS: "Processprofil",
    load_core.FILAMENT: "Filamentprofil",
}
MANUAL = "ställs in manuellt"
DEFAULT_NAME = "Bärande delar"


@dataclass
class ProfileChoice:
    """Svaret från dialogen."""

    base_profile: str = ""
    filament_profile: str = ""
    filament_temp_c: float = 0.0
    #: Datamappen för den valda slicern, eller tomt när ingen hittades.
    slicer: str = ""
    #: Lägg profilerna direkt i slicerns användarmapp.
    install_direct: bool = False
    #: Skriv också filer att importera för hand, i en mapp man väljer.
    export_files: bool = True
    #: Vad den nya profilen ska heta i slicern.
    name: str = "Bärande delar"

    @property
    def has_filament(self) -> bool:
        return bool(self.filament_profile.strip()) and self.filament_temp_c > 0


def _first_sentence(text: str) -> str:
    head, dot, _rest = text.partition(". ")
    return head + "." if dot else text


class ProfileDialog(QDialog):
    """Förhandsvisning och val av basprofiler i ett svep."""

    def __init__(
        self,
        parent=None,
        choice: ProfileChoice | None = None,
        load: load_core.LoadCase | None = None,
        nozzle_mm: float = 0.4,
        slicers: list[profile_core.SlicerInstall] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Slicerprofil för styrka")
        self.resize(760, 640)
        choice = choice or ProfileChoice()
        self._load = load
        self._nozzle_mm = nozzle_mm
        self.slicers = profile_core.find_slicers() if slicers is None else list(slicers)

        layout = QVBoxLayout(self)

        # --- Förhandsvisningen -------------------------------------------
        heading = QLabel(
            "<b>Det här sätter profilen</b> - tumregler för en del som ska bära, "
            "inte beräkningar. Allt annat ärvs från profilen du väljer nedanför."
        )
        heading.setWordWrap(True)
        layout.addWidget(heading)

        self.preview = QTableWidget(0, 4)
        self.preview.setHorizontalHeaderLabels(["Inställning", "Värde", "Varför", "Hamnar i"])
        self.preview.verticalHeader().setVisible(False)
        self.preview.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.preview.setWordWrap(True)
        header = self.preview.horizontalHeader()
        # Fasta bredder utom för skälet: långa värden radbryts i stället för
        # att tränga ihop motiveringen.
        for column, width in ((0, 160), (1, 150), (3, 150)):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
            self.preview.setColumnWidth(column, width)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        layout.addWidget(self.preview, 1)

        # --- Basprofilerna -----------------------------------------------
        form = QFormLayout()
        self.slicer_combo = QComboBox()
        for install in self.slicers:
            self.slicer_combo.addItem(install.display_name, str(install.data_dir))
        if self.slicers:
            remembered = self.slicer_combo.findData(choice.slicer)
            self.slicer_combo.setCurrentIndex(max(0, remembered))
            form.addRow("Slicer:", self.slicer_combo)
        else:
            self.slicer_combo.setVisible(False)

        self.printer_label = QLabel("")
        self.printer_label.setWordWrap(True)
        self.printer_label.setStyleSheet("color: #555;")
        form.addRow("", self.printer_label)

        # Redigerbara rullgardiner: listan när en slicer hittats, fritext
        # annars - och fritext även då, om namnet inte står med.
        self.process_combo = QComboBox()
        self.process_combo.setEditable(True)
        self.process_combo.setInsertPolicy(QComboBox.NoInsert)
        self.base_edit = self.process_combo.lineEdit()
        self.base_edit.setPlaceholderText("0.20mm Standard @FF C5")
        form.addRow("Processprofil:", self.process_combo)

        self.filament_combo = QComboBox()
        self.filament_combo.setEditable(True)
        self.filament_combo.setInsertPolicy(QComboBox.NoInsert)
        self.filament_edit = self.filament_combo.lineEdit()
        self.filament_edit.setPlaceholderText("Flashforge HS PETG @FF C5P (frivilligt)")
        form.addRow("Filamentprofil:", self.filament_combo)

        self.temp_spin = QDoubleSpinBox()
        self.temp_spin.setRange(150.0, 350.0)
        self.temp_spin.setDecimals(0)
        self.temp_spin.setSuffix(" °C")
        self.temp_spin.setValue(choice.filament_temp_c or DEFAULT_TEMP_C)
        self.temp_spin.setToolTip(
            "Den temperatur du brukar köra filamentet i. Profilen lägger på "
            f"{load_core.TEMPERATURE_BOOST_C} grader för bättre lagerhäftning. "
            "Väljer du en filamentprofil ur listan hämtas värdet därifrån."
        )
        form.addRow("Brukar köras i:", self.temp_spin)

        self.name_edit = QLineEdit(choice.name or DEFAULT_NAME)
        self.name_edit.setToolTip(
            "Namnet profilen får i slicerns rullgardiner. Samma namn som en\n"
            "egen profil skriver över den (du får frågan först)."
        )
        form.addRow("Namn på profilen:", self.name_edit)
        layout.addLayout(form)

        self.name_warning = QLabel("")
        self.name_warning.setWordWrap(True)
        self.name_warning.setStyleSheet("color: #b00;")
        layout.addWidget(self.name_warning)

        # --- Vart profilen tar vägen -------------------------------------
        self.install_check = QCheckBox("Lägg in profilen direkt i slicern")
        self.install_check.setToolTip(
            "Skriver profilerna i slicerns egen mapp för användarprofiler.\n"
            "Slicern måste startas om för att de ska synas."
        )
        self.install_check.setEnabled(bool(self.slicers))
        self.install_check.setChecked(bool(self.slicers))
        layout.addWidget(self.install_check)

        self.export_check = QCheckBox("Spara även filer att importera för hand…")
        self.export_check.setToolTip(
            "JSON-filer för Arkiv → Importera → Importera konfiguration, och en\n"
            "textfil med värdena att knappa in om importen inte går."
        )
        self.export_check.setChecked(not self.slicers or choice.export_files)
        layout.addWidget(self.export_check)

        if not self.slicers:
            note = QLabel(
                "Ingen slicer hittades i ~/.config eller bland Flatpak-apparna. "
                "Skriv namnen precis som de står i slicerns rullgardiner."
            )
            note.setWordWrap(True)
            note.setStyleSheet("color: gray;")
            layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.slicer_combo.currentIndexChanged.connect(self._fill_profiles)
        self.base_edit.textChanged.connect(self._check_names)
        self.name_edit.textChanged.connect(self._check_names)
        self.filament_edit.textChanged.connect(self._on_filament_changed)
        self.temp_spin.valueChanged.connect(self._refresh_preview)

        self._fill_profiles()
        self.base_edit.setText(choice.base_profile or self._default_process())
        self.filament_edit.setText(choice.filament_profile)
        if choice.filament_temp_c:
            self.temp_spin.setValue(choice.filament_temp_c)
        self._check_names()
        self._refresh_preview()

    # ------------------------------------------------------------------

    def current_slicer(self) -> profile_core.SlicerInstall | None:
        if not self.slicers:
            return None
        return self.slicers[max(0, self.slicer_combo.currentIndex())]

    def _fill_profiles(self, *_args) -> None:
        """Fyll rullgardinerna med namnen ur den valda slicern."""
        install = self.current_slicer()
        base, filament = self.base_edit.text(), self.filament_edit.text()
        for combo in (self.process_combo, self.filament_combo):
            combo.blockSignals(True)
            combo.clear()
        if install is not None:
            printer = install.selected_printer
            processes = profile_core.system_profiles(install, "process", printer)
            filaments = profile_core.system_profiles(install, "filament", printer)
            # Egna profiler först: det är dem man oftast menar.
            self.process_combo.addItems(profile_core.user_profile_names(install, "process", printer))
            self.process_combo.addItems(processes)
            self.filament_combo.addItem("")  # ingen filamentprofil
            self.filament_combo.addItems(
                profile_core.user_profile_names(install, "filament", printer)
            )
            self.filament_combo.addItems(filaments)
            self.printer_label.setText(
                f"Profilerna är filtrerade på skrivaren {printer}, som är vald i slicern."
                if printer
                else "Ingen skrivare vald i slicern - alla profiler visas."
            )
        for combo in (self.process_combo, self.filament_combo):
            combo.blockSignals(False)
        self.base_edit.setText(base)
        self.filament_edit.setText(filament)
        self._check_names()

    def _default_process(self) -> str:
        """Standardprofilen med 0,20 mm lager om den finns, annars den första."""
        names = [self.process_combo.itemText(i) for i in range(self.process_combo.count())]
        standard = [n for n in names if n.startswith("0.20mm Standard")]
        return (standard or names or [""])[0]

    def _known(self, kind: str) -> set[str] | None:
        install = self.current_slicer()
        if install is None:
            return None
        return set(install.all_profiles(kind))

    def _check_names(self, *_args) -> None:
        """Varna direkt om ett namn inte finns, och säg vad en egen bas betyder."""
        install = self.current_slicer()
        process = self.base_edit.text().strip()
        filament = self.filament_edit.text().strip()
        name = self.name_edit.text().strip()
        errors: list[str] = []
        notes: list[str] = []

        for kind, label, value in (
            ("process", "Processprofilen", process),
            ("filament", "Filamentprofilen", filament),
        ):
            if not value or install is None:
                continue
            if value not in install.all_profiles(kind):
                errors.append(f"{label} {value!r} finns inte i slicern.")
            elif value in install.user_profiles(kind):
                notes.append(
                    f"{value!r} är en egen profil: dina inställningar i den följer "
                    "med, och hållfastheten läggs ovanpå."
                )
        if errors:
            errors.append("Importen vägrar profiler vars basprofil inte finns.")
        if not name:
            errors.append("Ge profilen ett namn.")
        elif install is not None and name in install.profiles("process"):
            errors.append(f"{name!r} är en av slicerns egna profiler - välj ett annat namn.")

        self.name_warning.setStyleSheet("color: #b00;" if errors else "color: #555;")
        self.name_warning.setText(" ".join(errors + notes))

    def _on_filament_changed(self, *_args) -> None:
        """Hämta filamentets vanliga temperatur ur basprofilen, om den finns."""
        install = self.current_slicer()
        name = self.filament_edit.text().strip()
        if install is not None and name:
            temp = profile_core.inherited_value(
                install.all_profiles("filament"), name, "nozzle_temperature"
            )
            try:
                value = float(temp[0] if isinstance(temp, list) else temp)
            except (TypeError, ValueError, IndexError):
                value = 0.0
            if value > 0:
                self.temp_spin.setValue(value)
        self._check_names()
        self._refresh_preview()

    def _refresh_preview(self, *_args) -> None:
        """Tabellen överst, från samma källa som profilen skrivs ur."""
        rows = self.preview_rows()
        self.preview.setRowCount(len(rows))
        for row, cells in enumerate(rows):
            for column, text in enumerate(cells):
                # Kort motivering i cellen, hela skälet när man håller musen över.
                shown = _first_sentence(text) if column == 2 else text
                item = QTableWidgetItem(shown)
                item.setToolTip(text)
                if column == 3 and text.startswith(MANUAL):
                    item.setForeground(Qt.darkRed)
                self.preview.setItem(row, column, item)
        self.preview.resizeRowsToContents()

    def preview_rows(self) -> list[tuple[str, str, str, str]]:
        """(inställning, värde, skäl, hamnar i) för varje rad i tabellen."""
        if self._load is None or not self._load.active:
            return [("Ingen last angiven", "", "Kryssa i Belastning och ange vikten.", "")]
        has_filament = bool(self.filament_edit.text().strip())
        temp = float(self.temp_spin.value()) if has_filament else 0.0
        rows = []
        for setting in load_core.print_advice(self._load, self._nozzle_mm, normal_temp_c=temp):
            if setting.target == load_core.FILAMENT and not has_filament:
                where = f"{MANUAL} (ingen filamentprofil vald)"
            elif setting.manual:
                where = MANUAL
            else:
                where = TARGET_LABELS.get(setting.target, setting.target)
            rows.append((setting.name, setting.value, setting.why, where))
        return rows

    def choice(self) -> ProfileChoice:
        """Det användaren valde."""
        filament = self.filament_edit.text().strip()
        install = self.current_slicer()
        return ProfileChoice(
            base_profile=self.base_edit.text().strip(),
            filament_profile=filament,
            filament_temp_c=float(self.temp_spin.value()) if filament else 0.0,
            slicer=str(install.data_dir) if install else "",
            install_direct=bool(install) and self.install_check.isChecked(),
            export_files=self.export_check.isChecked(),
            name=self.name_edit.text().strip() or DEFAULT_NAME,
        )
