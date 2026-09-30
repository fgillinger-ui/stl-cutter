"""Testgeometri genererad i koden - inga binära testfiler i repot."""

from __future__ import annotations

import os

# Qt måste veta att det inte finns någon skärm innan det importeras.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import trimesh

from stl_cutter.core.printers import PrinterProfile


@pytest.fixture
def printer() -> PrinterProfile:
    """En Bambu-liknande profil: 256 mm kub, 5 mm marginal -> 246 mm användbart."""
    return PrinterProfile(name="Test 256", bed_x=256.0, bed_y=256.0, bed_z=256.0, margin_mm=5.0)


@pytest.fixture
def small_box() -> trimesh.Trimesh:
    """Får plats som den är."""
    return trimesh.creation.box(extents=[100.0, 80.0, 60.0])


@pytest.fixture
def big_box() -> trimesh.Trimesh:
    """För stor i en axel."""
    return trimesh.creation.box(extents=[600.0, 200.0, 100.0])


@pytest.fixture
def two_axis_box() -> trimesh.Trimesh:
    """För stor i två axlar."""
    return trimesh.creation.box(extents=[500.0, 400.0, 150.0])


@pytest.fixture
def big_cylinder() -> trimesh.Trimesh:
    return trimesh.creation.cylinder(radius=180.0, height=400.0, sections=64)


@pytest.fixture
def big_torus() -> trimesh.Trimesh:
    return trimesh.creation.torus(major_radius=200.0, minor_radius=50.0)


@pytest.fixture
def long_rod() -> trimesh.Trimesh:
    """Lång stav - snittet blir kompakt och tjockt (laxstjärt eller pinnar)."""
    return trimesh.creation.box(extents=[500.0, 60.0, 60.0])


@pytest.fixture
def thin_plate() -> trimesh.Trimesh:
    """Tunn platta - snittet blir bara 3 mm tjockt (pussel eller ingen fog)."""
    return trimesh.creation.box(extents=[600.0, 300.0, 3.0])


@pytest.fixture
def medium_plate() -> trimesh.Trimesh:
    """6 mm platt snitt - hamnar i pusselintervallet 4-8 mm."""
    return trimesh.creation.box(extents=[600.0, 300.0, 6.0])


@pytest.fixture
def big_sphere() -> trimesh.Trimesh:
    """Stort klot - runt snitt, ska ge pinnar."""
    return trimesh.creation.icosphere(subdivisions=4, radius=200.0)


@pytest.fixture
def necked_bar() -> trimesh.Trimesh:
    """Stav med ett 3 mm tunt midjeparti exakt där det jämnt fördelade snittet
    skulle hamna. Planeraren ska flytta snittet därifrån."""
    bar = trimesh.creation.box(extents=[500.0, 60.0, 60.0])
    neck_x = -500.0 / 3.0 + 500.0 / 6.0  # = nominell position för första snittet
    cutters = []
    for sign in (1.0, -1.0):
        block = trimesh.creation.box(extents=[20.0, 30.0, 80.0])
        block.apply_translation([neck_x, sign * 16.5, 0.0])
        cutters.append(block)
    return bar.difference(trimesh.util.concatenate(cutters))


