"""Huvudfönstret.

Vänsterpanelen leder användaren uppifrån och ner: modell, skrivare, montering,
förslag, kapa. Högerpanelen visar modellen i 3D. Allt tungt arbete körs i en
bakgrundstråd så att fönstret aldrig fryser.
"""

from __future__ import annotations

import logging
from functools import partial
from pathlib import Path

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
from ..core.planner import plan_splits
from ..core.printers import PrinterProfile, get_printer, load_printers, save_profile
from ..core.recommender import JOINT_TYPES, build_recommendation
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


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings | None = None):
        super().__init__()
        self.settings = settings or Settings.load()
        self.mesh_info = None
        self.plan = None
        self.result = None
        self.worker: Worker | None = None

        self.setWindowTitle(WINDOW_TITLE)
        self.setAcceptDrops(True)
        self.resize(1280, 820)
        self._build_ui()
        self._load_printers()
        self._apply_settings()
        self.status("Öppna en STL- eller 3MF-fil för att börja.")

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

        self.cut_table = QTableWidget(0, 4)
        self.cut_table.setHorizontalHeaderLabels(["Snitt", "Position", "Fogtyp", "Motivering"])
        self.cut_table.verticalHeader().setVisible(False)
        self.cut_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.cut_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.cut_table.setMinimumHeight(70)
        self.cut_table.setMaximumHeight(90)
        self.cut_table.currentCellChanged.connect(self._on_row_selected)
        layout.addWidget(self.cut_table)

        # Motiveringen får inte plats i kolumnen - visa hela för markerad rad.
        self.motivation_label = QLabel("Markera ett snitt för att läsa hela motiveringen.")
        self.motivation_label.setWordWrap(True)
        self.motivation_label.setMinimumHeight(48)
        self.motivation_label.setAlignment(Qt.AlignTop)
        self.motivation_label.setStyleSheet("color: #444; padding: 4px;")
        layout.addWidget(self.motivation_label)
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

        self.cut_button = QPushButton("Kapa modellen")
        self.cut_button.setEnabled(False)
        self.cut_button.clicked.connect(self.start_cut)
        layout.addWidget(self.cut_button)
        return box

    def _right_side(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)

        self.view = ModelView()
        layout.addWidget(self.view, 1)

        controls = QHBoxLayout()
        self.bed_checkbox = QCheckBox("Visa byggplatta")
        self.bed_checkbox.stateChanged.connect(self.on_bed_toggled)
        controls.addWidget(self.bed_checkbox)

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
        self.explode_slider.setValue(int(self.settings.explode_mm))
        if self.settings.last_output_dir:
            self.output_label.setText(self.settings.last_output_dir)

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
        self.analyse_button.setEnabled(True)

        x, y, z = info.extents_mm
        state = (
            "Meshen är hel."
            if info.watertight
            else f"Varning: {info.open_edges} öppna kanter — se meddelandet nedan."
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
                f"Modellen har {info.open_edges} öppna kanter som inte gick att laga. "
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
        self._fill_table(plan)
        self.view.show_model(self.mesh_info.mesh)
        self.view.show_planes(plan.planes, plan.bounds)
        self.on_bed_toggled()

    def _fill_table(self, plan) -> None:
        self.cut_table.setRowCount(len(plan.cuts))
        for row, cut in enumerate(plan.cuts):
            axis = "XYZ"[cut.plane.axis]
            self.cut_table.setItem(row, 0, QTableWidgetItem(str(cut.index)))
            self.cut_table.setItem(
                row, 1, QTableWidgetItem(f"{axis} = {cut.plane.position:.1f} mm")
            )

            combo = QComboBox()
            for joint_type in JOINT_TYPES:
                combo.addItem(JOINT_LABELS[joint_type], joint_type)
            current = cut.recommendation.joint_type if cut.recommendation else "none"
            combo.setCurrentIndex(max(combo.findData(current), 0))
            combo.currentIndexChanged.connect(partial(self._on_joint_changed, row))
            self.cut_table.setCellWidget(row, 2, combo)

            motivation = cut.recommendation.motivation if cut.recommendation else ""
            item = QTableWidgetItem(motivation)
            item.setToolTip(motivation)
            self.cut_table.setItem(row, 3, item)
        self.cut_table.resizeColumnsToContents()
        self.cut_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self._fit_table_height()
        if plan.cuts:
            self.cut_table.setCurrentCell(0, 0)
            self._show_motivation(0)

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
            return
        alternatives = ""
        if len(cut.alternatives) > 1:
            others = ", ".join(
                f"{JOINT_LABELS[a.joint_type]} ({a.confidence * 100:.0f} %)"
                for a in cut.alternatives[1:]
            )
            alternatives = f"<br><i>Andra möjligheter: {others}</i>"
        self.motivation_label.setText(
            f"<b>Snitt {cut.index}:</b> {cut.recommendation.motivation}{alternatives}"
        )

    def _on_joint_changed(self, row: int, _index: int) -> None:
        """Användaren valde en annan fogtyp för ett snitt."""
        if self.plan is None or row >= len(self.plan.cuts):
            return
        cut = self.plan.cuts[row]
        combo = self.cut_table.cellWidget(row, 2)
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
        item = QTableWidgetItem(recommendation.motivation)
        item.setToolTip(recommendation.motivation)
        self.cut_table.setItem(row, 3, item)
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

        def work(progress=None):
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
        # Ärvda hål från en trasig originalmodell är inte ett fel i kapningen.
        for warning in result.warnings:
            self.status(warning, error=not result.inherited_damage)
        if result.inherited_damage and not result.all_watertight:
            self.status(
                "Delarna går oftast att skriva ut ändå - testa dem i din slicer. "
                "Klagar den, laga originalmodellen och kapa om."
            )

        too_big = parts_fit(result, self.current_printer())
        if too_big:
            self.status(
                f"Delarna {too_big} får fortfarande inte plats i byggvolymen.", error=True
            )

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
        self.settings.save()
        event.accept()
