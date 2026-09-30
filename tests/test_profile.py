"""Slicerprofilen.

Nyckelnamnen är avlästa ur OrcaSlicers egna profiler. Testerna vaktar dem:
byter ett namn tyst blir profilen en fil slicern läser utan att något händer,
och det är värre än ett fel - inställningarna ser ut att vara satta.
"""

from __future__ import annotations

import json

import pytest

from conftest import make_fake_orca

from stl_cutter.core import load as load_core
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

    profiles = [p for p in bundle.files if p.suffix == ".json"]
    assert len(profiles) == 1
    data = json.loads(profiles[0].read_text(encoding="utf-8"))
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
        filament_base="Flashforge HS PETG @FF C5P",
        normal_temp_c=235.0,
    )

    assert len([p for p in bundle.files if p.suffix == ".json"]) == 2
    # Inget saknas - den enda anteckningen är den om vad importen bygger på.
    assert not [n for n in bundle.notes if "filamentprofil" in n]
    ids = set()
    for path in (p for p in bundle.files if p.suffix == ".json"):
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


# --------------------------------------------------------------------------
# Listan att knappa in för hand
# --------------------------------------------------------------------------


def test_a_readable_list_is_written_next_to_the_profiles(tmp_path):
    """Importen kan vägra av skäl vi inte ser - värdena ska ändå gå att sätta."""
    bundle = profile_core.write_profiles(
        shelf_load(), tmp_path, base_profile="0.20mm Standard @FF C5"
    )

    text_files = [p for p in bundle.files if p.suffix == ".txt"]
    assert len(text_files) == 1
    text = text_files[0].read_text(encoding="utf-8")
    assert "0.20mm Standard @FF C5" in text
    assert "wall_loops" in text and "Väggar" in text
    assert "25%" in text


def test_the_list_names_the_filament_settings_when_they_were_written(tmp_path):
    bundle = profile_core.write_profiles(
        shelf_load(),
        tmp_path,
        base_profile="0.20mm Standard @FF C5",
        filament_base="Flashforge PETG @FF C5",
        normal_temp_c=235.0,
    )

    text = [p for p in bundle.files if p.suffix == ".txt"][0].read_text(encoding="utf-8")
    assert "243" in text  # 235 + 8
    assert "Fläkt max" in text


def test_the_note_says_which_profile_the_import_depends_on(tmp_path):
    bundle = profile_core.write_profiles(
        shelf_load(), tmp_path, base_profile="0.20mm Standard @FF C5"
    )

    assert any("0.20mm Standard @FF C5" in note for note in bundle.notes)
    assert any("inställningar.txt" in note for note in bundle.notes)


# --------------------------------------------------------------------------
# Samma krav som slicerns import
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kg", [1.0, 5.0])
def test_written_profiles_pass_the_import_checks(kg):
    known_process = {"0.20mm Standard @FF C5"}
    known_filament = {"Flashforge HS PETG @FF C5P"}
    process = profile_core.process_profile(shelf_load(kg), "0.20mm Standard @FF C5")
    filament = profile_core.filament_profile(shelf_load(kg), "Flashforge HS PETG @FF C5P", 235.0)

    assert profile_core.validate_profile(process, known_process) == []
    assert profile_core.validate_profile(filament, known_filament) == []


@pytest.mark.parametrize(
    "change, reason",
    [
        ({"version": None}, "version"),
        ({"version": "ett"}, "version"),
        ({"print_settings_id": None}, "Preset type is unknown"),
        ({"filament_settings_id": ["x"]}, "Flera id-fält"),
        ({"wall_loops": 5}, "sträng"),
        ({"type": "process"}, "leverantörsprofiler"),
        ({"inherits": "0.20mm Standard @FF C5 "}, "finns inte"),
        ({"name": "a/b"}, "sökvägstecken"),
    ],
)
def test_the_check_catches_what_the_import_refuses(change, reason):
    """Varje fel här har importen avvisat tyst - kontrollen ska säga det högt."""
    data = profile_core.process_profile(shelf_load(), "0.20mm Standard @FF C5")
    for key, value in change.items():
        if value is None:
            data.pop(key)
        else:
            data[key] = value

    problems = profile_core.validate_profile(data, {"0.20mm Standard @FF C5"})

    assert any(reason in p for p in problems), problems


