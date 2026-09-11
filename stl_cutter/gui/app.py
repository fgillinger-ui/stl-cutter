"""Huvudfönstret.

Vänsterpanelen leder användaren uppifrån och ner: modell, skrivare, montering,
förslag, kapa. Högerpanelen visar modellen i 3D. Allt tungt arbete körs i en
bakgrundstråd så att fönstret aldrig fryser.
"""

from __future__ import annotations

import logging
from functools import partial
from pathlib import Path

import numpy as np
import trimesh
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core import exporter, mesh_io
from ..core.cutter import cut_mesh, parts_fit
from ..core.planner import (
    AXIS_NAMES,
    dominant_axis,
    make_cut,
    oriented_mesh,
    plan_from_cuts,
    plan_splits,
)
from ..core.printers import PrinterProfile, get_printer, load_printers, save_profile
from ..core.recommender import JOINT_TYPES, build_recommendation
from . import joint_images
from .joint_help import JointHelpDialog
from .paths import log_file
from .settings import Settings
from .view3d import ModelView
from .workers import Worker

log = logging.getLogger(__name__)

WINDOW_TITLE = "STL Cutter - dela upp modeller för 3D-utskrift"

JOINT_LABELS = {
    "none": "Ingen fog (limmas)",
    "puzzle": "Pusselprofil",
    "dovetail": "Laxstjärt",
    "pins": "Styrpinnar",
    "screw": "Skruv med mutter",
}

MAX_EXPLODE_MM = 200

#: Bredd på bilden bredvid motiveringen.
JOINT_THUMBNAIL_WIDTH = 180

#: Största lutning som går att ställa in i tabellen.
MAX_TILT_DEG = 60.0

#: Stoppkantens standardhöjd när den slås på.
DEFAULT_STOP_MM = 6.0

#: Hur långt delarna sprängs isär automatiskt vid förhandsgranskning.
DEFAULT_PREVIEW_EXPLODE_MM = 40

#: Hur mycket ett plan vinklas per pixel vid Shift+dragning.
TILT_DEGREES_PER_PIXEL = 0.35

