"""GUI:t.

Testerna körs utan skärm (QT_QPA_PLATFORM=offscreen, satt i conftest). De
kontrollerar arbetsflödet och logiken - inte hur det ser ut. Bakgrundsarbetet
körs på riktigt, i en QThread, precis som när programmet används.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PySide6", reason="PySide6 krävs för GUI-testerna")
pytest.importorskip("pyqtgraph", reason="pyqtgraph krävs för GUI-testerna")

from PySide6.QtWidgets import QApplication  # noqa: E402

from stl_cutter.core import mesh_io  # noqa: E402
from stl_cutter.gui.app import (  # noqa: E402
    COLUMN_AXIS,
    COLUMN_INDEX,
    COLUMN_JOINT,
    COLUMN_MOTIVATION,
    COLUMN_POSITION,
    JOINT_LABELS,
    MainWindow,
)
from stl_cutter.gui.settings import Settings  # noqa: E402
from stl_cutter.gui.view3d import explode_offsets, part_colors, plane_quad  # noqa: E402
from stl_cutter.gui.workers import Worker, friendly_error  # noqa: E402

WORKER_TIMEOUT_MS = 120_000


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    """Ett fönster med isolerade inställningar och profiler."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    win = MainWindow(Settings())
    yield win
    win.close()


def wait_for_worker(qapp, window, timeout=WORKER_TIMEOUT_MS) -> None:
    """Vänta in bakgrundstråden och leverera dess signaler."""
    assert window.worker is not None, "inget bakgrundsarbete startades"
    assert window.worker.wait(timeout), "bakgrundsarbetet blev aldrig klart"
    qapp.processEvents()
    qapp.processEvents()


@pytest.fixture
def model_file(tmp_path, big_box) -> Path:
    path = tmp_path / "modell.stl"
    mesh_io.save_stl(big_box, path)
    return path


# --------------------------------------------------------------------------
# Uppbyggnad
# --------------------------------------------------------------------------


def test_window_starts_with_the_workflow_in_order(window):
    from PySide6.QtWidgets import QGroupBox

    titles = [box.title() for box in window.findChildren(QGroupBox)]

    assert titles == [
        "1. Modell",
        "2. Skrivare",
        "3. Montering",
        "4. Förslag",
        "5. Kapa och exportera",
    ]


def test_printer_dropdown_is_filled_and_fills_the_fields(window):
    assert window.printer_combo.count() >= 5

    window.printer_combo.setCurrentText("Prusa MK4")

    assert window.bed_x.value() == 250
    assert window.bed_y.value() == 210
    assert window.bed_z.value() == 220


def test_editing_the_bed_overrides_the_profile(window):
    window.bed_x.setValue(300)
    window.margin.setValue(8)

    printer = window.current_printer()

    assert printer.bed_x == 300
    assert printer.margin_mm == 8
    assert printer.usable[0] == 300 - 16