# --------------------------------------------------------------------------
# Råden och profilen är samma data
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kg", [1.0, 5.0])
def test_the_preview_and_the_profile_say_the_same_thing(kg):
    """Det som visas i förhandsvisningen är exakt det som skrivs."""
    case = shelf_load(kg)
    advice = load_core.print_advice(case, normal_temp_c=235.0)
    process = profile_core.process_profile(case, "bas")
    filament = profile_core.filament_profile(case, "bas", 235.0)

    for setting in advice:
        for key, value in setting.profile:
            if setting.target == load_core.PROCESS:
                assert process[key] == value, key
            else:
                assert filament[key] == [value], key
    # Och inget i profilen som inte står i råden.
    shown = {k for s in advice for k, _ in s.profile}
    written = (set(process) | set(filament)) - set(profile_core.HEADER_KEYS)
    assert written == shown

    walls = next(s for s in advice if s.name.startswith("Väggar"))
    assert walls.value == f"{process['wall_loops']} st"
    layer = next(s for s in advice if s.name == "Lagerhöjd")
    assert layer.value == f"{process['layer_height']} mm"
    temp = next(s for s in advice if s.name == "Temperatur")
    assert temp.value.startswith(filament["nozzle_temperature"][0])


def test_the_orientation_is_marked_manual():
    advice = load_core.print_advice(shelf_load())

    orientation = next(s for s in advice if s.name == "Orientering")
    assert orientation.manual
    assert "ställs in manuellt" in load_core.describe_advice(shelf_load())


def test_a_six_tenths_nozzle_gets_the_same_layer_in_both():
    """Förut gav råden 0,39 mm och profilen 0,40 mm för samma munstycke."""
    advice = load_core.print_advice(shelf_load(), nozzle_mm=0.6)
    process = profile_core.process_profile(shelf_load(), "bas", nozzle_mm=0.6)

    layer = next(s for s in advice if s.name == "Lagerhöjd")
    assert layer.value == f"{process['layer_height']} mm"


def test_filament_values_follow_the_extruder_variants():
    out = profile_core.filament_profile(
        shelf_load(), "bas", 235.0, extruder_variants=["Direct Drive Standard", "Direct Drive High Flow"]
    )

    assert out["filament_extruder_variant"] == ["Direct Drive Standard", "Direct Drive High Flow"]
    assert out["nozzle_temperature"] == ["243", "243"]


# --------------------------------------------------------------------------
# Installerade slicers
# --------------------------------------------------------------------------


def test_slicers_are_found_in_config_and_flatpak(tmp_path):
    home = tmp_path / "home"
    make_fake_orca(home / ".config")
    make_fake_orca(home / ".var" / "app" / "com.orcaslicer.OrcaSlicer" / "config", "OrcaSlicer")
    # En mapp utan system/ är ingen slicer som körts.
    (home / ".config" / "BambuStudio").mkdir(parents=True)

    found = profile_core.find_slicers(home=home)

    assert [s.display_name for s in found] == ["Orca-Flashforge", "OrcaSlicer (Flatpak)"]


def test_no_slicer_means_an_empty_list(tmp_path):
    assert profile_core.find_slicers(home=tmp_path) == []


def test_profile_names_are_read_and_filtered_on_the_printer(tmp_path):
    home = tmp_path / "home"
    make_fake_orca(home / ".config", printer="Flashforge Creator 5 Pro 0.4 nozzle")
    install = profile_core.find_slicers(home=home)[0]

    printer = install.selected_printer
    processes = profile_core.system_profiles(install, "process", printer)
    filaments = profile_core.system_profiles(install, "filament", printer)

    assert printer == "Flashforge Creator 5 Pro 0.4 nozzle"
    # Bara de som är skrivna för C5 Pro, och aldrig en abstrakt basprofil.
    assert processes == ["0.20mm Standard @FF C5", "0.24mm Standard @FF C5"]
    assert filaments == ["Flashforge HS PETG @FF C5P", "Flashforge PLA Basic @FF C5P"]
    assert "fdm_process_common" not in profile_core.system_profiles(install, "process")


def test_a_user_printer_is_traced_to_its_system_parent(tmp_path):
    """Har man sparat en egen skrivarprofil står systemets namn i arvet."""
    home = tmp_path / "home"
    data = make_fake_orca(home / ".config", printer="Min C5")
    user_machine = data / "user" / "default" / "machine" / "Min C5.json"
    user_machine.parent.mkdir(parents=True)
    user_machine.write_text(
        json.dumps({"name": "Min C5", "inherits": "Flashforge Creator 5 Pro 0.4 nozzle"}),
        encoding="utf-8",
    )

    install = profile_core.find_slicers(home=home)[0]

    assert install.selected_printer == "Flashforge Creator 5 Pro 0.4 nozzle"


def test_the_user_folder_follows_the_login(tmp_path):
    """Inloggad i slicern läser den user/<id>, inte user/default."""
    home = tmp_path / "home"
    make_fake_orca(home / ".config", preset_folder="12345")
    install = profile_core.find_slicers(home=home)[0]

    assert install.user_dir == install.data_dir / "user" / "12345"


