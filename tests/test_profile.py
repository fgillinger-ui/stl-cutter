"""Slicerprofilen.

Nyckelnamnen är avlästa ur OrcaSlicers egna profiler. Testerna vaktar dem:
byter ett namn tyst blir profilen en fil slicern läser utan att något händer,
och det är värre än ett fel - inställningarna ser ut att vara satta.
"""

from __future__ import annotations

import json

import pytest

from stl_cutter.core import profile as profile_core
from stl_cutter.core.load import LoadCase


def shelf_load(kg: float = 5.0) -> LoadCase:
    return LoadCase(mass_kg=kg, support="cantilever")


# --------------------------------------------------------------------------
# Processprofilen
# --------------------------------------------------------------------------


def test_the_keys_are_the_ones_orca_reads():
    """Exakt de nycklar OrcaSlicers egna profiler använder."""
    out = profile_core.process_profile(shelf_load(), "0.20mm Standard @FF C5")

    assert out["inherits"] == "0.20mm Standard @FF C5"
    for key in (
        "wall_loops",
        "top_shell_layers",
        "bottom_shell_layers",
        "sparse_infill_density",
        "sparse_infill_pattern",
        "layer_height",
    ):
        assert key in out, f"{key} saknas - slicern skulle inte sätta något"


def test_the_version_is_there_and_parseable():
    """Utan version avbryter slicern inläsningen på rad tre och säger bara
    "There are 0 configs imported" - det felet kostade en runda."""
    out = profile_core.process_profile(shelf_load(), "bas")

    assert "version" in out
    parts = out["version"].split(".")
    assert len(parts) >= 3 and all(p.isdigit() for p in parts)


def test_the_type_comes_from_the_id_field_not_from_type():
    """OrcaSlicer avgör profiltypen av vilket id-fält som finns, inte av
    fältet "type". Saknas id:t blir det "Preset type is unknown"."""
    process = profile_core.process_profile(shelf_load(), "bas", name="Hylla")
    filament = profile_core.filament_profile(shelf_load(), "bas", 235.0, name="Hylla")

    assert process["print_settings_id"] == "Hylla"
    assert filament["filament_settings_id"] == ["Hylla"]
    assert "filament_settings_id" not in process
    assert "print_settings_id" not in filament


def test_vendor_only_fields_are_left_out():
    """"type" och "instantiation" hör till leverantörsprofiler. Slicern
    plockar bort dem ur en användarprofil och loggar det som ett fel."""
    out = profile_core.process_profile(shelf_load(), "bas")

    assert "type" not in out
    assert "instantiation" not in out


def test_every_value_is_a_string():
    """Orca läser värdena som strängar. Ett tal tolkas inte, det ignoreras."""
    out = profile_core.process_profile(shelf_load(), "bas")

    assert all(isinstance(v, str) for v in out.values())


def test_the_settings_match_the_advice():
    out = profile_core.process_profile(shelf_load(), "bas")

    assert out["wall_loops"] == "5"
    assert out["sparse_infill_density"] == "25%"
    assert out["sparse_infill_pattern"] == "gyroid"
    assert out["top_shell_layers"] == out["bottom_shell_layers"] == "5"


def test_a_light_load_gets_fewer_walls():
    out = profile_core.process_profile(shelf_load(1.0), "bas")

    assert out["wall_loops"] == "4"


def test_the_layer_height_follows_the_nozzle():
    """Ungefär 65 % av munstycket - ett 0,6-munstycke ska inte få 0,26 mm."""
    fine = profile_core.process_profile(shelf_load(), "bas", nozzle_mm=0.4)
    coarse = profile_core.process_profile(shelf_load(), "bas", nozzle_mm=0.6)

    assert float(fine["layer_height"]) == pytest.approx(0.26, abs=0.01)
    assert float(coarse["layer_height"]) == pytest.approx(0.40, abs=0.02)


def test_a_profile_without_a_base_is_refused():
    """En processprofil har hundratals inställningar och vi kan sju. Utan
    arvet hade resten varit påhittade siffror."""
    with pytest.raises(profile_core.ProfileError) as excinfo:
        profile_core.process_profile(shelf_load(), "   ")

    assert "rullgardin" in str(excinfo.value)


def test_no_load_means_no_profile():
    with pytest.raises(profile_core.ProfileError):
        profile_core.process_profile(LoadCase(), "bas")


# --------------------------------------------------------------------------
# Filamentprofilen
# --------------------------------------------------------------------------


def test_filament_values_are_lists_of_strings():
    """I filamentprofiler är varje värde en lista - en post per extruder."""
    out = profile_core.filament_profile(shelf_load(), "PETG-bas", 235.0)

    assert out["nozzle_temperature"] == ["243"]
    assert out["nozzle_temperature_initial_layer"] == ["243"]
    assert out["fan_max_speed"] == ["50"]
    assert out["fan_min_speed"] == ["30"]


def test_the_temperature_must_be_given():
    """+8 °C över *vad*? PLA 215, PETG 235, ASA 255 - det går inte att gissa,
    och en gissad temperatur i en fil som ser beräknad ut är värre än ingen."""
    with pytest.raises(profile_core.ProfileError) as excinfo:
        profile_core.filament_profile(shelf_load(), "bas", 0.0)

    assert "temperatur" in str(excinfo.value)


# --------------------------------------------------------------------------
# Filerna
# --------------------------------------------------------------------------


def test_the_files_are_valid_json(tmp_path):
    bundle = profile_core.write_profiles(
        shelf_load(), tmp_path, base_profile="0.20mm Standard @FF C5"
    )

    assert len(bundle.files) == 1
    data = json.loads(bundle.files[0].read_text(encoding="utf-8"))
    assert data["name"] == "Bärande delar"


def test_a_missing_temperature_is_said_out_loud(tmp_path):
    """Att filamentprofilen uteblev ska stå i klartext, annars ser man bara en
    fil och tror att temperaturen är omhändertagen."""
    bundle = profile_core.write_profiles(shelf_load(), tmp_path, base_profile="bas")

    assert bundle.notes and "filamentprofil" in bundle.notes[0]


def test_both_files_are_written_when_the_filament_is_known(tmp_path):
    bundle = profile_core.write_profiles(
        shelf_load(),
        tmp_path,
        base_profile="0.20mm Standard @FF C5",
        filament_base="Flashforge HS PETG @FF C5",
        normal_temp_c=235.0,
    )

    assert len(bundle.files) == 2
    assert not bundle.notes
    ids = set()
    for path in bundle.files:
        data = json.loads(path.read_text(encoding="utf-8"))
        ids |= {k for k in data if k.endswith("_settings_id")}
    assert ids == {"print_settings_id", "filament_settings_id"}


def test_the_name_survives_as_a_filename(tmp_path):
    """Svenska tecken i namnet ska inte ge ett filnamn som inte går att öppna.

    Slicern använder dessutom profilnamnet som filnamn när den sparar, och
    vägrar ett namn med sökvägstecken - så det måste städas i filen också,
    inte bara på disken.
    """
    bundle = profile_core.write_profiles(
        shelf_load(), tmp_path, base_profile="bas", name="Hylla åäö / NAS"
    )

    assert bundle.files[0].exists()
    assert "/" not in bundle.files[0].name
    data = json.loads(bundle.files[0].read_text(encoding="utf-8"))
    assert "/" not in data["name"]
    assert data["name"] == data["print_settings_id"]