def test_saving_a_profile_adds_it_to_the_dropdown(window, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Min skrivare", True))
    window.bed_x.setValue(310)

    window.save_printer_profile()

    assert window.printer_combo.findText("Min skrivare") >= 0
    assert window.current_printer().bed_x == 310


def test_buttons_are_disabled_until_there_is_something_to_do(window):
    assert not window.analyse_button.isEnabled()
    assert not window.cut_button.isEnabled()


# --------------------------------------------------------------------------
# 1. Modell
# --------------------------------------------------------------------------


def test_loading_a_model_shows_its_facts(qapp, window, model_file):
    window.load_model(model_file)
    wait_for_worker(qapp, window)

    assert window.mesh_info is not None
    assert window.analyse_button.isEnabled()
    label = window.model_label.text()
    assert "modell.stl" in label
    assert "600" in label and "mm" in label
    assert "hel" in label.lower()
    assert "Meshen" in window.status_box.toPlainText() or "modell.stl" in window.status_box.toPlainText()


def test_loading_a_broken_file_gives_a_readable_message(qapp, window, tmp_path):
    broken = tmp_path / "trasig.stl"
    broken.write_text("det här är inte en STL-fil", encoding="utf-8")

    window.load_model(broken)
    wait_for_worker(qapp, window)

    text = window.status_box.toPlainText()
    assert "FEL:" in text
    assert "Traceback" not in text
    assert "loggen" in text
    assert window.mesh_info is None


def test_a_missing_file_gives_a_readable_message(qapp, window, tmp_path):
    window.load_model(tmp_path / "finns-inte.stl")
    wait_for_worker(qapp, window)

    assert "hitta" in window.status_box.toPlainText()
    assert "Traceback" not in window.status_box.toPlainText()


# --------------------------------------------------------------------------
# 4. Analys och fogval
# --------------------------------------------------------------------------


def test_analysis_fills_the_table(qapp, window, model_file):
    window.load_model(model_file)
    wait_for_worker(qapp, window)

    window.start_analysis()
    wait_for_worker(qapp, window)

    assert window.plan is not None
    assert window.cut_button.isEnabled()
    assert window.cut_table.rowCount() == len(window.plan.cuts)

    for row, cut in enumerate(window.plan.cuts):
        assert window.cut_table.item(row, COLUMN_INDEX).text() == str(cut.index)
        assert window.cut_table.cellWidget(row, COLUMN_AXIS).currentData() == cut.plane.axis
        position = window.cut_table.cellWidget(row, COLUMN_POSITION)
        assert position.value() == pytest.approx(cut.plane.position, abs=0.05)
        combo = window.cut_table.cellWidget(row, COLUMN_JOINT)
        assert combo.currentData() == cut.recommendation.joint_type
        assert (
            window.cut_table.item(row, COLUMN_MOTIVATION).text()
            == cut.recommendation.motivation
        )


def test_changing_the_joint_type_updates_the_plan(qapp, window, model_file):
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    window.start_analysis()
    wait_for_worker(qapp, window)

    combo = window.cut_table.cellWidget(0, COLUMN_JOINT)
    combo.setCurrentIndex(combo.findData("pins"))

    assert window.plan.cuts[0].recommendation.joint_type == "pins"
    assert (
        window.cut_table.item(0, COLUMN_MOTIVATION).text()
        == window.plan.cuts[0].recommendation.motivation
    )
    assert JOINT_LABELS["pins"] in window.status_box.toPlainText()


def test_every_joint_type_is_selectable(qapp, window, model_file):
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    window.start_analysis()
    wait_for_worker(qapp, window)

    combo = window.cut_table.cellWidget(0, COLUMN_JOINT)
    for joint_type in ("none", "puzzle", "dovetail", "pins", "screw"):
        combo.setCurrentIndex(combo.findData(joint_type))
        assert window.plan.cuts[0].recommendation.joint_type == joint_type


def test_assembly_intent_reaches_the_recommendation(qapp, window, tmp_path, long_rod):
    path = tmp_path / "stav.stl"
    mesh_io.save_stl(long_rod, path)
    window.load_model(path)
    wait_for_worker(qapp, window)

    window.demount_radio.setChecked(True)
    window.start_analysis()
    wait_for_worker(qapp, window)

    assert window.plan.assembly_intent == "demountable"
    assert any(c.recommendation.joint_type == "screw" for c in window.plan.cuts)


# --------------------------------------------------------------------------
# 5. Kapa och exportera
# --------------------------------------------------------------------------


def test_cutting_writes_the_parts_and_shows_them(qapp, window, model_file, tmp_path):
    out_dir = tmp_path / "ut"
    window.settings.last_output_dir = str(out_dir)

    window.load_model(model_file)
    wait_for_worker(qapp, window)
    window.start_analysis()
    wait_for_worker(qapp, window)
    window.start_cut()
    wait_for_worker(qapp, window)

    assert window.result is not None
    assert len(window.result.parts) == 3
    assert sorted(p.name for p in out_dir.glob("part_*.stl")) == [
        "part_01.stl",
        "part_02.stl",
        "part_03.stl",
    ]
    report = json.loads((out_dir / "split_report.json").read_text(encoding="utf-8"))
    assert report["result"]["joints"]

    status = window.status_box.toPlainText()
    assert "Kapade i 3 delar" in status
    assert "fogar" in status
    assert len(window.view._part_items) == 3


def test_cut_uses_the_manually_chosen_joint(qapp, window, model_file, tmp_path):
    window.settings.last_output_dir = str(tmp_path / "ut")
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    window.start_analysis()
    wait_for_worker(qapp, window)

    for row in range(window.cut_table.rowCount()):
        combo = window.cut_table.cellWidget(row, COLUMN_JOINT)
        combo.setCurrentIndex(combo.findData("pins"))

    window.start_cut()
    wait_for_worker(qapp, window)

    assert {j.requested_type for j in window.result.joints} == {"pins"}


def test_cutting_without_an_output_dir_asks_for_one(qapp, window, model_file, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: "")
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    window.start_analysis()
    wait_for_worker(qapp, window)
    window.settings.last_output_dir = ""

    window.start_cut()

    assert "målmapp" in window.status_box.toPlainText()


# --------------------------------------------------------------------------
# Bakgrundstråd, avbrott och fel
# --------------------------------------------------------------------------


def test_worker_reports_progress_and_result(qapp):
    seen = []
    worker = Worker(lambda progress: (progress(0.5, "halvvägs"), 42)[1])
    worker.progressed.connect(lambda f, m: seen.append((f, m)))
    results = []
    worker.succeeded.connect(results.append)

    worker.start()
    assert worker.wait(10_000)
    qapp.processEvents()

    assert seen == [(0.5, "halvvägs")]
    assert results == [42]


def test_worker_turns_an_exception_into_a_readable_message(qapp):
    def boom(progress):
        raise ValueError("meshen är trasig")

    worker = Worker(boom)
    messages = []
    worker.failed.connect(messages.append)

    worker.start()
    assert worker.wait(10_000)
    qapp.processEvents()

    assert messages == ["Modellen gick inte att tolka: meshen är trasig"]


def test_worker_can_be_cancelled(qapp):
    import time

    def slow(progress):
        for i in range(1000):
            progress(i / 1000, "arbetar")
            time.sleep(0.01)
        return "aldrig"

    worker = Worker(slow)
    cancelled = []
    worker.cancelled.connect(lambda: cancelled.append(True))

    worker.start()
    while not worker.isRunning():
        qapp.processEvents()
    worker.cancel()

    assert worker.wait(20_000)
    qapp.processEvents()
    assert cancelled == [True]


def test_cancelling_an_analysis_leaves_the_gui_usable(qapp, window, model_file):
    window.load_model(model_file)
    wait_for_worker(qapp, window)

    window.start_analysis()
    window.cancel_work()
    wait_for_worker(qapp, window)

    assert window.plan is None
    assert "Avbr" in window.status_box.toPlainText()
    assert window.analyse_button.isEnabled()
    assert not window.progress.isVisible()


@pytest.mark.parametrize(
    "exception,expected",
    [
        (FileNotFoundError(), "hitta"),
        (PermissionError(), "behörighet"),
        (MemoryError(), "minne"),
        (ValueError("x"), "tolka"),
        (RuntimeError("x"), "gick fel"),
    ],
)
def test_error_messages_are_in_swedish_and_without_stacktrace(exception, expected):
    message = friendly_error(exception)
    assert expected in message
    assert "Traceback" not in message


# --------------------------------------------------------------------------
# Inställningar och logg
# --------------------------------------------------------------------------


def test_settings_survive_a_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    Settings(printer="Prusa MK4", clearance_mm=0.25, assembly_intent="demountable").save(path)

    loaded = Settings.load(path)

    assert loaded.printer == "Prusa MK4"
    assert loaded.clearance_mm == 0.25
    assert loaded.assembly_intent == "demountable"


def test_broken_settings_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ trasig json", encoding="utf-8")

    assert Settings.load(path).printer == Settings().printer


def test_unknown_keys_in_settings_are_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"printer": "Ender 3", "framtida_nyhet": 1}), encoding="utf-8")

    assert Settings.load(path).printer == "Ender 3"