def test_profiles_are_installed_in_the_user_folder_named_as_the_profile(tmp_path):
    home = tmp_path / "home"
    make_fake_orca(home / ".config")
    install = profile_core.find_slicers(home=home)[0]
    process = profile_core.process_profile(shelf_load(), "0.20mm Standard @FF C5", "Hylla")
    filament = profile_core.filament_profile(shelf_load(), "Flashforge HS PETG @FF C5P", 235.0, "Hylla")

    written = profile_core.install_profiles(install, [process, filament])

    user = install.data_dir / "user" / "default"
    assert written == [user / "process" / "Hylla.json", user / "filament" / "Hylla.json"]
    # Slicern tar namnet från filnamnet - de måste stämma.
    for path in written:
        assert json.loads(path.read_text(encoding="utf-8"))["name"] == path.stem
    assert not any("user" in str(p) for p in (install.data_dir / "system").rglob("*Hylla*"))


def test_the_extruder_variants_come_from_the_base(tmp_path):
    home = tmp_path / "home"
    make_fake_orca(home / ".config")
    install = profile_core.find_slicers(home=home)[0]

    assert profile_core.extruder_variants(install, "Flashforge HS PETG @FF C5P") == [
        "Direct Drive Standard"
    ]


# --------------------------------------------------------------------------
# Egna profiler som bas
# --------------------------------------------------------------------------


def _install(tmp_path):
    home = tmp_path / "home"
    make_fake_orca(home / ".config", printer="Flashforge Creator 5 Pro 0.4 nozzle")
    return profile_core.find_slicers(home=home)[0]


def test_own_profiles_are_listed(tmp_path):
    install = _install(tmp_path)

    names = profile_core.user_profile_names(
        install, "process", "Flashforge Creator 5 Pro 0.4 nozzle"
    )

    # compatible_printers saknas i den egna filen och ärvs från systemprofilen.
    assert names == ["Synology hylla"]
    assert profile_core.user_profile_names(install, "process", "Någon annan skrivare") == []


def test_an_own_base_is_flattened_onto_the_system_profile(tmp_path):
    """Den nya profilen får ärva från systemet, inte från en annan egen
    profil: slicern läser användarmappen i godtycklig ordning och hoppar
    över en profil vars förälder inte är inläst än."""
    install = _install(tmp_path)

    base, extra = profile_core.resolve_base(install, "process", "Synology hylla")
    out = profile_core.process_profile(shelf_load(), base, "Synology hylla styrka", overrides=extra)

    assert out["inherits"] == "0.20mm Standard @FF C5"
    assert out["enable_support"] == "1" and out["support_style"] == "organic"
    # Hållfastheten vinner över den egna profilens två väggar.
    assert out["wall_loops"] == "5"
    assert out["name"] == out["print_settings_id"] == "Synology hylla styrka"
    assert profile_core.validate_profile(out, set(install.profiles("process"))) == []


def test_a_chain_of_own_profiles_is_followed(tmp_path):
    install = _install(tmp_path)
    child = install.user_dir / "process" / "Hylla 2.json"
    child.write_text(
        json.dumps({"name": "Hylla 2", "inherits": "Synology hylla", "support_style": "tree"}),
        encoding="utf-8",
    )
    install._profiles.clear()

    base, extra = profile_core.resolve_base(install, "process", "Hylla 2")

    assert base == "0.20mm Standard @FF C5"
    assert extra["support_style"] == "tree"  # närmast den valda vinner
    assert extra["enable_support"] == "1"


def test_a_system_base_needs_no_flattening(tmp_path):
    install = _install(tmp_path)

    assert profile_core.resolve_base(install, "process", "0.20mm Standard @FF C5") == (
        "0.20mm Standard @FF C5",
        {},
    )
    assert profile_core.resolve_base(None, "process", "vad som helst") == ("vad som helst", {})


def test_an_own_profile_without_a_system_parent_is_refused(tmp_path):
    install = _install(tmp_path)
    (install.user_dir / "process" / "Rot.json").write_text(
        json.dumps({"name": "Rot", "wall_loops": "3"}), encoding="utf-8"
    )
    install._profiles.clear()

    with pytest.raises(profile_core.ProfileError):
        profile_core.resolve_base(install, "process", "Rot")


def test_the_text_list_only_shows_the_strength_settings(tmp_path):
    """Den egna profilens inställningar finns redan i slicern - listan att
    knappa in för hand ska bara ha det programmet ändrar."""
    bundle = profile_core.write_profiles(
        shelf_load(), tmp_path, base_profile="bas", process_overrides={"enable_support": "1"}
    )

    text = [p for p in bundle.files if p.suffix == ".txt"][0].read_text(encoding="utf-8")
    assert "enable_support" not in text
    assert "wall_loops" in text