def make_fake_orca(config_home, app_key="Orca-Flashforge", preset_folder="", printer=None):
    """En slicers datamapp i miniatyr, uppbyggd som Orca-Flashforges.

    Namnen och arvet är avlästa ur Orca-Flashforges resources/profiles
    (release_0817): processprofilerna heter "@FF C5", filamenten "@FF C5P",
    och C5P-filamenten ärver från "Generic PETG HF @System" i biblioteket.
    """
    import json
    from pathlib import Path

    data = Path(config_home) / app_key
    system = data / "system"
    ff = system / "Flashforge"

    def put(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content, indent=1), encoding="utf-8")

    c5p = "Flashforge Creator 5 Pro 0.4 nozzle"
    ad5m = "Flashforge Adventurer 5M 0.4 Nozzle"
    put(system / "Flashforge.json", {
        "name": "Flashforge", "version": "02.01.01.02",
        "machine_list": [{"name": c5p, "sub_path": f"machine/{c5p}.json"}],
        "process_list": [
            {"name": "fdm_process_common", "sub_path": "process/fdm_process_common.json"},
            {"name": "0.20mm Standard @FF C5", "sub_path": "process/0.20mm Standard @FF C5.json"},
            {"name": "0.24mm Standard @FF C5", "sub_path": "process/0.24mm Standard @FF C5.json"},
            {"name": "0.20mm Standard @Flashforge AD5M 0.4 Nozzle",
             "sub_path": "process/0.20mm Standard @Flashforge AD5M 0.4 Nozzle.json"},
        ],
        "filament_list": [
            {"name": "Flashforge HS PETG @FF C5P", "sub_path": "filament/Flashforge HS PETG @FF C5P.json"},
            {"name": "Flashforge PLA Basic @FF C5P", "sub_path": "filament/Flashforge PLA Basic @FF C5P.json"},
            {"name": "Flashforge PLA Basic @FF AD5M", "sub_path": "filament/Flashforge PLA Basic @FF AD5M.json"},
        ],
    })
    put(ff / "machine" / f"{c5p}.json", {"name": c5p, "instantiation": "true",
                                          "printer_settings_id": c5p})
    put(ff / "process" / "fdm_process_common.json",
        {"name": "fdm_process_common", "instantiation": "false"})
    for name in ("0.20mm Standard @FF C5", "0.24mm Standard @FF C5"):
        put(ff / "process" / f"{name}.json", {
            "name": name, "inherits": "fdm_process_common", "instantiation": "true",
            "print_settings_id": name, "compatible_printers": [c5p],
        })
    put(ff / "process" / "0.20mm Standard @Flashforge AD5M 0.4 Nozzle.json", {
        "name": "0.20mm Standard @Flashforge AD5M 0.4 Nozzle", "instantiation": "true",
        "compatible_printers": [ad5m],
    })
    put(ff / "filament" / "Flashforge HS PETG @FF C5P.json", {
        "name": "Flashforge HS PETG @FF C5P", "inherits": "Generic PETG HF @System",
        "instantiation": "true", "compatible_printers": [c5p],
        "nozzle_temperature": ["235"], "filament_extruder_variant": ["Direct Drive Standard"],
    })
    put(ff / "filament" / "Flashforge PLA Basic @FF C5P.json", {
        "name": "Flashforge PLA Basic @FF C5P", "instantiation": "true",
        "compatible_printers": [c5p], "nozzle_temperature": ["210"],
    })
    put(ff / "filament" / "Flashforge PLA Basic @FF AD5M.json", {
        "name": "Flashforge PLA Basic @FF AD5M", "instantiation": "true",
        "compatible_printers": [ad5m], "nozzle_temperature": ["220"],
    })
    put(system / "OrcaFilamentLibrary.json", {
        "name": "OrcaFilamentLibrary",
        "filament_list": [{"name": "Generic PETG HF @System",
                           "sub_path": "filament/Generic PETG HF @System.json"}],
    })
    put(system / "OrcaFilamentLibrary" / "filament" / "Generic PETG HF @System.json", {
        "name": "Generic PETG HF @System", "instantiation": "true",
        "nozzle_temperature": ["240"],
    })
    conf = {"app": {"preset_folder": preset_folder}}
    if printer is not None:
        conf["presets"] = {"machine": printer}
    put(data / f"{app_key}.conf", conf)
    return data


@pytest.fixture
def fake_orca(tmp_path, monkeypatch):
    """Orca-Flashforge "installerad" i en falsk hemkatalog, C5 Pro vald."""
    home = tmp_path / "home"
    config = home / ".config"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    return make_fake_orca(config, printer="Flashforge Creator 5 Pro 0.4 nozzle")