def test_closing_saves_the_settings(qapp, window, tmp_path):
    window.printer_combo.setCurrentText("Ender 3")
    window.demount_radio.setChecked(True)
    window.clearance.setValue(0.22)

    window.close()

    saved = Settings.load(Path(tmp_path) / "config" / "stl-cutter" / "settings.json")
    assert saved.printer == "Ender 3"
    assert saved.assembly_intent == "demountable"
    assert saved.clearance_mm == 0.22


def test_log_file_is_written(tmp_path, monkeypatch):
    import logging

    from stl_cutter.gui.logging_setup import setup_logging

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    path = setup_logging(tmp_path / "stl-cutter" / "log.txt")
    logging.getLogger("stl_cutter.test").info("en testrad")
    logging.shutdown()

    assert path.exists()
    assert "en testrad" in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 3D-vyn
# --------------------------------------------------------------------------


def test_part_colors_are_distinct():
    colors = part_colors(8)

    assert len(colors) == 8
    assert len(set(colors)) == 8
    assert all(len(c) == 4 and all(0 <= v <= 1 for v in c) for c in colors)


def test_explode_pushes_parts_away_from_the_centre():
    centres = np.array([[-100.0, 0, 0], [100.0, 0, 0], [0, 0, 0]])

    offsets = explode_offsets(centres, 50.0)

    assert offsets[0][0] == pytest.approx(-50.0)
    assert offsets[1][0] == pytest.approx(50.0)
    assert offsets[2][0] == pytest.approx(0.0)


