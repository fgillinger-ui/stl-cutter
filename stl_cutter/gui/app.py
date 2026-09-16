"""Huvudfönstret.

Vänsterpanelen leder användaren uppifrån och ner: modell, skrivare, montering,
förslag, kapa. Högerpanelen visar modellen i 3D. Allt tungt arbete körs i en
bakgrundstråd så att fönstret aldrig fryser.
"""

from __future__ import annotations

import logging
from dataclasses import replace
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

from ..core import assembly as assembly_core
from ..core import exporter, load as load_core, mesh_io, resize as resize_core
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
from ..core.resize import ResizeError
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

#: Val i rullgardinen för hur måttändringen ska fördelas.
SPAN_SELECTIONS = (
    ("auto", "Automatiskt (håll modellen symmetrisk)"),
    ("distribute", "Fördela jämnt över alla partier"),
    ("longest", "Lägg till i längsta partiet"),
)

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
        #: Modellen som den såg ut innan senaste måttändringen, för Ångra.
        self.mesh_before_resize = None
        #: Filens objekt var för sig. Ett enda objekt är det vanliga; flera
        #: förekommer när en CAD-fil innehåller delar som hör ihop.
        self.parts: list = []
        self.spans = None
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
        layout.addWidget(self._section_resize())
        layout.addWidget(self._section_printer())
        layout.addWidget(self._section_assembly())
        layout.addWidget(self._section_load())
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

    def _section_resize(self) -> QGroupBox:
        """1b. Ändra mått - sker alltid före snittplaneringen."""
        box = QGroupBox("1b. Ändra mått")
        layout = QVBoxLayout(box)

        self.current_size_label = QLabel("Nuvarande mått: –")
        layout.addWidget(self.current_size_label)

        # Raden syns bara när filen innehåller flera objekt. Med ett enda
        # objekt vore den bara i vägen.
        self.parts_row = QWidget()
        parts_layout = QHBoxLayout(self.parts_row)
        parts_layout.setContentsMargins(0, 0, 0, 0)
        parts_layout.addWidget(QLabel("Objekt:"))
        self.part_combo = QComboBox()
        self.part_combo.setToolTip(
            "Måttet nedan gäller det här objektet. Övriga objekt följer med."
        )
        self.part_combo.currentIndexChanged.connect(self._on_part_changed)
        parts_layout.addWidget(self.part_combo, 1)
        layout.addWidget(self.parts_row)

        self.link_parts = QCheckBox("Låt övriga objekt följa med symmetriskt")
        self.link_parts.setChecked(True)
        self.link_parts.setToolTip(
            "Övriga objekt får samma tillskott i millimeter - inte samma mått.\n"
            "En hylla på 230 mm och en bakplatta på 250 mm som ska till 270 ger\n"
            "alltså plattan 290 mm, så att laxstjärtar och spår fortfarande\n"
            "sitter mitt för varandra."
        )
        layout.addWidget(self.link_parts)
        self.parts_row.setVisible(False)
        self.link_parts.setVisible(False)

        self.target_x = self._spin(1, 10000, "")
        self.target_y = self._spin(1, 10000, "")
        self.target_z = self._spin(1, 10000, "")
        self.target_spins = (self.target_x, self.target_y, self.target_z)
        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("Önskat (mm):"))
        for label, spin in zip(("X", "Y", "Z"), self.target_spins):
            spin.setToolTip({"X": "Bredd", "Y": "Djup", "Z": "Höjd"}[label])
            spin.valueChanged.connect(partial(self._on_target_changed, spin))
            target_row.addWidget(QLabel(label))
            target_row.addWidget(spin, 1)
        layout.addLayout(target_row)

        self.lock_ratio = QCheckBox("Lås proportioner")
        self.lock_ratio.setToolTip(
            "Ändrar alla tre måtten i samma förhållande. Av som standard - "
            "vanligen vill man ändra ett enda mått."
        )
        layout.addWidget(self.lock_ratio)

        self.show_spans_button = QPushButton("Visa var modellen kan sträckas")
        self.show_spans_button.clicked.connect(self.show_spans)
        self.show_spans_button.setEnabled(False)
        layout.addWidget(self.show_spans_button)

        self.span_selection = QComboBox()
        for value, label in SPAN_SELECTIONS:
            self.span_selection.addItem(label, value)
        self.span_selection.setToolTip(
            "Automatiskt är standard: är modellen spegelsymmetrisk längs axeln "
            "blir den det även efteråt, och tillskottet fördelas över de "
            "jämnstora partierna så att mellanrummen förblir lika stora.\n"
            "Längsta partiet lägger allt på ett ställe - ett medvetet val när "
            "modellen bara har en rak sträcka."
        )
        layout.addWidget(self.span_selection)

        self.scale_anyway = QCheckBox("Skala ändå (godstjocklek och hål förändras)")
        self.scale_anyway.setChecked(False)
        self.scale_anyway.setVisible(False)
        self.scale_anyway.toggled.connect(self._on_scale_anyway)
        layout.addWidget(self.scale_anyway)

        button_row = QHBoxLayout()
        self.resize_button = QPushButton("Ändra mått")
        self.resize_button.clicked.connect(self.start_resize)
        self.resize_button.setEnabled(False)
        button_row.addWidget(self.resize_button, 1)
        self.export_button = QPushButton("Exportera utan att dela")
        self.export_button.setToolTip(
            "Skriv modellen som den är just nu, utan att kapa den.\n"
            "Flera objekt i filen blir en fil var."
        )
        self.export_button.clicked.connect(self.export_model)
        self.export_button.setEnabled(False)
        button_row.addWidget(self.export_button)
        self.undo_resize_button = QPushButton("Ångra")
        self.undo_resize_button.setToolTip("Återställ modellen som den var.")
        self.undo_resize_button.clicked.connect(self.undo_resize)
        self.undo_resize_button.setEnabled(False)
        button_row.addWidget(self.undo_resize_button)
        layout.addLayout(button_row)
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

    def _section_load(self) -> QGroupBox:
        """Last att ta hänsyn till när snitten placeras.

        Rutan är avstängd som standard. Slås den på gissar programmet
        upphängningen ur formen och **visar gissningen med sitt skäl**, för
        fel upphängning vänder momentkurvan helt - det är inget som får
        avgöras i tysthet.
        """
        box = QGroupBox("3b. Belastning")
        layout = QVBoxLayout(box)

        self.load_check = QCheckBox("Delen ska bära last (hylla, konsol)")
        self.load_check.setToolTip(
            "Snitten läggs där böjmomentet är minst. Programmet räknar inte ut\n"
            "hur mycket delen bär - bara var den är som känsligast för en fog."
        )
        self.load_check.stateChanged.connect(self._on_load_changed)
        layout.addWidget(self.load_check)

        weight_row = QHBoxLayout()
        weight_row.addWidget(QLabel("Vikt att bära:"))
        self.load_weight = self._spin(0.0, 500.0, " kg", decimals=1, step=0.5)
        self.load_weight.setValue(5.0)
        self.load_weight.valueChanged.connect(self._on_load_changed)
        weight_row.addWidget(self.load_weight, 1)
        layout.addLayout(weight_row)

        support_row = QHBoxLayout()
        support_row.addWidget(QLabel("Upphängning:"))
        self.support_combo = QComboBox()
        self.support_combo.addItem("Gissa ur formen", "auto")
        for key in ("cantilever", "both_ends"):
            self.support_combo.addItem(load_core.SUPPORT_LABELS[key], key)
        self.support_combo.currentIndexChanged.connect(self._on_load_changed)
        support_row.addWidget(self.support_combo, 1)
        layout.addLayout(support_row)

        axis_row = QHBoxLayout()
        axis_row.addWidget(QLabel("Spännaxel:"))
        self.load_axis_combo = QComboBox()
        self.load_axis_combo.addItem("Gissa", -1)
        for index, name in enumerate(AXIS_NAMES):
            self.load_axis_combo.addItem(name, index)
        self.load_axis_combo.currentIndexChanged.connect(self._on_load_changed)
        axis_row.addWidget(self.load_axis_combo, 1)

        axis_row.addWidget(QLabel("Infästning:"))
        self.load_end_combo = QComboBox()
        self.load_end_combo.addItem("Gissa", None)
        self.load_end_combo.addItem("Vid axelns början", True)
        self.load_end_combo.addItem("Vid axelns slut", False)
        self.load_end_combo.currentIndexChanged.connect(self._on_load_changed)
        axis_row.addWidget(self.load_end_combo, 1)
        layout.addLayout(axis_row)

        self.load_guess_label = QLabel("")
        self.load_guess_label.setWordWrap(True)
        self.load_guess_label.setStyleSheet("color: #555;")
        layout.addWidget(self.load_guess_label)

        self.advice_button = QPushButton("Utskriftsinställningar för styrka…")
        self.advice_button.clicked.connect(self.show_print_advice)
        layout.addWidget(self.advice_button)

        self._on_load_changed()
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

        self.lay_flat_check = QCheckBox("Vänd delarna platt inför utskrift")
        self.lay_flat_check.setChecked(True)
        self.lay_flat_check.setToolTip(
            "Varje del vrids till sitt plattaste läge. Det tar bort stödbehovet\n"
            "och lägger utskriftens lager längs delen i stället för tvärs, vilket\n"
            "gör den flera gånger starkare i böjning.\n"
            "Gäller även Exportera utan att dela."
        )
        layout.addWidget(self.lay_flat_check)

        self.split_bodies_check = QCheckBox("Lösa kroppar som egna filer")
        self.split_bodies_check.setChecked(True)
        self.split_bodies_check.setToolTip(
            "Faller en del i flera lösa klumpar blir varje klump en egen fil\n"
            "(part_03a, part_03b …). I samma fil ser slicern dem som ett objekt\n"
            "och de går varken att vända eller placera var för sig."
        )
        layout.addWidget(self.split_bodies_check)

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

        self.export_action = QAction("&Exportera modellen (utan att dela)…", self)
        self.export_action.setToolTip(
            "Skriv modellen som den är just nu - efter en måttändring, men utan "
            "att kapa den."
        )
        self.export_action.triggered.connect(self.export_model)
        self.export_action.setEnabled(False)
        menu.addAction(self.export_action)

        menu.addSeparator()

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
            self.resize_button,
            self.show_spans_button,
            self.undo_resize_button,
            self.export_button,
            self.export_action,
        ):
            widget.setEnabled(not busy and self._enabled_when_idle(widget))

    def _enabled_when_idle(self, widget) -> bool:
        if widget is self.analyse_button:
            return self.mesh_info is not None
        if widget is self.cut_button:
            return self.plan is not None
        if widget in (self.resize_button, self.show_spans_button):
            return self.mesh_info is not None
        if widget in (self.export_button, self.export_action):
            return self.mesh_info is not None
        if widget is self.undo_resize_button:
            return self.mesh_before_resize is not None
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
        self._rebuild_parts()
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
        self.mesh_before_resize = None
        self.spans = None
        self.undo_resize_button.setEnabled(False)
        self.resize_button.setEnabled(True)
        self.show_spans_button.setEnabled(True)
        self.scale_anyway.setVisible(False)
        self.scale_anyway.setChecked(False)
        self._update_size_fields()
        self._on_load_changed()
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
    # 1b. Ändra mått
    # ------------------------------------------------------------------

    def _update_model_label(self) -> None:
        """Panelen med filnamn, mått och meshens tillstånd.

        Måtten läses ur `mesh_info` varje gång, aldrig ur ett sparat värde från
        inläsningen: panelen och måttsektionen ska aldrig kunna visa två olika
        mått för samma modell.
        """
        if self.mesh_info is None:
            self.model_label.setText("Ingen modell öppnad.")
            return
        info = self.mesh_info
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

    def _rebuild_parts(self) -> None:
        """Dela upp den inlästa modellen i objekt och fyll rullgardinen.

        Misslyckas uppdelningen är det inte värt att fälla hela inläsningen -
        då får filen räknas som ett enda objekt, precis som förut.
        """
        self.parts = []
        if self.mesh_info is not None:
            try:
                self.parts = assembly_core.split_parts(self.mesh_info.mesh)
            except Exception:  # pragma: no cover - försvar mot udda geometri
                log.exception("Kunde inte dela upp modellen i objekt")
                self.parts = []

        several = len(self.parts) > 1
        self._filling = True
        try:
            self.part_combo.clear()
            for part in self.parts:
                self.part_combo.addItem(part.summary())
        finally:
            self._filling = False
        self.parts_row.setVisible(several)
        self.link_parts.setVisible(several)
        if several:
            self.status(
                f"Filen innehåller {len(self.parts)} separata objekt. "
                "Måttet gäller det valda; övriga följer med."
            )

    def _selected_part(self) -> int:
        index = self.part_combo.currentIndex()
        return index if 0 <= index < len(self.parts) else 0

    def _on_part_changed(self, _index: int) -> None:
        """Byter man objekt ska måttfälten visa det objektets mått."""
        if self._filling:
            return
        self._update_size_fields()

    def _update_size_fields(self) -> None:
        """Skriv modellens mått i etiketten och i inmatningsfälten.

        Panelen uppdateras i samma andetag. Att låta den ligga kvar med måtten
        från inläsningen var orsaken till att den kunde visa 230 × 240 × 182 mm
        samtidigt som måttsektionen visade något annat.
        """
        self._update_model_label()
        if self.mesh_info is None:
            self.current_size_label.setText("Nuvarande mått: –")
            return
        x, y, z = self._current_extents()
        if len(self.parts) > 1:
            name = self.parts[self._selected_part()].name
            self.current_size_label.setText(
                f"Nuvarande mått ({name}): {x:.1f} × {y:.1f} × {z:.1f} mm"
            )
        else:
            self.current_size_label.setText(
                f"Nuvarande mått: {x:.1f} × {y:.1f} × {z:.1f} mm"
            )
        self._filling = True
        try:
            for spin, value in zip(self.target_spins, (x, y, z)):
                spin.setValue(float(value))
        finally:
            self._filling = False

    def _current_extents(self) -> tuple[float, float, float]:
        """Måtten som fälten gäller: det valda objektets, inte hela filens."""
        if len(self.parts) > 1:
            return tuple(float(v) for v in self.parts[self._selected_part()].extents_mm)
        return tuple(float(v) for v in self.mesh_info.extents_mm)

    def _on_target_changed(self, spin, value: float) -> None:
        """Håll proportionerna om kryssrutan är i, annars gör ingenting."""
        if self._filling or self.mesh_info is None or not self.lock_ratio.isChecked():
            return
        index = self.target_spins.index(spin)
        current = self._current_extents()
        if current[index] <= 0:
            return
        factor = float(value) / current[index]
        self._filling = True
        try:
            for other_index, other in enumerate(self.target_spins):
                if other_index != index:
                    other.setValue(current[other_index] * factor)
        finally:
            self._filling = False

    def _requested_targets(self) -> list[float | None]:
        """Måtten som faktiskt har ändrats; oförändrade blir None."""
        current = self._current_extents()
        targets: list[float | None] = []
        for index, spin in enumerate(self.target_spins):
            wanted = float(spin.value())
            targets.append(None if abs(wanted - current[index]) < 0.05 else wanted)
        return targets

    def _axes_of_interest(self) -> list[int]:
        """Axlarna användaren vill ändra - eller alla tre om inget är ifyllt."""
        targets = self._requested_targets()
        axes = [index for index, target in enumerate(targets) if target is not None]
        return axes or [0, 1, 2]

    def show_spans(self) -> None:
        """Visa de prismatiska partierna i grönt och insättningspunkterna i gult.

        Med ett mått ifyllt planeras hela måttändringen utan att köras, så att
        man ser exakt var materialet kommer att hamna innan man trycker på
        "Ändra mått".
        """
        if self.mesh_info is None:
            return
        mesh = self.mesh_info.mesh
        axes = self._axes_of_interest()
        targets = self._requested_targets()
        selection = self.span_selection.currentData() or "auto"

        def work(progress=None):
            found = []
            planned = []
            for position, axis in enumerate(axes):
                progress(position / len(axes), f"Söker partier längs {AXIS_NAMES[axis]}")
                spans = resize_core.find_prismatic_spans(mesh, axis, progress=None)
                found.extend(spans)
                if targets[axis] is None or not spans:
                    continue
                try:
                    insertions, _, _ = resize_core.plan_insertions(
                        mesh,
                        axis,
                        float(targets[axis]),
                        span_selection=selection,
                        spans=spans,
                    )
                except ResizeError as error:
                    # Planeringen misslyckades: partierna är fortfarande värda
                    # att visa, och felet berättar varför inget gult syns.
                    log.debug("Kunde inte planera insättningar: %s", error)
                    continue
                planned.extend(insertions)
            progress(1.0, "Klart")
            return found, planned

        self._start(work, self._on_spans_found, "Söker partier med konstant tvärsnitt…")

    def _on_spans_found(self, outcome) -> None:
        spans, insertions = outcome
        self.spans = spans
        self.view.show_model(self.mesh_info.mesh)
        if not spans:
            self._no_span_warning()
            return
        self.view.show_spans(spans, self.mesh_info.mesh.bounds)
        self.view.show_insertions(insertions, self.mesh_info.mesh.bounds)
        self.scale_anyway.setVisible(False)
        self.status(f"{len(spans)} parti(er) med konstant tvärsnitt (grönt):")
        for index, span in enumerate(spans, start=1):
            self.status(f"  {index}. {span.describe()}")
        if insertions:
            self.status(f"{len(insertions)} planerad(e) insättningspunkt(er) (gult):")
            for item in insertions:
                name = AXIS_NAMES[item.axis]
                self.status(
                    f"  {item.delta:+.1f} mm vid {name.lower()}={item.cut_at:.1f} mm"
                )
        else:
            self.status(
                "Fyll i ett önskat mått och tryck igen för att se var materialet "
                "kommer att läggas."
            )

    def _no_span_warning(self, message: str | None = None) -> None:
        """Ingen zon hittad: visa varningen och kräv ett aktivt val."""
        self.status(
            message
            or "Modellen har inget parti med konstant tvärsnitt längs den axeln — "
            "måttet kan bara ändras genom skalning, vilket förändrar godstjocklek "
            "och hål.",
            error=True,
        )
        self.scale_anyway.setVisible(True)
        self.scale_anyway.setChecked(False)

    def _on_scale_anyway(self, checked: bool) -> None:
        if checked:
            self.status(
                "Skalning vald: godstjocklek, hörnradier och hål ändras i samma "
                "förhållande. Runda hål blir ovala."
            )

    def start_resize(self) -> None:
        if self.mesh_info is None:
            return
        targets = self._requested_targets()
        if all(target is None for target in targets):
            self.status("Ändra minst ett av måtten först.", error=True)
            return

        mesh = self.mesh_info.mesh
        mode = "scale" if self.scale_anyway.isChecked() else "preserve"
        selection = self.span_selection.currentData() or "auto"

        if len(self.parts) > 1:
            # Måtten i fälten gäller det VALDA objektet, inte hela filens låda.
            # Därför måste vägen gå via assembly även när kryssrutan är ur -
            # då ändras bara det valda objektet. Skickades måttet i stället till
            # en vanlig måttändring av hela meshen bad man om något helt annat:
            # "gör filens djup 280" i stället för "gör hyllans djup 280".
            self._start_linked_resize(targets, mode, selection)
            return

        def work(progress=None):
            try:
                return resize_core.resize(
                    mesh,
                    targets,
                    mode=mode,
                    span_selection=selection,
                    progress=progress,
                )
            except ResizeError as error:
                # Felet bärs tillbaka som ett resultat i stället för att kastas,
                # så att gränssnittet kan visa förklaringen och kryssrutan
                # "Skala ändå" i stället för ett anonymt felmeddelande.
                return error

        self._start(work, self._on_resized, "Ändrar mått…")

    def _start_linked_resize(self, targets, mode: str, selection: str) -> None:
        """Måttändring där filens övriga objekt följer med.

        Axlarna körs en i taget. Varje varv bär ledarens önskade mått, och
        ``resize_together`` räknar om det till ett tillskott som de övriga
        objekten får dela.
        """
        parts = list(self.parts)
        leader = self._selected_part()
        follow = [self.link_parts.isChecked()] * len(parts)
        follow[leader] = True

        def work(progress=None):
            current = parts
            reports = []
            axes = [i for i, target in enumerate(targets) if target is not None]
            for step, axis in enumerate(axes):
                if progress is not None:
                    progress(step / max(len(axes), 1), f"Ändrar {AXIS_NAMES[axis]}…")
                try:
                    report = assembly_core.resize_together(
                        current,
                        axis=axis,
                        target_mm=float(targets[axis]),
                        leader=leader,
                        follow=follow,
                        mode=mode,
                        span_selection=selection,
                    )
                except ResizeError as error:
                    return error
                current = report.parts
                reports.append(report)
            return reports

        self._start(work, self._on_linked_resized, "Ändrar mått på alla objekt…")

    def _on_linked_resized(self, outcome) -> None:
        if isinstance(outcome, ResizeError):
            self._no_span_warning(f"{outcome.message} {outcome.suggestion}".strip())
            return
        if not outcome:
            return

        parts = outcome[-1].parts
        merged = trimesh.util.concatenate([part.mesh for part in parts])
        self.mesh_before_resize = self.mesh_info.mesh
        self.mesh_info = self._reload_info(merged)
        self.undo_resize_button.setEnabled(True)

        for report in outcome:
            for line in assembly_core.describe_assembly(report).splitlines():
                self.status(line)
            for note in report.notes:
                self.status(f"  {note}")
            for warning in report.warnings:
                self.status(f"Varning: {warning}", error=True)

        self._rebuild_parts()
        self._after_model_changed()
        self.status("Måtten är ändrade. Kör Analysera igen för att planera snitten.")

    def _on_resized(self, outcome) -> None:
        if isinstance(outcome, ResizeError):
            self._no_span_warning(f"{outcome.message} {outcome.suggestion}".strip())
            return

        self.mesh_before_resize = self.mesh_info.mesh
        self.mesh_info = self._reload_info(outcome.mesh)
        self.undo_resize_button.setEnabled(True)
        for entry in outcome.axes:
            self.status(entry.placement)
            if entry.symmetric_before:
                self.status(
                    "  Modellen var spegelsymmetrisk längs "
                    f"{AXIS_NAMES[entry.axis]} och är det fortfarande."
                )
        for warning in outcome.warnings:
            self.status(f"Varning: {warning}", error=True)
        self._after_model_changed()
        self.status("Måtten är ändrade. Kör Analysera igen för att planera snitten.")

    def undo_resize(self) -> None:
        if self.mesh_before_resize is None:
            return
        self.mesh_info = self._reload_info(self.mesh_before_resize)
        self.mesh_before_resize = None
        self.undo_resize_button.setEnabled(False)
        # Objektlistan bär måtten och måste tillbaka den också, annars står
        # den kvar och visar de ändrade objekten.
        self._rebuild_parts()
        self._after_model_changed()
        self.status("Måttändringen är ångrad - modellen är tillbaka som den var.")

    def _reload_info(self, mesh):
        """Bygg en ny MeshInfo för en ändrad mesh, med samma sökväg som förut."""
        info = self.mesh_info
        return mesh_io.MeshInfo(
            path=info.path,
            mesh=mesh,
            watertight=bool(mesh.is_watertight),
            winding_consistent=bool(mesh.is_winding_consistent),
            volume_mm3=float(abs(mesh.volume)),
            extents_mm=tuple(float(v) for v in mesh.extents),
            repairs=list(info.repairs),
            open_edges=mesh_io.open_edge_count(mesh),
        )

    def _after_model_changed(self) -> None:
        """Modellen har bytts ut: allt som beror på den gamla nollställs.

        Måttändringen sker före snittplaneringen. En plan som lades för de
        gamla måtten hör inte ihop med den nya modellen, och att låta den ligga
        kvar hade varit värre än att be användaren analysera om.
        """
        self.plan = None
        self.result = None
        self.spans = None
        self.cut_table.setRowCount(0)
        self.plan_summary.setText("")
        self.cut_button.setEnabled(False)
        self.preview_button.setEnabled(False)
        # show_model ramar in den nya bounding boxen, så modellen står kvar
        # mitt i vyn. En måttändring flyttar modellens mitt, och utan
        # omcentrering ser förskjutningen ut som en asymmetri den inte är.
        self.view.show_model(self.mesh_info.mesh)
        self.on_bed_toggled()
        self._update_size_fields()
        # Gissningen om upphängning läses ur modellens form och gäller bara
        # den modell den gjordes för.
        self._on_load_changed()

    # ------------------------------------------------------------------
    # 4. Analys och förslag
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Belastning
    # ------------------------------------------------------------------

    def current_load(self):
        """Lastfallet ur rutan 3b, eller None när ingen last angetts.

        Det som användaren själv valt vinner alltid över gissningen, och det
        som står kvar på "Gissa" hämtas ur modellens form.
        """
        if self.mesh_info is None or not self.load_check.isChecked():
            return None
        weight = float(self.load_weight.value())
        if weight <= 0:
            return None

        case = load_core.guess_load_case(self.mesh_info.mesh, weight)
        support = self.support_combo.currentData()
        if support != "auto":
            case = replace(case, support=support, guessed_from="")
        axis = self.load_axis_combo.currentData()
        if axis is not None and axis >= 0:
            case = replace(case, axis=int(axis), guessed_from="")
        end = self.load_end_combo.currentData()
        if end is not None:
            case = replace(case, fixed_at_low=bool(end), guessed_from="")
        return case

    def _on_load_changed(self, *_args) -> None:
        """Slå av och på rutan, och visa gissningen så fort den går att göra."""
        active = self.load_check.isChecked()
        for widget in (
            self.load_weight,
            self.support_combo,
            self.load_axis_combo,
            self.load_end_combo,
            self.advice_button,
        ):
            widget.setEnabled(active)

        if not active:
            self.load_guess_label.setText("")
            return
        if self.mesh_info is None:
            self.load_guess_label.setText("Läs in en modell för att se gissningen.")
            return

        case = self.current_load()
        if case is None:
            self.load_guess_label.setText("")
            return
        text = load_core.describe_load_case(case)
        if case.guessed_from:
            text += f"<br><i>Gissat: {case.guessed_from} Rätta det här ovanför om det är fel.</i>"
        self.load_guess_label.setText(text)

    def show_print_advice(self) -> None:
        case = self.current_load()
        if case is None or not case.active:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Utskriftsinställningar för styrka")
        box.setTextFormat(Qt.PlainText)
        box.setText(load_core.describe_advice(case))
        box.exec()

    def start_analysis(self) -> None:
        if self.mesh_info is None:
            return
        printer = self.current_printer()
        intent = self.current_intent()
        mesh = self.mesh_info.mesh
        auto_orient = self.settings.auto_orient
        case = self.current_load()

        def work(progress=None):
            return plan_splits(
                mesh,
                printer,
                auto_orient=auto_orient,
                analyse=True,
                assembly_intent=intent,
                progress=progress,
                load=case,
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

    def export_model(self) -> None:
        """Skriv modellen som den är just nu, utan att kapa den.

        Efter en måttändring är det ofta hela ärendet: modellen får plats som
        den är, eller ska tillbaka in i CAD. Att behöva gå via en kapning för
        att få ut filen vore omvägen.
        """
        if self.mesh_info is None:
            self.status("Öppna en modell först.", error=True)
            return

        source = self.mesh_info.path
        suggested = Path(self.settings.last_output_dir or source.parent) / (
            f"{source.stem}_ändrad.stl"
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Exportera modellen utan att dela den",
            str(suggested),
            "STL (*.stl);;3MF (*.3mf)",
        )
        if not path:
            return

        target = Path(path)
        self.settings.last_output_dir = str(target.parent)
        # Flera objekt skrivs var för sig - en fil per utskrift.
        meshes = (
            [part.mesh for part in self.parts]
            if len(self.parts) > 1
            else [self.mesh_info.mesh]
        )

        # Samma vändning som delarna får efter en kapning: en platta som står
        # upp i CAD-filen ska inte komma ut stående till slicern.
        flat = self.lay_flat_check.isChecked()

        def work(progress=None):
            if progress is not None:
                progress(0.3, "Skriver filer")
            return exporter.export_model(meshes, target, lay_flat=flat)

        self._start(work, self._on_model_exported, "Exporterar modellen…")

    def _on_model_exported(self, written) -> None:
        for path in written:
            self.status(f"Skrev {path}")
        if len(written) > 1:
            self.status(
                f"{len(written)} filer - ett objekt i taget, redo att skrivas ut."
            )
        self.status("Modellen är exporterad utan att delas.")

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
        lay_flat = self.lay_flat_check.isChecked()
        split_bodies = self.split_bodies_check.isChecked()

        def work(progress=None):
            # Har vi redan förhandsgranskat samma plan behöver vi inte kapa igen.
            result = ready
            if result is None:
                result = cut_mesh(
                    mesh, plan, joints=True, printer=printer, progress=progress
                )
            progress(0.97, "Skriver filer")
            export = exporter.export_parts(
                result,
                out_dir,
                printer,
                source=source,
                lay_flat=lay_flat,
                split_bodies=split_bodies,
            )
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
