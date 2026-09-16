"""Spara ett arbete och ta upp det igen.

Det som mäts är det enda som räknas: kommer exakt samma sak tillbaka? Ett
projekt som tappar ett fogval eller en måttändring är värre än inget projekt,
för då tror man att arbetet är sparat.
"""

from __future__ import annotations

import json
import zipfile

import pytest
import trimesh

from stl_cutter.core import project as project_core
from stl_cutter.core.load import LoadCase
from stl_cutter.core.printers import PrinterProfile


def shelf() -> trimesh.Trimesh:
    return trimesh.creation.box(extents=(250.0, 270.0, 182.0))


def a_project(**changes) -> project_core.Project:
    defaults = dict(
        mesh=shelf(),
        printer=PrinterProfile(name="Flashforge", bed_x=262, bed_y=262, bed_z=262),
        source="/hem/fredrik/hylla.stl",
        assembly_intent="demountable",
        load=LoadCase(mass_kg=5.0, support="cantilever", axis=1, fixed_at_low=True),
        cuts=[
            project_core.ProjectCut(
                axis=1, position_mm=201.1, joint_type="dovetail", params={"stop_mm": 6.0}
            )
        ],
        lay_flat=True,
        split_bodies=False,
        output_dir="/hem/fredrik/ut",
        base_profile="Synology hylla",
    )
    defaults.update(changes)
    return project_core.Project(**defaults)


# --------------------------------------------------------------------------
# Tur och retur
# --------------------------------------------------------------------------


def test_everything_comes_back(tmp_path):
    """Kärnan i funktionen: allt användaren bestämt ska vara kvar."""
    path = project_core.save_project(a_project(), tmp_path / "hylla")

    back = project_core.load_project(path)

    assert back.printer.name == "Flashforge"
    assert back.printer.bed_x == 262
    assert back.assembly_intent == "demountable"
    assert back.load is not None and back.load.mass_kg == pytest.approx(5.0)
    assert back.load.support == "cantilever" and back.load.axis == 1
    assert back.lay_flat is True and back.split_bodies is False
    assert back.output_dir == "/hem/fredrik/ut"
    assert back.base_profile == "Synology hylla"


def test_the_cut_and_its_joint_choice_survive(tmp_path):
    """Fogvalet är ett beslut användaren fattat - det får inte tappas bort."""
    path = project_core.save_project(a_project(), tmp_path / "hylla")

    cut = project_core.load_project(path).cuts[0]

    assert cut.axis == 1
    assert cut.position_mm == pytest.approx(201.1)
    assert cut.joint_type == "dovetail"
    assert cut.params["stop_mm"] == pytest.approx(6.0)


def test_the_model_is_in_the_file(tmp_path):
    """Poängen med att lägga modellen i filen: en måttändrad modell finns
    ingen annanstans, och originalet kan flyttas eller ändras i CAD."""
    resized = trimesh.creation.box(extents=(270.0, 270.0, 182.0))
    path = project_core.save_project(a_project(mesh=resized), tmp_path / "hylla")

    back = project_core.load_project(path)

    assert back.mesh.extents[0] == pytest.approx(270.0, abs=0.01)
    assert back.mesh.volume == pytest.approx(resized.volume, rel=1e-4)
    assert back.mesh.is_watertight


def test_a_project_without_cuts_is_fine(tmp_path):
    path = project_core.save_project(a_project(cuts=[]), tmp_path / "tom")

    back = project_core.load_project(path)

    assert back.cuts == []
    assert "inga" in back.describe()


def test_a_tilted_cut_keeps_its_angle(tmp_path):
    """Ett vinklat snitt måste spara sin normal - axeln räcker inte."""
    tilted = project_core.ProjectCut(
        axis=1, position_mm=100.0, normal=(0.0, 0.966, 0.259), joint_type="pins"
    )
    path = project_core.save_project(a_project(cuts=[tilted]), tmp_path / "vinklad")

    back = project_core.load_project(path).cuts[0]

    assert back.has_normal
    assert back.normal[2] == pytest.approx(0.259, abs=1e-4)


def test_a_straight_cut_needs_no_normal():
    straight = project_core.ProjectCut(axis=0, position_mm=10.0)

    assert not straight.has_normal


# --------------------------------------------------------------------------
# Filen
# --------------------------------------------------------------------------


def test_the_file_is_a_readable_zip(tmp_path):
    """Går något fel ska filen gå att öppna för hand."""
    path = project_core.save_project(a_project(), tmp_path / "hylla")

    with zipfile.ZipFile(path) as archive:
        assert sorted(archive.namelist()) == ["model.stl", "project.json"]
        settings = json.loads(archive.read("project.json").decode("utf-8"))

    assert settings["version"] == project_core.PROJECT_VERSION
    assert settings["saved"]


def test_the_suffix_is_added(tmp_path):
    path = project_core.save_project(a_project(), tmp_path / "utan-ändelse")

    assert path.suffix == project_core.PROJECT_SUFFIX


# --------------------------------------------------------------------------
# När det går fel
# --------------------------------------------------------------------------


def test_a_missing_file_says_so(tmp_path):
    with pytest.raises(project_core.ProjectError) as excinfo:
        project_core.load_project(tmp_path / "finns-inte.stlcut")

    assert "Hittar ingen" in str(excinfo.value)


def test_an_stl_is_not_a_project(tmp_path):
    """Väljer man en modell i stället för ett projekt ska det sägas rakt ut."""
    model = tmp_path / "modell.stl"
    shelf().export(model)

    with pytest.raises(project_core.ProjectError) as excinfo:
        project_core.load_project(model)

    assert "inget projekt" in str(excinfo.value)


def test_a_newer_format_is_refused_instead_of_half_read(tmp_path):
    """Hellre ett tydligt fel än att tyst tappa hälften av snitten."""
    path = project_core.save_project(a_project(), tmp_path / "hylla")
    with zipfile.ZipFile(path) as archive:
        settings = json.loads(archive.read("project.json").decode("utf-8"))
        model = archive.read("model.stl")
    settings["version"] = project_core.PROJECT_VERSION + 5
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("project.json", json.dumps(settings))
        archive.writestr("model.stl", model)

    with pytest.raises(project_core.ProjectError) as excinfo:
        project_core.load_project(path)

    assert "nyare version" in str(excinfo.value)


def test_a_zip_without_a_model_is_refused(tmp_path):
    path = tmp_path / "trasig.stlcut"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("project.json", json.dumps({"version": 1, "printer": {}}))

    with pytest.raises(project_core.ProjectError) as excinfo:
        project_core.load_project(path)

    assert "model.stl" in str(excinfo.value)


# --------------------------------------------------------------------------
# Beskrivningen
# --------------------------------------------------------------------------


def test_the_description_says_what_is_in_it(tmp_path):
    text = a_project().describe()

    assert "250.0 x 270.0 x 182.0 mm" in text
    assert "hylla.stl" in text
    assert "Flashforge" in text
    assert "dovetail" in text
    assert "Last:" in text