def test_explode_zero_moves_nothing():
    centres = np.array([[-100.0, 0, 0], [100.0, 0, 0]])
    assert np.allclose(explode_offsets(centres, 0.0), 0.0)


def test_plane_quad_lies_in_the_plane_and_covers_the_model():
    from stl_cutter.core.planner import Plane

    bounds = np.array([[-100.0, -50, -25], [100.0, 50, 25]])
    plane = Plane(origin=(10.0, 0.0, 0.0), normal=(1.0, 0.0, 0.0), axis=0)

    vertices, faces = plane_quad(plane, bounds)

    assert np.allclose(vertices[:, 0], 10.0)
    assert vertices[:, 1].min() < -50 and vertices[:, 1].max() > 50
    assert len(faces) == 2


def test_view_shows_model_planes_and_parts(qapp, window, model_file):
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    assert window.view._model_item is not None

    window.start_analysis()
    wait_for_worker(qapp, window)
    assert len(window.view._plane_items) == len(window.plan.cuts)


def test_explode_slider_moves_the_parts(qapp, window, model_file, tmp_path):
    window.settings.last_output_dir = str(tmp_path / "ut")
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    window.start_analysis()
    wait_for_worker(qapp, window)
    window.start_cut()
    wait_for_worker(qapp, window)

    window.explode_slider.setValue(40)

    assert window.explode_label.text() == "40 mm"
    assert window.settings.explode_mm == 40.0


def test_bed_checkbox_toggles_the_grid(window):
    window.bed_checkbox.setChecked(True)
    assert window.view._bed_item is not None

    window.bed_checkbox.setChecked(False)
    assert window.view._bed_item is None


def test_drag_and_drop_accepts_stl(window, model_file):
    from PySide6.QtCore import QMimeData, QUrl

    data = QMimeData()
    data.setUrls([QUrl.fromLocalFile(str(model_file))])

    class FakeEvent:
        def mimeData(self):
            return data

    assert window._dropped_path(FakeEvent()) == model_file


def test_drag_and_drop_rejects_other_files(window, tmp_path):
    from PySide6.QtCore import QMimeData, QUrl

    other = tmp_path / "anteckningar.txt"
    other.write_text("nej", encoding="utf-8")
    data = QMimeData()
    data.setUrls([QUrl.fromLocalFile(str(other))])

    class FakeEvent:
        def mimeData(self):
            return data

    assert window._dropped_path(FakeEvent()) is None


# --------------------------------------------------------------------------
# Trasiga modeller i gränssnittet
# --------------------------------------------------------------------------


def test_a_repairable_model_is_reported_as_whole(qapp, window, tmp_path):
    """Sprickor lagas vid inläsning - användaren ska inte skrämmas i onödan."""
    import numpy as np
    import trimesh

    cracked = trimesh.creation.box(extents=[600.0, 300.0, 40.0]).subdivide()
    cracked.unmerge_vertices()
    cracked.vertices += np.random.default_rng(11).normal(0, 0.005, cracked.vertices.shape)
    path = tmp_path / "sprickig.stl"
    mesh_io.save_stl(cracked, path)

    window.load_model(path)
    wait_for_worker(qapp, window)

    assert window.mesh_info.watertight
    assert "Meshen är hel" in window.model_label.text()
    assert any("svetsade" in line for line in window.status_box.toPlainText().splitlines())


