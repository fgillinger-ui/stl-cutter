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
from stl_cutter.gui.app import JOINT_LABELS, MainWindow  # noqa: E402
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
        assert window.cut_table.item(row, 0).text() == str(cut.index)
        assert "mm" in window.cut_table.item(row, 1).text()
        combo = window.cut_table.cellWidget(row, 2)
        assert combo.currentData() == cut.recommendation.joint_type
        assert window.cut_table.item(row, 3).text() == cut.recommendation.motivation


def test_changing_the_joint_type_updates_the_plan(qapp, window, model_file):
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    window.start_analysis()
    wait_for_worker(qapp, window)

    combo = window.cut_table.cellWidget(0, 2)
    combo.setCurrentIndex(combo.findData("pins"))

    assert window.plan.cuts[0].recommendation.joint_type == "pins"
    assert window.cut_table.item(0, 3).text() == window.plan.cuts[0].recommendation.motivation
    assert JOINT_LABELS["pins"] in window.status_box.toPlainText()


def test_every_joint_type_is_selectable(qapp, window, model_file):
    window.load_model(model_file)
    wait_for_worker(qapp, window)
    window.start_analysis()
    wait_for_worker(qapp, window)

    combo = window.cut_table.cellWidget(0, 2)
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
        combo = window.cut_table.cellWidget(row, 2)
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