#: Kolumner i snittabellen.
COLUMN_INDEX, COLUMN_AXIS, COLUMN_POSITION, COLUMN_JOINT, COLUMN_MOTIVATION = range(5)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings | None = None):
        super().__init__()
        self.settings = settings or Settings.load()
        self.mesh_info = None
        self.plan = None
        self.result = None
        self.worker: Worker | None = None
        #: Sant medan tabellen ritas om, så att signaler inte studsar tillbaka.
        self._filling = False

        self.setWindowTitle(WINDOW_TITLE)
        self.setAcceptDrops(True)
        self.resize(1280, 820)
        self._build_ui()
        self._load_printers()
        self._apply_settings()
        self.status("Öppna en STL- eller 3MF-fil för att börja.")
        self.status(
            "Tips: dra med musen i 3D-vyn för att vrida modellen, "
            "högerklicka för färdiga vinklar."
        )

    # ------------------------------------------------------------------
    # Uppbyggnad
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Horizontal)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setSpacing(6)
        layout.addWidget(self._section_model())
        layout.addWidget(self._section_printer())
        layout.addWidget(self._section_assembly())
        layout.addWidget(self._section_suggestions())
        layout.addWidget(self._section_export())
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(470)
        splitter.addWidget(scroll)

        splitter.addWidget(self._right_side())
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([470, 810])
        self.setCentralWidget(splitter)

        self._build_statusbar()
        self._build_menu()

    def _section_model(self) -> QGroupBox:
        box = QGroupBox("1. Modell")
        layout = QVBoxLayout(box)

        self.open_button = QPushButton("Öppna fil…")
        self.open_button.clicked.connect(self.choose_model)
        layout.addWidget(self.open_button)

        hint = QLabel("Du kan också dra och släppa en STL- eller 3MF-fil i fönstret.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        layout.addWidget(hint)

        self.model_label = QLabel("Ingen modell öppnad.")
        self.model_label.setWordWrap(True)
        layout.addWidget(self.model_label)
        return box

    def _section_printer(self) -> QGroupBox:
        box = QGroupBox("2. Skrivare")
        layout = QVBoxLayout(box)

        self.printer_combo = QComboBox()
        self.printer_combo.currentTextChanged.connect(self.on_printer_changed)
        layout.addWidget(self.printer_combo)

        # Byggvolymen på en rad - annars tar panelen upp hela fönsterhöjden.
        self.bed_x = self._spin(10, 2000, "")
        self.bed_y = self._spin(10, 2000, "")
        self.bed_z = self._spin(10, 2000, "")
        self.margin = self._spin(0, 100, " mm")
        for spin in (self.bed_x, self.bed_y, self.bed_z, self.margin):
            spin.valueChanged.connect(self._on_printer_fields_changed)

        bed_row = QHBoxLayout()
        bed_row.addWidget(QLabel("Byggvolym (mm):"))
        for label, spin in (("X", self.bed_x), ("Y", self.bed_y), ("Z", self.bed_z)):
            spin.setToolTip({"X": "Bredd", "Y": "Djup", "Z": "Höjd"}[label])
            bed_row.addWidget(QLabel(label))
            bed_row.addWidget(spin, 1)
        layout.addLayout(bed_row)

        margin_row = QHBoxLayout()
        margin_row.addWidget(QLabel("Marginal:"))
        margin_row.addWidget(self.margin, 1)
        self.save_profile_button = QPushButton("Spara som ny profil")
        self.save_profile_button.clicked.connect(self.save_printer_profile)
        margin_row.addWidget(self.save_profile_button)
        layout.addLayout(margin_row)
        return box

    def _section_assembly(self) -> QGroupBox:
        box = QGroupBox("3. Montering")
        layout = QVBoxLayout(box)

        self.glue_radio = QRadioButton("Limmas ihop")
        self.demount_radio = QRadioButton("Ska kunna tas isär")
        self.glue_radio.setChecked(True)
        self.intent_group = QButtonGroup(self)
        self.intent_group.addButton(self.glue_radio)
        self.intent_group.addButton(self.demount_radio)

        choice_row = QHBoxLayout()
        choice_row.addWidget(self.glue_radio)
        choice_row.addWidget(self.demount_radio)
        choice_row.addStretch(1)
        layout.addLayout(choice_row)

        clearance_row = QHBoxLayout()
        clearance_row.addWidget(QLabel("Tolerans (spel i fogen):"))
        self.clearance = self._spin(0.0, 2.0, " mm", decimals=2, step=0.05)
        clearance_row.addWidget(self.clearance, 1)
        layout.addLayout(clearance_row)
        return box

    def _section_suggestions(self) -> QGroupBox:
        box = QGroupBox("4. Förslag")
        layout = QVBoxLayout(box)

        self.analyse_button = QPushButton("Analysera")
        self.analyse_button.setEnabled(False)
        self.analyse_button.clicked.connect(self.start_analysis)
        layout.addWidget(self.analyse_button)

        self.cut_table = QTableWidget(0, 5)
        self.cut_table.setHorizontalHeaderLabels(
            ["Snitt", "Axel", "Position", "Fogtyp", "Motivering"]
        )
        self.cut_table.verticalHeader().setVisible(False)
        self.cut_table.horizontalHeader().setSectionResizeMode(
            COLUMN_MOTIVATION, QHeaderView.Stretch
        )
        self.cut_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.cut_table.setMinimumHeight(70)
        self.cut_table.setMaximumHeight(90)
        self.cut_table.currentCellChanged.connect(self._on_row_selected)
        layout.addWidget(self.cut_table)

        # Manuell redigering: lägg till, ta bort och flytta snitt själv.
        buttons = QHBoxLayout()
        self.add_cut_button = QPushButton("Lägg till snitt")
        self.add_cut_button.setEnabled(False)
        self.add_cut_button.clicked.connect(self.add_cut)
        buttons.addWidget(self.add_cut_button)

        self.remove_cut_button = QPushButton("Ta bort snitt")
        self.remove_cut_button.setEnabled(False)
        self.remove_cut_button.clicked.connect(self.remove_cut)
        buttons.addWidget(self.remove_cut_button)

        self.straighten_button = QPushButton("Räta upp")
        self.straighten_button.setEnabled(False)
        self.straighten_button.setToolTip("Ta bort lutningen på det markerade snittet")
        self.straighten_button.clicked.connect(self.straighten_cut)
        buttons.addWidget(self.straighten_button)

        self.reset_cuts_button = QPushButton("Räkna ut åt mig")
        self.reset_cuts_button.setEnabled(False)
        self.reset_cuts_button.setToolTip(
            "Kasta de manuella snitten och låt programmet räkna ut dem igen"
        )
        self.reset_cuts_button.clicked.connect(self.start_analysis)
        buttons.addWidget(self.reset_cuts_button)
        layout.addLayout(buttons)

        self.plan_summary = QLabel("")
        self.plan_summary.setWordWrap(True)
        layout.addWidget(self.plan_summary)

        # Motiveringen får inte plats i kolumnen - visa hela för markerad rad,
        # med en bild som visar vad fogtypen faktiskt är.
        details = QHBoxLayout()

        self.joint_image = QLabel()
        self.joint_image.setAlignment(Qt.AlignTop)
        self.joint_image.setFixedWidth(JOINT_THUMBNAIL_WIDTH)
        self.joint_image.setVisible(False)
        details.addWidget(self.joint_image)

        self.motivation_label = QLabel("Markera ett snitt för att läsa hela motiveringen.")
        self.motivation_label.setWordWrap(True)
        self.motivation_label.setMinimumHeight(48)
        self.motivation_label.setAlignment(Qt.AlignTop)
        self.motivation_label.setStyleSheet("color: #444; padding: 4px;")
        details.addWidget(self.motivation_label, 1)
        layout.addLayout(details)

        # Exakta inställningar för det markerade snittet.
        settings_row = QHBoxLayout()
        settings_row.addWidget(QLabel("Lutning:"))
        self.tilt_spin = self._spin(-MAX_TILT_DEG, MAX_TILT_DEG, "°", decimals=1, step=1.0)
        self.tilt_spin.setToolTip(
            "Vinkla snittet. 0 = rakt. Går också att göra med Shift och dra i planet."
        )
        self.tilt_spin.setEnabled(False)
        self.tilt_spin.valueChanged.connect(self._on_tilt_changed)
        settings_row.addWidget(self.tilt_spin)

        settings_row.addWidget(QLabel("kring"))
        self.tilt_axis_combo = QComboBox()
        self.tilt_axis_combo.setToolTip("Vilken axel snittet lutas kring")
        self.tilt_axis_combo.setEnabled(False)
        self.tilt_axis_combo.currentIndexChanged.connect(self._on_tilt_changed)
        settings_row.addWidget(self.tilt_axis_combo)

        settings_row.addSpacing(12)
        self.stop_check = QCheckBox("Stoppkant")
        self.stop_check.setToolTip(
            "Stäng botten på laxstjärtsspåret, så att delen glider in och tar emot "
            "mot material i stället för att bara hållas av friktion."
        )
        self.stop_check.setEnabled(False)
        self.stop_check.stateChanged.connect(self._on_stop_changed)
        settings_row.addWidget(self.stop_check)

        self.stop_spin = self._spin(1.0, 50.0, " mm", decimals=1, step=1.0)
        self.stop_spin.setValue(DEFAULT_STOP_MM)
        self.stop_spin.setEnabled(False)
        self.stop_spin.valueChanged.connect(self._on_stop_changed)
        settings_row.addWidget(self.stop_spin)
        settings_row.addStretch(1)
        layout.addLayout(settings_row)

        self.joint_help_button = QPushButton("Fogtyper - vad är vad?")
        self.joint_help_button.clicked.connect(self.show_joint_help)
        layout.addWidget(self.joint_help_button)
        return box

    def _section_export(self) -> QGroupBox:
        box = QGroupBox("5. Kapa och exportera")
        layout = QVBoxLayout(box)

        row = QHBoxLayout()
        self.output_label = QLabel("Ingen målmapp vald.")
        self.output_label.setWordWrap(True)
        choose = QPushButton("Välj målmapp…")
        choose.clicked.connect(self.choose_output_dir)
        row.addWidget(self.output_label, 1)
        row.addWidget(choose)
        layout.addLayout(row)

        self.preview_button = QPushButton("Förhandsgranska (kapar inte filen)")
        self.preview_button.setEnabled(False)
        self.preview_button.setToolTip(
            "Kapa modellen i minnet och visa delarna i sprängskiss - inga filer skrivs"
        )
        self.preview_button.clicked.connect(self.start_preview)
        layout.addWidget(self.preview_button)

        self.cut_button = QPushButton("Kapa och exportera")
        self.cut_button.setEnabled(False)
        self.cut_button.clicked.connect(self.start_cut)
        layout.addWidget(self.cut_button)
        return box

    def _right_side(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)

        self.view = ModelView(light_background=self.settings.light_background)
        self.view.plane_dragged.connect(self._on_plane_dragged)
        self.view.plane_tilted.connect(self._on_plane_tilted)
        self.view.plane_released.connect(self._on_plane_released)
        layout.addWidget(self.view, 1)

        controls = QHBoxLayout()
        self.bed_checkbox = QCheckBox("Visa byggplatta")
        self.bed_checkbox.stateChanged.connect(self.on_bed_toggled)
        controls.addWidget(self.bed_checkbox)

        self.light_checkbox = QCheckBox("Ljus bakgrund")
        self.light_checkbox.stateChanged.connect(self.on_background_toggled)
        controls.addWidget(self.light_checkbox)

        controls.addWidget(QLabel("Spräng isär:"))
        self.explode_slider = QSlider(Qt.Horizontal)
        self.explode_slider.setRange(0, MAX_EXPLODE_MM)
        self.explode_slider.valueChanged.connect(self.on_explode_changed)
        controls.addWidget(self.explode_slider, 1)
        self.explode_label = QLabel("0 mm")
        controls.addWidget(self.explode_label)
        layout.addLayout(controls)
        return container

    def _build_statusbar(self) -> None:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(6, 0, 6, 4)

        self.status_box = QPlainTextEdit()
        self.status_box.setReadOnly(True)
        self.status_box.setMaximumHeight(96)
        layout.addWidget(self.status_box)

        row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        self.cancel_button = QPushButton("Avbryt")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self.cancel_work)
        row.addWidget(self.progress, 1)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)

        self.statusBar().addPermanentWidget(container, 1)

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("&Arkiv")
        open_action = QAction("&Öppna modell…", self)
        open_action.triggered.connect(self.choose_model)
        menu.addAction(open_action)

        log_action = QAction("Visa &loggfilens plats", self)
        log_action.triggered.connect(
            lambda: self.status(f"Full logg skrivs till {log_file()}")
        )
        menu.addAction(log_action)

        menu.addSeparator()
        quit_action = QAction("A&vsluta", self)
        quit_action.triggered.connect(self.close)
        menu.addAction(quit_action)

    @staticmethod
    def _spin(low, high, suffix, decimals=1, step=1.0) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setDecimals(decimals)
        spin.setSingleStep(step)
        spin.setSuffix(suffix)
        return spin

    # ------------------------------------------------------------------
    # Inställningar och skrivare
    # ------------------------------------------------------------------

    def _load_printers(self) -> None:
        self.printer_combo.blockSignals(True)
        self.printer_combo.clear()
        for profile in load_printers():
            self.printer_combo.addItem(profile.name)
        self.printer_combo.blockSignals(False)

    def _apply_settings(self) -> None:
        index = self.printer_combo.findText(self.settings.printer)
        self.printer_combo.setCurrentIndex(max(index, 0))
        self.on_printer_changed(self.printer_combo.currentText())

        self.clearance.setValue(self.settings.clearance_mm)
        self.margin.setValue(self.settings.margin_mm)
        self.demount_radio.setChecked(self.settings.assembly_intent == "demountable")
        self.glue_radio.setChecked(self.settings.assembly_intent != "demountable")
        self.bed_checkbox.setChecked(self.settings.show_bed)
        self.light_checkbox.setChecked(self.settings.light_background)
        self.explode_slider.setValue(int(self.settings.explode_mm))
        if self.settings.last_output_dir:
            self.output_label.setText(self.settings.last_output_dir)

    def _on_printer_fields_changed(self, *_args) -> None:
        self._update_summary()
        self.on_bed_toggled()

    def on_printer_changed(self, name: str) -> None:
        if not name:
            return
        try:
            profile = get_printer(name)
        except KeyError as exc:
            self.status(str(exc), error=True)
            return
        self.bed_x.setValue(profile.bed_x)
        self.bed_y.setValue(profile.bed_y)
        self.bed_z.setValue(profile.bed_z)
        self.margin.setValue(profile.margin_mm)
        self.clearance.setValue(profile.clearance_mm)
        self.on_bed_toggled()

    def current_printer(self) -> PrinterProfile:
        """Profilen som den ser ut i rutorna just nu - ändringar räknas."""
        return PrinterProfile(
            name=self.printer_combo.currentText() or "Custom",
            bed_x=self.bed_x.value(),
            bed_y=self.bed_y.value(),
            bed_z=self.bed_z.value(),
            margin_mm=self.margin.value(),
            clearance_mm=self.clearance.value(),
        )

    def current_intent(self) -> str:
        return "demountable" if self.demount_radio.isChecked() else "glue"

    def save_printer_profile(self) -> None:
        name, ok = QInputDialog.getText(
            self, "Spara profil", "Namn på profilen:", text=self.printer_combo.currentText()
        )
        if not ok or not name.strip():
            return
        profile = self.current_printer()
        profile.name = name.strip()
        path = save_profile(profile)
        self._load_printers()
        self.printer_combo.setCurrentText(profile.name)
        self.status(f"Sparade profilen {profile.name!r} i {path}")

    # ------------------------------------------------------------------
    # Status och framsteg
    # ------------------------------------------------------------------

    def status(self, message: str, error: bool = False) -> None:
        prefix = "FEL: " if error else ""
        self.status_box.appendPlainText(f"{prefix}{message}")
        self.status_box.verticalScrollBar().setValue(
            self.status_box.verticalScrollBar().maximum()
        )
        (log.error if error else log.info)("%s", message)

    def _busy(self, busy: bool) -> None:
        self.progress.setVisible(busy)
        self.cancel_button.setVisible(busy)
        for widget in (
            self.open_button,
            self.analyse_button,
            self.cut_button,
            self.printer_combo,
            self.save_profile_button,
        ):
            widget.setEnabled(not busy and self._enabled_when_idle(widget))

    def _enabled_when_idle(self, widget) -> bool:
        if widget is self.analyse_button:
            return self.mesh_info is not None
        if widget is self.cut_button:
            return self.plan is not None
        return True

    def on_progress(self, fraction: float, message: str) -> None:
        self.progress.setValue(int(fraction * 100))
        self.statusBar().showMessage(message)

    def cancel_work(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.status("Avbryter…")

    def _start(self, work, on_success, description: str) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.status("Vänta tills det pågående arbetet är klart.", error=True)
            return
        self.status(description)
        self._busy(True)
        worker = Worker(work, self)
        worker.progressed.connect(self.on_progress)
        worker.succeeded.connect(on_success)
        worker.failed.connect(partial(self._on_failed))
        worker.cancelled.connect(self._on_cancelled)
        worker.finished.connect(lambda: self._busy(False))
        self.worker = worker
        worker.start()

    def _on_failed(self, message: str) -> None:
        self.status(message, error=True)
        self.status(f"Detaljer finns i loggen: {log_file()}")

    def _on_cancelled(self) -> None:
        self.status("Avbrutet.")
        self.statusBar().showMessage("Avbrutet")

    # ------------------------------------------------------------------
    # 1. Modell
    # ------------------------------------------------------------------

    def choose_model(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Öppna modell",
            self.settings.last_open_dir or str(Path.home()),
            "3D-modeller (*.stl *.3mf);;Alla filer (*)",
        )
        if path:
            self.load_model(Path(path))

    def load_model(self, path: Path) -> None:
        def work(progress=None):
            progress(0.1, f"Läser {path.name}")
            info = mesh_io.load_mesh(path)
            progress(1.0, "Modellen är inläst")
            return info

        self._start(work, self._on_model_loaded, f"Öppnar {path.name}…")
        self.settings.last_open_dir = str(path.parent)

    def _on_model_loaded(self, info) -> None:
        self.mesh_info = info
        self.plan = None
        self.result = None
        self.cut_table.setRowCount(0)
        self.cut_button.setEnabled(False)
        self.preview_button.setEnabled(False)
        self.analyse_button.setEnabled(True)
        self.add_cut_button.setEnabled(True)
        self.remove_cut_button.setEnabled(True)
        self.straighten_button.setEnabled(True)
        self.reset_cuts_button.setEnabled(True)
        self.plan_summary.setText("")

        x, y, z = info.extents_mm
        state = (
            "Meshen är hel."
            if info.watertight
            else f"Varning: {info.open_edges} trasiga kanter — se meddelandet nedan."
        )
        self.model_label.setText(
            f"<b>{info.path.name}</b><br>{x:.1f} × {y:.1f} × {z:.1f} mm<br>"
            f"Volym {info.volume_mm3 / 1000:.1f} cm³<br>{state}"
        )
        self.status(info.summary())
        for repair in info.repairs:
            self.status(f"Reparation: {repair}")
        if not info.watertight:
            self.status(
                f"Modellen har {info.open_edges} kanter som inte delas av exakt två "
                "trianglar och som inte gick att laga. "
                "Delarna kommer att ärva hålen, och din slicer kan klaga på dem. "
                "Vill du vara säker: laga modellen först (se Felsökning i README) "
                "och kapa om."
            )
        self.view.show_model(info.mesh)
        self.on_bed_toggled()

    # -- drag and drop ----------------------------------------------------

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt-namn
        if self._dropped_path(event) is not None:
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt-namn
        path = self._dropped_path(event)
        if path is not None:
            event.acceptProposedAction()
            self.load_model(path)

    @staticmethod
    def _dropped_path(event) -> Path | None:
        data = event.mimeData()
        if not data.hasUrls():
            return None
        for url in data.urls():
            path = Path(url.toLocalFile())
            if path.suffix.lower() in mesh_io.SUPPORTED_INPUT:
                return path
        return None

    # ------------------------------------------------------------------
    # 4. Analys och förslag
    # ------------------------------------------------------------------

    def start_analysis(self) -> None:
        if self.mesh_info is None:
            return
        printer = self.current_printer()
        intent = self.current_intent()
        mesh = self.mesh_info.mesh
        auto_orient = self.settings.auto_orient

        def work(progress=None):
            return plan_splits(
                mesh,
                printer,
                auto_orient=auto_orient,
                analyse=True,
                assembly_intent=intent,
                progress=progress,
            )

        self._start(work, self._on_plan_ready, "Analyserar snittlägen…")

    def _on_plan_ready(self, plan) -> None:
        self.plan = plan
        self.result = None
        self.cut_button.setEnabled(True)
        self.status(
            f"{plan.part_count} delar ({plan.divisions[0]}×{plan.divisions[1]}×"
            f"{plan.divisions[2]}), orientering: {plan.orientation_name}."
        )
        if not plan.needs_cutting:
            self.status("Modellen får plats som den är - ingen kapning behövs.")
        self.reset_cuts_button.setEnabled(True)
        self.preview_button.setEnabled(bool(plan.cuts))
        self._fill_table(plan)
        # Visa modellen i planens koordinatsystem - annars stämmer inte
        # snittplanen med modellen när den roterats automatiskt.
        self._refresh_planes()

    def _fill_table(self, plan) -> None:
        """Rita om tabellen från planen. Alla rader är redigerbara."""
        self._filling = True
        try:
            self.cut_table.setRowCount(len(plan.cuts))
            for row, cut in enumerate(plan.cuts):
                self._fill_row(row, cut)
        finally:
            self._filling = False

        self.cut_table.resizeColumnsToContents()
        self.cut_table.horizontalHeader().setSectionResizeMode(
            COLUMN_MOTIVATION, QHeaderView.Stretch
        )
        self._fit_table_height()
        self._update_summary()
        if plan.cuts:
            self.cut_table.setCurrentCell(0, 0)
            self._show_motivation(0)
        else:
            self.motivation_label.setText(
                "Inga snitt. Klicka <b>Lägg till snitt</b> för att placera ett själv."
            )
            self.joint_image.setVisible(False)

    def _fill_row(self, row: int, cut) -> None:
        index_item = QTableWidgetItem(str(cut.index))
        index_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        self.cut_table.setItem(row, COLUMN_INDEX, index_item)

        axis_combo = QComboBox()
        for axis, name in enumerate(AXIS_NAMES):
            axis_combo.addItem(name, axis)
        axis_combo.setCurrentIndex(cut.plane.axis)
        axis_combo.setToolTip("Vilket håll snittet går i")
        axis_combo.currentIndexChanged.connect(partial(self._on_axis_changed, row))
        self.cut_table.setCellWidget(row, COLUMN_AXIS, axis_combo)

        low, high = self._axis_range(cut.plane.axis)
        position = QDoubleSpinBox()
        position.setRange(low, high)
        position.setDecimals(1)
        position.setSingleStep(1.0)
        position.setSuffix(" mm")
        position.setValue(cut.plane.position)
        position.setToolTip(f"Var snittet ligger ({low:.0f} till {high:.0f} mm)")
        position.valueChanged.connect(partial(self._on_position_moved, row))
        position.editingFinished.connect(partial(self._on_position_settled, row))
        self.cut_table.setCellWidget(row, COLUMN_POSITION, position)

        joint_combo = QComboBox()
        for joint_type in JOINT_TYPES:
            joint_combo.addItem(JOINT_LABELS[joint_type], joint_type)
        current = cut.recommendation.joint_type if cut.recommendation else "none"
        joint_combo.setCurrentIndex(max(joint_combo.findData(current), 0))
        joint_combo.currentIndexChanged.connect(partial(self._on_joint_changed, row))
        self.cut_table.setCellWidget(row, COLUMN_JOINT, joint_combo)

        motivation = cut.recommendation.motivation if cut.recommendation else ""
        item = QTableWidgetItem(motivation)
        item.setToolTip(motivation)
        self.cut_table.setItem(row, COLUMN_MOTIVATION, item)

    # -- manuell redigering ------------------------------------------------

    def _axis_range(self, axis: int) -> tuple[float, float]:
        """Var ett snitt får ligga längs en axel: innanför modellen."""
        if self.plan is None:
            return (-1000.0, 1000.0)
        bounds = self.plan.bounds
        return (float(bounds[0][axis]) + 0.1, float(bounds[1][axis]) - 0.1)

    def _ensure_plan(self) -> bool:
        """Skapa en tom manuell plan om användaren börjar med att lägga till snitt."""
        if self.plan is not None:
            return True
        if self.mesh_info is None:
            return False
        self.plan = plan_from_cuts(
            self.mesh_info.mesh,
            self.current_printer(),
            [],
            assembly_intent=self.current_intent(),
        )
        self.view.show_model(oriented_mesh(self.mesh_info.mesh, self.plan))
        return True

    def _rebuild_plan(self, cuts, select=None) -> None:
        """Bygg om planen av de aktuella snitten och rita om allt.

        Snitten sorteras efter läge, så raderna kan byta plats när ett snitt
        flyttas förbi ett annat. `select` är snittet markeringen ska följa, så
        att användaren inte tappar bort det hen just redigerade.
        """
        printer = self.current_printer()
        plan = self.plan
        self.plan = plan_from_cuts(
            self.mesh_info.mesh,
            printer,
            cuts,
            transform=plan.transform,
            orientation_name=plan.orientation_name,
            assembly_intent=self.current_intent(),
        )
        self.result = None
        self.cut_button.setEnabled(bool(self.plan.cuts))
        self.preview_button.setEnabled(bool(self.plan.cuts))
        self._fill_table(self.plan)
        self._refresh_planes()

        if select is not None:
            for row, cut in enumerate(self.plan.cuts):
                if cut is select:
                    self.cut_table.setCurrentCell(row, COLUMN_POSITION)
                    self._show_motivation(row)
                    break

    def _refresh_planes(self) -> None:
        if self.plan is None or self.mesh_info is None:
            return
        self.view.show_model(oriented_mesh(self.mesh_info.mesh, self.plan))
        self.view.show_planes(self.plan.planes, self.plan.bounds)
        self.on_bed_toggled()

    def _analysed_cut(self, axis: int, position: float, index: int, normal=None):
        return make_cut(
            oriented_mesh(self.mesh_info.mesh, self.plan),
            axis,
            position,
            index=index,
            printer=self.current_printer(),
            assembly_intent=self.current_intent(),
            bounds=self.plan.bounds,
            normal=normal,
        )

    def add_cut(self) -> None:
        """Lägg ett nytt snitt mitt på modellens längsta axel."""
        if not self._ensure_plan():
            return
        extents = self.plan.bounds[1] - self.plan.bounds[0]
        axis = int(np.argmax(extents))
        low, high = self._axis_range(axis)
        position = (low + high) / 2.0

        # Ligger redan ett snitt där, lägg det nya en bit vid sidan om.
        taken = [c.plane.position for c in self.plan.cuts if c.plane.axis == axis]
        while any(abs(position - other) < 5.0 for other in taken) and position < high - 5.0:
            position += 10.0

        cut = self._analysed_cut(axis, position, len(self.plan.cuts) + 1)
        self._rebuild_plan([*self.plan.cuts, cut], select=cut)
        self.status(
            f"Lade till ett snitt vid {AXIS_NAMES[axis]} = {position:.1f} mm. "
            "Flytta det i tabellen eller dra i värdet."
        )

    def remove_cut(self) -> None:
        row = self.cut_table.currentRow()
        if self.plan is None or not (0 <= row < len(self.plan.cuts)):
            self.status("Markera ett snitt i tabellen först.", error=True)
            return
        removed = self.plan.cuts[row]
        remaining = [c for i, c in enumerate(self.plan.cuts) if i != row]
        self._rebuild_plan(remaining)
        self.status(
            f"Tog bort snittet vid {AXIS_NAMES[removed.plane.axis]} = "
            f"{removed.plane.position:.1f} mm."
        )

    def _on_axis_changed(self, row: int, _index: int) -> None:
        if self._filling or self.plan is None or not (0 <= row < len(self.plan.cuts)):
            return
        axis = self.cut_table.cellWidget(row, COLUMN_AXIS).currentData()
        low, high = self._axis_range(axis)
        cuts = list(self.plan.cuts)
        cuts[row] = self._analysed_cut(axis, (low + high) / 2.0, cuts[row].index)
        self._rebuild_plan(cuts, select=cuts[row])

    def _on_position_moved(self, row: int, value: float) -> None:
        """Medan värdet ändras: flytta planet i vyn, men analysera inte om."""
        if self._filling or self.plan is None or not (0 <= row < len(self.plan.cuts)):
            return
        cut = self.plan.cuts[row]
        origin = list(cut.plane.origin)
        origin[cut.plane.axis] = float(value)
        cut.plane = type(cut.plane)(
            origin=tuple(origin), normal=cut.plane.normal, axis=cut.plane.axis
        )
        self.view.show_planes(self.plan.planes, self.plan.bounds)
        self._update_summary()

    def _on_position_settled(self, row: int) -> None:
        """När värdet är klart: analysera om snittet på sin nya plats."""
        if self._filling or self.plan is None or not (0 <= row < len(self.plan.cuts)):
            return
        widget = self.cut_table.cellWidget(row, COLUMN_POSITION)
        cut = self.plan.cuts[row]
        if abs(widget.value() - cut.plane.position) < 1e-9 and cut.analysis is not None:
            if abs(cut.analysis.position_mm - widget.value()) < 1e-6:
                return
        cuts = list(self.plan.cuts)
        cuts[row] = self._analysed_cut(cut.plane.axis, widget.value(), cut.index)
        self._rebuild_plan(cuts, select=cuts[row])

    # -- dra planet direkt i 3D-vyn ---------------------------------------

    def _on_plane_dragged(self, number: int, distance_mm: float) -> None:
        """Användaren drar i ett plan: flytta det längs sin egen normal."""
        if self.plan is None or not (0 <= number < len(self.plan.cuts)):
            return
        self._leave_preview()
        cut = self.plan.cuts[number]
        plane = cut.plane
        origin = np.asarray(plane.origin, dtype=float) + plane.unit_normal * distance_mm
        origin = np.clip(origin, self.plan.bounds[0] + 0.1, self.plan.bounds[1] - 0.1)
        cut.plane = type(plane)(
            origin=tuple(float(v) for v in origin), normal=plane.normal, axis=plane.axis
        )
        self._sync_row_position(number)
        self.view.show_planes(self.plan.planes, self.plan.bounds)
        self._update_summary()

    def _on_plane_tilted(self, number: int, dx: float, dy: float) -> None:
        """Shift+dra: vinkla planet kring vyns egna axlar."""
        if self.plan is None or not (0 <= number < len(self.plan.cuts)):
            return
        self._leave_preview()
        cut = self.plan.cuts[number]
        plane = cut.plane

        view = self.view.viewMatrix()
        matrix = np.array(view.data(), dtype=float).reshape(4, 4).T
        right, up = matrix[0, :3], matrix[1, :3]

        normal = plane.unit_normal
        for axis, degrees in ((up, -dx * TILT_DEGREES_PER_PIXEL), (right, -dy * TILT_DEGREES_PER_PIXEL)):
            if abs(degrees) < 1e-9:
                continue
            rotation = trimesh.transformations.rotation_matrix(np.radians(degrees), axis)
            normal = rotation[:3, :3] @ normal

        length = float(np.linalg.norm(normal))
        if length < 1e-9:
            return
        normal = normal / length
        cut.plane = type(plane)(
            origin=plane.origin,
            normal=tuple(float(v) for v in normal),
            axis=int(dominant_axis(normal)),
        )
        self._sync_row_position(number)
        self.view.show_planes(self.plan.planes, self.plan.bounds)
        self._update_summary()

    def _on_plane_released(self, number: int) -> None:
        """Dragningen är klar - analysera om snittet på sin nya plats."""
        if self.plan is None or not (0 <= number < len(self.plan.cuts)):
            return
        cut = self.plan.cuts[number]
        cuts = list(self.plan.cuts)
        cuts[number] = self._analysed_cut(
            cut.plane.axis, cut.plane.position, cut.index, normal=cut.plane.normal
        )
        cuts[number].plane = cut.plane
        self._rebuild_plan(cuts, select=cuts[number])

    def _leave_preview(self) -> None:
        """Rör man ett plan gäller inte förhandsgranskningen längre."""
        if not self.view.showing_parts:
            return
        self.result = None
        self.view.clear_parts()
        if self.mesh_info is not None and self.plan is not None:
            self.view.show_model(oriented_mesh(self.mesh_info.mesh, self.plan))
            self.view.show_planes(self.plan.planes, self.plan.bounds)
            self.on_bed_toggled()
        self._update_summary()

    def _sync_row_position(self, number: int) -> None:
        """Håll tabellen i takt med planet medan det dras."""
        if not (0 <= number < self.cut_table.rowCount()):
            return
        cut = self.plan.cuts[number]
        self._filling = True
        try:
            widget = self.cut_table.cellWidget(number, COLUMN_POSITION)
            if widget is not None:
                widget.setValue(cut.plane.position)
            axis_widget = self.cut_table.cellWidget(number, COLUMN_AXIS)
            if axis_widget is not None:
                axis_widget.setCurrentIndex(cut.plane.axis)
        finally:
            self._filling = False

    def straighten_cut(self) -> None:
        """Ta bort lutningen på det markerade snittet."""
        row = self.cut_table.currentRow()
        if self.plan is None or not (0 <= row < len(self.plan.cuts)):
            self.status("Markera ett snitt i tabellen först.", error=True)
            return
        cut = self.plan.cuts[row]
        if cut.plane.is_axis_aligned:
            self.status("Snittet är redan rakt.")
            return
        cuts = list(self.plan.cuts)
        cuts[row] = self._analysed_cut(cut.plane.axis, cut.plane.position, cut.index)
        self._rebuild_plan(cuts, select=cuts[row])
        self.status(f"Rätade upp snitt {cut.index}.")

    def _update_summary(self) -> None:
        """Visa hur många delar planen ger och om de får plats."""
        if self.plan is None:
            self.plan_summary.setText("")
            return
        printer = self.current_printer()
        boxes = self.plan.part_boxes
        if not boxes:
            self.plan_summary.setText("")
            return
        biggest = max(boxes, key=lambda b: max(b.size_mm))
        x, y, z = biggest.size_mm
        too_big = [b.index for b in boxes if not printer.fits(sorted(b.size_mm))]
        text = (
            f"{self.plan.part_count} delar, störst {x:.0f} × {y:.0f} × {z:.0f} mm "
            f"(byggvolym {printer.usable[0]:.0f} × {printer.usable[1]:.0f} × "
            f"{printer.usable[2]:.0f} mm)"
        )
        if too_big:
            text += f" — <b>delarna {too_big} får inte plats</b>"
            self.plan_summary.setStyleSheet("color: #a33;")
        else:
            self.plan_summary.setStyleSheet("color: #363;")
        self.plan_summary.setText(text)

    def _fit_table_height(self) -> None:
        """Låt tabellen ta precis den plats den behöver - resten hör till knapparna."""
        rows = self.cut_table.rowCount()
        header = self.cut_table.horizontalHeader().height()
        row_height = self.cut_table.rowHeight(0) if rows else 24
        wanted = header + rows * row_height + 6
        self.cut_table.setMinimumHeight(min(max(wanted, 70), 220))
        self.cut_table.setMaximumHeight(min(max(wanted, 70), 220))

    def _on_row_selected(self, row: int, *_args) -> None:
        self._show_motivation(row)

    def _show_motivation(self, row: int) -> None:
        """Visa hela motiveringen för ett snitt, inte den avhuggna kolumntexten."""
        if self.plan is None or not (0 <= row < len(self.plan.cuts)):
            return
        cut = self.plan.cuts[row]
        if cut.recommendation is None:
            self.motivation_label.setText("")
            self.joint_image.setVisible(False)
            return
        alternatives = ""
        if len(cut.alternatives) > 1:
            others = ", ".join(
                f"{JOINT_LABELS[a.joint_type]} ({a.confidence * 100:.0f} %)"
                for a in cut.alternatives[1:]
            )
            alternatives = f"<br><i>Andra möjligheter: {others}</i>"
        tilt = ""
        if not cut.plane.is_axis_aligned:
            tilt = (
                f" <i>Planet lutar {cut.plane.tilt_deg:.0f}° från "
                f"{AXIS_NAMES[cut.plane.axis]}-axeln.</i>"
            )
        self.motivation_label.setText(
            f"<b>Snitt {cut.index}:</b>{tilt} {cut.recommendation.motivation}{alternatives}"
        )
        self._show_joint_image(cut.recommendation.joint_type)
        self._update_cut_controls(cut)

    def _update_cut_controls(self, cut) -> None:
        """Fyll lutning och stoppkant med det markerade snittets värden."""
        self._filling = True
        try:
            self.tilt_spin.setEnabled(True)
            self.tilt_axis_combo.setEnabled(True)
            self.tilt_spin.setValue(cut.plane.tilt_deg)

            # Man lutar kring de två axlar som inte är snittets egen.
            self.tilt_axis_combo.clear()
            for axis in range(3):
                if axis != cut.plane.axis:
                    self.tilt_axis_combo.addItem(f"{AXIS_NAMES[axis]}-axeln", axis)

            joint_type = cut.recommendation.joint_type if cut.recommendation else "none"
            is_dovetail = joint_type == "dovetail"
            stop = float((cut.recommendation.params or {}).get("stop_mm", 0.0)) if cut.recommendation else 0.0
            self.stop_check.setEnabled(is_dovetail)
            self.stop_check.setChecked(is_dovetail and stop > 0)
            self.stop_spin.setEnabled(is_dovetail and stop > 0)
            if stop > 0:
                self.stop_spin.setValue(stop)
            self.stop_check.setToolTip(
                "Stäng botten på laxstjärtsspåret, så att delen glider in och tar "
                "emot mot material i stället för att bara hållas av friktion."
                if is_dovetail
                else "Gäller bara laxstjärt."
            )
        finally:
            self._filling = False

    def _on_tilt_changed(self, *_args) -> None:
        """Sätt lutningen exakt i grader."""
        row = self.cut_table.currentRow()
        if self._filling or self.plan is None or not (0 <= row < len(self.plan.cuts)):
            return
        cut = self.plan.cuts[row]
        around = self.tilt_axis_combo.currentData()
        if around is None:
            return

        base = np.zeros(3)
        base[cut.plane.axis] = 1.0
        direction = np.zeros(3)
        direction[int(around)] = 1.0
        rotation = trimesh.transformations.rotation_matrix(
            np.radians(self.tilt_spin.value()), direction
        )
        normal = rotation[:3, :3] @ base

        cuts = list(self.plan.cuts)
        cuts[row] = self._analysed_cut(
            cut.plane.axis, cut.plane.position, cut.index, normal=tuple(normal)
        )
        self._rebuild_plan(cuts, select=cuts[row])

    def _on_stop_changed(self, *_args) -> None:
        """Slå på eller av stoppkanten i laxstjärtens botten."""
        row = self.cut_table.currentRow()
        if self._filling or self.plan is None or not (0 <= row < len(self.plan.cuts)):
            return
        cut = self.plan.cuts[row]
        if cut.recommendation is None:
            return

        enabled = self.stop_check.isChecked()
        self.stop_spin.setEnabled(enabled)
        stop = self.stop_spin.value() if enabled else 0.0
        cut.recommendation.params = {**(cut.recommendation.params or {}), "stop_mm": stop}
        self.result = None
        self.status(
            f"Snitt {cut.index}: stoppkant {stop:.1f} mm i laxstjärtens botten."
            if enabled
            else f"Snitt {cut.index}: laxstjärtsspåret går igenom."
        )

    def _show_joint_image(self, joint_type: str) -> None:
        """Bild på den valda fogtypen, om den finns."""
        picture = joint_images.pixmap(joint_type, width=JOINT_THUMBNAIL_WIDTH)
        self.joint_image.setVisible(picture is not None)
        if picture is not None:
            self.joint_image.setPixmap(picture)
            self.joint_image.setToolTip(joint_images.DESCRIPTIONS.get(joint_type, ""))

    def show_joint_help(self) -> None:
        """Öppna fönstret som visar alla fogtyper med bild."""
        dialog = JointHelpDialog(JOINT_LABELS, self)
        dialog.exec()

    def _on_joint_changed(self, row: int, _index: int) -> None:
        """Användaren valde en annan fogtyp för ett snitt."""
        if self._filling or self.plan is None or row >= len(self.plan.cuts):
            return
        cut = self.plan.cuts[row]
        combo = self.cut_table.cellWidget(row, COLUMN_JOINT)
        joint_type = combo.currentData()
        if cut.analysis is None:
            return

        recommendation = build_recommendation(
            joint_type,
            cut.analysis,
            intent=self.current_intent(),
            clearance_mm=self.clearance.value(),
        )
        cut.recommendation = recommendation
        self._update_cut_controls(cut)
        item = QTableWidgetItem(recommendation.motivation)
        item.setToolTip(recommendation.motivation)
        self.cut_table.setItem(row, COLUMN_MOTIVATION, item)
        self._show_motivation(row)
        self.status(
            f"Snitt {cut.index}: fogtyp ändrad till {JOINT_LABELS[joint_type]}."
        )

    # ------------------------------------------------------------------
    # 5. Kapa och exportera
    # ------------------------------------------------------------------

    def choose_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Välj målmapp", self.settings.last_output_dir or str(Path.home())
        )
        if path:
            self.settings.last_output_dir = path
            self.output_label.setText(path)

    def start_preview(self) -> None:
        """Kapa i minnet och visa resultatet, utan att skriva några filer."""
        if self.plan is None or self.mesh_info is None:
            return
        printer = self.current_printer()
        plan = self.plan
        mesh = self.mesh_info.mesh

        def work(progress=None):
            return cut_mesh(mesh, plan, joints=True, printer=printer, progress=progress)

        self._start(work, self._on_preview_done, "Förhandsgranskar…")

    def _on_preview_done(self, result) -> None:
        self.result = result
        self._report_result(result)
        self.status("Förhandsgranskning - inga filer har skrivits.")
        self.view.show_parts(result.parts)
        # Planen ligger kvar ovanpå delarna, så man kan justera och titta igen.
        self.view.show_planes(self.plan.planes, self.plan.bounds)
        if self.explode_slider.value() == 0:
            self.explode_slider.setValue(DEFAULT_PREVIEW_EXPLODE_MM)
        self.on_bed_toggled()
        self._summarise_result(result)

    def _report_result(self, result) -> None:
        """Gemensam rapportering för förhandsgranskning och kapning."""
        self.status(f"Kapade i {len(result.parts)} delar.")
        built = [j for j in result.joints if j.applied]
        if result.joints:
            self.status(f"Byggde {len(built)} av {len(result.joints)} fogar.")
        for joint in result.joints:
            if joint.fell_back:
                self.status(
                    f"Snitt {joint.cut_index}: fogen {joint.requested_type} fick inte plats, "
                    f"använde {joint.joint_type} i stället."
                )
        for warning in result.warnings:
            self.status(warning, error=not result.inherited_damage)
        if result.inherited_damage and not result.all_watertight:
            self.status(
                "Delarna går oftast att skriva ut ändå - testa dem i din slicer. "
                "Klagar den, laga originalmodellen och kapa om."
            )

    def _summarise_result(self, result) -> None:
        """Visa de verkliga delarnas mått efter en kapning."""
        printer = self.current_printer()
        too_big = parts_fit(result, printer)
        biggest = max(result.parts, key=lambda p: max(p.extents_mm))
        x, y, z = biggest.extents_mm
        text = (
            f"{len(result.parts)} delar, störst {x:.0f} × {y:.0f} × {z:.0f} mm "
            f"(byggvolym {printer.usable[0]:.0f} × {printer.usable[1]:.0f} × "
            f"{printer.usable[2]:.0f} mm)"
        )
        if too_big:
            text += f" — <b>delarna {too_big} får inte plats</b>"
            self.plan_summary.setStyleSheet("color: #a33;")
            self.status(
                f"Delarna {too_big} får inte plats i byggvolymen.", error=True
            )
        else:
            self.plan_summary.setStyleSheet("color: #363;")
        self.plan_summary.setText(text)

    def start_cut(self) -> None:
        if self.plan is None or self.mesh_info is None:
            return
        if not self.settings.last_output_dir:
            self.choose_output_dir()
            if not self.settings.last_output_dir:
                self.status("Välj en målmapp först.", error=True)
                return

        printer = self.current_printer()
        plan = self.plan
        mesh = self.mesh_info.mesh
        source = self.mesh_info.path
        out_dir = Path(self.settings.last_output_dir)

        ready = self.result

        def work(progress=None):
            # Har vi redan förhandsgranskat samma plan behöver vi inte kapa igen.
            result = ready
            if result is None:
                result = cut_mesh(
                    mesh, plan, joints=True, printer=printer, progress=progress
                )
            progress(0.97, "Skriver filer")
            export = exporter.export_parts(result, out_dir, printer, source=source)
            return result, export

        self._start(work, self._on_cut_done, "Kapar modellen…")

    def _on_cut_done(self, payload) -> None:
        result, export = payload
        self.result = result

        self._report_result(result)
        self._summarise_result(result)
        self.status(f"Skrev {len(export.part_files)} filer till {export.directory}")
        self.status(f"Rapport: {export.report_file.name}")
        self.view.show_parts(result.parts)
        self.on_bed_toggled()

    # ------------------------------------------------------------------
    # 3D-vyn
    # ------------------------------------------------------------------

    def on_explode_changed(self, value: int) -> None:
        self.explode_label.setText(f"{value} mm")
        self.settings.explode_mm = float(value)
        self.view.set_explode(float(value))

    def on_background_toggled(self, *_args) -> None:
        light = self.light_checkbox.isChecked()
        self.settings.light_background = light
        self.view.set_light_background(light)

    def on_bed_toggled(self, *_args) -> None:
        visible = self.bed_checkbox.isChecked()
        self.settings.show_bed = visible
        self.view.set_bed(self.current_printer(), visible)

    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt-namn
        if self.worker is not None and self.worker.isRunning():
            answer = QMessageBox.question(
                self,
                "Avbryta?",
                "Ett arbete pågår. Vill du avbryta och avsluta?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.worker.cancel()
            self.worker.wait(3000)

        self.settings.printer = self.printer_combo.currentText()
        self.settings.assembly_intent = self.current_intent()
        self.settings.clearance_mm = self.clearance.value()
        self.settings.margin_mm = self.margin.value()
        self.settings.light_background = self.light_checkbox.isChecked()
        self.settings.save()
        event.accept()