def test_unrepairable_damage_is_explained_not_blamed_on_the_cut(qapp, window, tmp_path):
    """Ärvda hål ska förklaras, inte rapporteras som FEL i kapningen."""
    import numpy as np
    import trimesh

    broken = trimesh.creation.box(extents=[600.0, 300.0, 100.0]).subdivide().subdivide()
    keep = np.ones(len(broken.faces), dtype=bool)
    keep[::4] = False
    broken.update_faces(keep)
    path = tmp_path / "trasig.stl"
    mesh_io.save_stl(broken, path)
    window.settings.last_output_dir = str(tmp_path / "ut")

    window.load_model(path)
    wait_for_worker(qapp, window)

    if window.mesh_info.watertight:
        pytest.skip("modellen gick att laga - inget ärvt fel att testa")

    assert "trasiga kanter" in window.model_label.text()
    assert "ärver" in window.status_box.toPlainText() or "hålen" in window.status_box.toPlainText()

    window.start_analysis()
    wait_for_worker(qapp, window)
    window.start_cut()
    wait_for_worker(qapp, window)

    status = window.status_box.toPlainText()
    if not window.result.all_watertight:
        assert "FEL: Del" not in status, "ärvda hål ska inte rapporteras som fel i kapningen"
        assert "slicer" in status


def test_background_can_be_switched(window):
    from stl_cutter.gui.view3d import DARK_BACKGROUND, LIGHT_BACKGROUND, MODEL_COLOR_ON_DARK

    window.light_checkbox.setChecked(True)
    assert window.view._light
    assert window.settings.light_background

    window.light_checkbox.setChecked(False)

    assert not window.view._light
    assert window.view.model_color == MODEL_COLOR_ON_DARK
    assert LIGHT_BACKGROUND != DARK_BACKGROUND


def test_background_choice_is_remembered(qapp, window, tmp_path):
    window.light_checkbox.setChecked(False)

    window.close()

    saved = Settings.load(Path(tmp_path) / "config" / "stl-cutter" / "settings.json")
    assert saved.light_background is False


def test_switching_background_keeps_the_model(qapp, window, model_file):
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    assert window.view._model_item is not None

    window.light_checkbox.setChecked(False)
    window.light_checkbox.setChecked(True)

    assert window.view._model_item is not None


def test_a_multi_body_model_is_merged_and_cut_cleanly(qapp, window, tmp_path):
    """En fil med flera kroppar ska bli en hel solid, inte trasiga delar."""
    import trimesh

    first = trimesh.creation.box(extents=[400.0, 200.0, 40.0])
    second = trimesh.creation.box(extents=[400.0, 200.0, 40.0])
    second.apply_translation([400.0, 0.0, 0.0])
    path = tmp_path / "flerkropp.stl"
    mesh_io.save_stl(trimesh.util.concatenate([first, second]), path)
    window.settings.last_output_dir = str(tmp_path / "ut")

    window.load_model(path)
    wait_for_worker(qapp, window)

    assert window.mesh_info.watertight
    assert "Meshen är hel" in window.model_label.text()

    window.start_analysis()
    wait_for_worker(qapp, window)
    window.start_cut()
    wait_for_worker(qapp, window)

    assert window.result.all_watertight
    assert "FEL:" not in window.status_box.toPlainText()
    assert any(j.applied for j in window.result.joints)


def test_a_picture_of_the_joint_is_shown(qapp, window, model_file):
    """Bilden bredvid motiveringen ska visa den valda fogtypen."""
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    window.start_analysis()
    wait_for_worker(qapp, window)

    # isVisible() kräver att toppfönstret visas; isHidden() speglar valet.
    assert not window.joint_image.isHidden()
    first = window.joint_image.pixmap().toImage()

    combo = window.cut_table.cellWidget(0, COLUMN_JOINT)
    combo.setCurrentIndex(combo.findData("pins"))

    assert window.joint_image.pixmap().toImage() != first, "bilden ska följa fogvalet"


def test_joint_help_dialog_opens(qapp, window, monkeypatch):
    from stl_cutter.gui import joint_help

    opened = []

    class FakeDialog:
        def __init__(self, labels, parent=None):
            opened.append(labels)

        def exec(self):
            return 0

    monkeypatch.setattr(joint_help, "JointHelpDialog", FakeDialog)
    monkeypatch.setattr("stl_cutter.gui.app.JointHelpDialog", FakeDialog)

    window.show_joint_help()

    assert opened and "dovetail" in opened[0]


