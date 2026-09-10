import json

from stl_cutter.core.printers import PrinterProfile, get_printer, load_printers, save_profile


def test_builtin_profiles_present():
    names = [p.name for p in load_printers(None)]
    assert "Bambu Lab P1S" in names
    assert "Prusa MK4" in names
    assert "Ender 3" in names
    assert "Custom" in names


def test_usable_volume_subtracts_margin_on_both_sides():
    profile = PrinterProfile("t", 256, 256, 256, margin_mm=5)
    assert profile.usable == (246.0, 246.0, 246.0)


def test_fuzzy_lookup_finds_bambu():
    assert get_printer("Bambu P1S", None).name == "Bambu Lab P1S"


def test_save_and_reload_user_profile(tmp_path):
    user_file = tmp_path / "printers.json"
    profile = PrinterProfile("Min skrivare", 300, 300, 400, margin_mm=8, clearance_mm=0.25)
    save_profile(profile, user_file)

    stored = json.loads(user_file.read_text(encoding="utf-8"))
    assert stored["printers"][0]["name"] == "Min skrivare"

    assert get_printer("Min skrivare", user_file).bed_z == 400