def test_the_gui_survives_missing_joint_images(qapp, window, model_file, monkeypatch):
    from pathlib import Path as _Path

    from stl_cutter.gui import joint_images

    monkeypatch.setattr(joint_images, "_CANDIDATES", (_Path("/finns/inte"),))

    window.load_model(model_file)
    wait_for_worker(qapp, window)
    window.start_analysis()
    wait_for_worker(qapp, window)

    assert window.joint_image.isHidden()
    assert window.cut_table.rowCount() > 0


# --------------------------------------------------------------------------
# Att vrida på modellen med musen
# --------------------------------------------------------------------------


def _mouse_event(kind, button, x, y, buttons=None):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    held = buttons if buttons is not None else button
    return QMouseEvent(
        kind, QPointF(x, y), QPointF(x, y), button, held, Qt.NoModifier
    )


def _drag(view, button, path):
    """Tryck ner, dra längs `path` och släpp."""
    from PySide6.QtCore import QEvent, Qt

    start = path[0]
    view.mousePressEvent(_mouse_event(QEvent.MouseButtonPress, button, *start))
    for point in path[1:]:
        view.mouseMoveEvent(
            _mouse_event(QEvent.MouseMove, Qt.NoButton, *point, buttons=button)
        )
    view.mouseReleaseEvent(_mouse_event(QEvent.MouseButtonRelease, button, *path[-1]))


def test_right_drag_rotates_the_model(qapp, window, model_file):
    """Det användaren bad om: dra med höger musknapp för att se runt modellen."""
    from PySide6.QtCore import Qt

    window.load_model(model_file)
    wait_for_worker(qapp, window)
    view = window.view
    before = (view.opts["azimuth"], view.opts["elevation"])

    _drag(view, Qt.RightButton, [(100, 100), (140, 100), (180, 130)])

    after = (view.opts["azimuth"], view.opts["elevation"])
    assert after != before, "höger musknapp ska vrida kameran"
    assert after[0] != before[0], "vridningen i sidled saknas"
    assert after[1] != before[1], "vridningen i höjdled saknas"


def test_left_drag_still_rotates(qapp, window, model_file):
    from PySide6.QtCore import Qt

    window.load_model(model_file)
    wait_for_worker(qapp, window)
    view = window.view
    before = view.opts["azimuth"]

    _drag(view, Qt.LeftButton, [(100, 100), (150, 100)])

    assert view.opts["azimuth"] != before


def test_right_drag_with_ctrl_pans_instead(qapp, window, model_file):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    window.load_model(model_file)
    wait_for_worker(qapp, window)
    view = window.view
    angle_before = view.opts["azimuth"]
    centre_before = tuple(view.opts["center"])

    view.mousePressEvent(_mouse_event(QEvent.MouseButtonPress, Qt.RightButton, 100, 100))
    view.mouseMoveEvent(
        QMouseEvent(
            QEvent.MouseMove,
            QPointF(160, 140),
            QPointF(160, 140),
            Qt.NoButton,
            Qt.RightButton,
            Qt.ControlModifier,
        )
    )

    assert view.opts["azimuth"] == angle_before, "Ctrl ska flytta vyn, inte vrida"
    assert tuple(view.opts["center"]) != centre_before


def test_right_click_without_dragging_opens_the_view_menu(qapp, window, model_file):
    from PySide6.QtCore import QEvent, Qt

    window.load_model(model_file)
    wait_for_worker(qapp, window)
    view = window.view
    opened = []
    view.show_view_menu = lambda position: opened.append(position)

    view.mousePressEvent(_mouse_event(QEvent.MouseButtonPress, Qt.RightButton, 100, 100))
    view.mouseReleaseEvent(
        _mouse_event(QEvent.MouseButtonRelease, Qt.RightButton, 101, 102)
    )

    assert opened, "ett högerklick utan dragning ska öppna menyn"


def test_right_drag_does_not_open_the_menu(qapp, window, model_file):
    from PySide6.QtCore import Qt

    window.load_model(model_file)
    wait_for_worker(qapp, window)
    view = window.view
    opened = []
    view.show_view_menu = lambda position: opened.append(position)

    _drag(view, Qt.RightButton, [(100, 100), (200, 160)])

    assert not opened, "en dragning ska vrida, inte öppna meny"


def test_the_view_menu_offers_every_standard_angle(qapp, window):
    from stl_cutter.gui.view3d import STANDARD_VIEWS

    menu = window.view.build_view_menu()
    labels = [action.text() for action in menu.actions() if action.text()]

    for name in STANDARD_VIEWS:
        assert name in labels
    assert "Anpassa till modellen" in labels


@pytest.mark.parametrize("name", ["Ovanifrån", "Framifrån", "Från höger"])
def test_standard_views_set_the_camera(qapp, window, name):
    from stl_cutter.gui.view3d import STANDARD_VIEWS

    azimuth, elevation = STANDARD_VIEWS[name]
    window.view.set_view(azimuth, elevation)

    assert window.view.opts["azimuth"] == pytest.approx(azimuth)
    assert window.view.opts["elevation"] == pytest.approx(elevation)


def test_fit_view_zooms_to_the_model(qapp, window, model_file):
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    fitted = window.view.opts["distance"]

    window.view.opts["distance"] = 99999
    window.view.fit_view()

    assert window.view.opts["distance"] == pytest.approx(fitted)


def test_fit_view_follows_the_parts_after_cutting(qapp, window, model_file, tmp_path):
    window.settings.last_output_dir = str(tmp_path / "ut")
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    window.start_analysis()
    wait_for_worker(qapp, window)
    window.start_cut()
    wait_for_worker(qapp, window)

    window.explode_slider.setValue(80)
    bounds = window.view.content_bounds()

    assert bounds is not None
    # Sprängda delar täcker mer än originalet gjorde.
    assert float(bounds[1][0] - bounds[0][0]) > 600.0


def test_fit_view_is_harmless_with_an_empty_scene(qapp, window):
    window.view.fit_view()

    assert window.view.content_bounds() is None


# --------------------------------------------------------------------------
# Manuell kapning: sätt snitten själv
# --------------------------------------------------------------------------


def _position_widget(window, row):
    return window.cut_table.cellWidget(row, COLUMN_POSITION)


def _axis_widget(window, row):
    return window.cut_table.cellWidget(row, COLUMN_AXIS)


def _move_cut(window, row, position):
    widget = _position_widget(window, row)
    widget.setValue(position)
    window._on_position_settled(row)


def test_a_cut_can_be_added_without_running_the_analysis(qapp, window, model_file):
    """Man ska kunna börja med att placera ett snitt själv."""
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    assert window.plan is None
    assert window.add_cut_button.isEnabled()

    window.add_cut()

    assert window.plan is not None
    assert len(window.plan.cuts) == 1
    assert window.cut_table.rowCount() == 1
    assert window.cut_button.isEnabled()


def test_a_new_cut_lands_on_the_longest_axis(qapp, window, model_file):
    window.load_model(model_file)
    wait_for_worker(qapp, window)

    window.add_cut()

    # big_box är 600 x 200 x 100 mm - längsta axeln är X.
    assert window.plan.cuts[0].plane.axis == 0


def test_the_position_field_is_limited_to_the_model(qapp, window, model_file):
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    window.add_cut()

    widget = _position_widget(window, 0)

    assert widget.minimum() == pytest.approx(-300.0, abs=0.5)
    assert widget.maximum() == pytest.approx(300.0, abs=0.5)


def test_moving_a_cut_updates_the_plan_and_the_view(qapp, window, model_file):
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    window.add_cut()

    _move_cut(window, 0, -180.0)

    assert window.plan.cuts[0].plane.position == pytest.approx(-180.0)
    assert window.plan.cuts[0].analysis.position_mm == pytest.approx(-180.0)
    assert len(window.view._plane_items) == 1


def test_dragging_the_value_moves_the_plane_without_reanalysing(qapp, window, model_file):
    """Medan värdet ändras ska vyn följa med direkt - analysen kan vänta."""
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    window.add_cut()
    before = window.plan.cuts[0].analysis.position_mm

    _position_widget(window, 0).setValue(-120.0)  # bara valueChanged

    assert window.plan.cuts[0].plane.position == pytest.approx(-120.0)
    assert window.plan.cuts[0].analysis.position_mm == pytest.approx(before)


def test_the_axis_of_a_cut_can_be_changed(qapp, window, model_file):
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    window.add_cut()
    assert window.plan.cuts[0].plane.axis == 0

    _axis_widget(window, 0).setCurrentIndex(1)

    assert window.plan.cuts[0].plane.axis == 1
    assert window.plan.cuts[0].analysis is not None


def test_a_cut_can_be_removed(qapp, window, model_file):
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    window.add_cut()
    window.add_cut()
    assert window.cut_table.rowCount() == 2

    window.cut_table.setCurrentCell(0, 0)
    window.remove_cut()

    assert window.cut_table.rowCount() == 1
    assert len(window.plan.cuts) == 1


def test_removing_without_a_selection_says_so(qapp, window, model_file):
    window.load_model(model_file)
    wait_for_worker(qapp, window)

    window.remove_cut()

    assert "Markera ett snitt" in window.status_box.toPlainText()


def test_the_selection_follows_the_cut_when_rows_reorder(qapp, window, model_file):
    """Snitten sorteras efter läge - markeringen ska inte tappas bort."""
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    window.add_cut()
    _move_cut(window, 0, -200.0)
    window.add_cut()
    row = window.cut_table.currentRow()

    # Flytta det markerade snittet förbi det andra, så att raderna byter plats.
    _move_cut(window, row, -250.0)

    positions = [round(c.plane.position) for c in window.plan.cuts]
    assert positions == sorted(positions), "snitten ska ligga i ordning"
    selected = window.plan.cuts[window.cut_table.currentRow()]
    assert selected.plane.position == pytest.approx(-250.0), (
        "markeringen ska följa med det snitt som flyttades"
    )
    assert window.cut_table.currentRow() == 0, "det flyttade snittet ligger nu först"


def test_the_summary_warns_when_the_parts_do_not_fit(qapp, window, model_file):
    window.load_model(model_file)
    wait_for_worker(qapp, window)

    window.add_cut()  # ett snitt räcker inte för en 600 mm modell

    text = window.plan_summary.text()
    assert "2 delar" in text
    assert "får inte plats" in text


def test_the_summary_is_happy_when_everything_fits(qapp, window, model_file):
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    for position in (-200.0, 0.0, 200.0):
        window.add_cut()
        _move_cut(window, window.cut_table.currentRow(), position)

    text = window.plan_summary.text()

    assert "4 delar" in text
    assert "får inte plats" not in text


def test_manual_cuts_are_what_gets_cut(qapp, window, model_file, tmp_path):
    """Hela poängen: mina snitt, min fogtyp, mina delar."""
    window.settings.last_output_dir = str(tmp_path / "ut")
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    for position in (-200.0, 0.0, 200.0):
        window.add_cut()
        _move_cut(window, window.cut_table.currentRow(), position)
    for row in range(window.cut_table.rowCount()):
        combo = window.cut_table.cellWidget(row, COLUMN_JOINT)
        combo.setCurrentIndex(combo.findData("pins"))

    window.start_cut()
    wait_for_worker(qapp, window)

    assert len(window.result.parts) == 4
    assert {j.requested_type for j in window.result.joints} == {"pins"}
    assert window.result.all_watertight
    assert sorted(p.name for p in (tmp_path / "ut").glob("part_*.stl")) == [
        "part_01.stl",
        "part_02.stl",
        "part_03.stl",
        "part_04.stl",
    ]


def test_the_analysis_replaces_manual_cuts(qapp, window, model_file):
    """"Räkna ut åt mig" ska ge tillbaka programmets förslag."""
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    window.add_cut()
    _move_cut(window, 0, -280.0)
    assert len(window.plan.cuts) == 1

    window.start_analysis()
    wait_for_worker(qapp, window)

    assert len(window.plan.cuts) == 2  # 600 mm mot 246 mm användbart
    assert window.plan.orientation_name != "manuell"


def test_the_view_shows_the_model_in_the_plans_frame(qapp, window, tmp_path):
    """Snittplanen ritas i planens koordinatsystem - modellen måste följa med."""
    import numpy as np
    import trimesh

    rod = trimesh.creation.box(extents=[500.0, 60.0, 60.0])
    rod.apply_transform(trimesh.transformations.rotation_matrix(np.radians(45), [0, 0, 1]))
    path = tmp_path / "sned.stl"
    mesh_io.save_stl(rod, path)

    window.load_model(path)
    wait_for_worker(qapp, window)
    window.start_analysis()
    wait_for_worker(qapp, window)

    assert not np.allclose(window.plan.transform, np.eye(4)), "modellen ska ha roterats"
    shown = window.view.content_bounds()
    assert np.allclose(shown, window.plan.bounds, atol=1.0)
