import json

import pytest

from stl_cutter.cli import main
from stl_cutter.core import exporter, mesh_io
from stl_cutter.core.cutter import cut_mesh
from stl_cutter.core.planner import plan_splits


def test_export_writes_parts_and_report(tmp_path, big_box, printer):
    plan = plan_splits(big_box, printer, auto_orient=False)
    result = cut_mesh(big_box, plan)

    export = exporter.export_parts(result, tmp_path / "ut", printer)

    assert [p.name for p in export.part_files] == ["part_01.stl", "part_02.stl", "part_03.stl"]
    assert all(p.stat().st_size > 0 for p in export.part_files)

    report = json.loads(export.report_file.read_text(encoding="utf-8"))
    assert report["plan"]["part_count"] == 3
    assert len(report["result"]["parts"]) == 3
    assert report["result"]["parts"][0]["file"] == "part_01.stl"
    assert report["printer"]["name"] == printer.name

    reloaded = mesh_io.load_mesh(export.part_files[0])
    assert reloaded.watertight


def test_cli_cut_end_to_end(tmp_path, big_box, capsys):
    model = tmp_path / "modell.stl"
    mesh_io.save_stl(big_box, model)
    out = tmp_path / "ut"

    code = main(["cut", str(model), "--printer", "Bambu P1S", "--out", str(out)])

    assert code == 0
    assert (out / "split_report.json").exists()
    assert sorted(p.name for p in out.glob("part_*.stl")) == [
        "part_01.stl",
        "part_02.stl",
        "part_03.stl",
    ]
    assert "Kapade i 3 delar" in capsys.readouterr().out


def test_cli_dry_run_writes_only_the_plan(tmp_path, big_box):
    model = tmp_path / "modell.stl"
    mesh_io.save_stl(big_box, model)
    out = tmp_path / "ut"

    assert main(["cut", str(model), "--printer", "Prusa MK4", "--out", str(out), "--dry-run"]) == 0

    assert list(out.glob("part_*.stl")) == []
    report = json.loads((out / "split_report.json").read_text(encoding="utf-8"))
    assert report["result"] is None
    assert report["plan"]["part_count"] >= 3


def test_cli_list_printers(capsys):
    assert main(["--list-printers"]) == 0
    assert "Bambu Lab P1S" in capsys.readouterr().out


def test_cli_unknown_printer_is_a_friendly_error(tmp_path, big_box, capsys):
    model = tmp_path / "modell.stl"
    mesh_io.save_stl(big_box, model)

    assert main(["cut", str(model), "--printer", "Finns inte", "--out", str(tmp_path)]) == 2
    assert "Okänd skrivare" in capsys.readouterr().err


def test_report_contains_analysis_and_recommendations(tmp_path, big_box, printer):
    """Fas 2: rapporten ska bära analys, poäng och de tre bästa fogförslagen."""
    plan = plan_splits(big_box, printer, auto_orient=False, analyse=True)
    result = cut_mesh(big_box, plan)

    export = exporter.export_parts(result, tmp_path / "ut", printer)
    report = json.loads(export.report_file.read_text(encoding="utf-8"))

    cuts = report["plan"]["cuts"]
    assert len(cuts) == 2
    for cut in cuts:
        assert cut["analysis"]["area_mm2"] > 0
        assert cut["analysis"]["contour_count"] >= 1
        assert cut["analysis"]["min_wall_mm"] > 0
        assert "roundness" in cut["analysis"]
        assert cut["score"]["total"] >= 0
        assert cut["score"]["penalties"] is not None
        assert cut["recommendation"]["joint_type"] in (
            "none",
            "puzzle",
            "dovetail",
            "pins",
            "screw",
        )
        assert cut["recommendation"]["motivation"]
        assert 0.0 <= cut["recommendation"]["confidence"] <= 1.0
        assert 1 <= len(cut["alternatives"]) <= 3
    assert report["plan"]["assembly_intent"] == "glue"


def test_cli_explain_prints_swedish_motivations(tmp_path, big_box, capsys):
    model = tmp_path / "modell.stl"
    mesh_io.save_stl(big_box, model)

    code = main(
        [
            "cut",
            str(model),
            "--printer",
            "Bambu P1S",
            "--out",
            str(tmp_path / "ut"),
            "--dry-run",
            "--explain",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "Rekommendation:" in out
    assert "Motivering:" in out
    assert "Säkerhet:" in out


def test_cli_assembly_intent_changes_the_recommendation(tmp_path, long_rod, capsys):
    model = tmp_path / "stav.stl"
    mesh_io.save_stl(long_rod, model)

    main(
        [
            "cut",
            str(model),
            "--printer",
            "Bambu P1S",
            "--out",
            str(tmp_path / "ut"),
            "--dry-run",
            "--explain",
            "--assembly",
            "demountable",
        ]
    )
    demountable = capsys.readouterr().out

    assert "screw" in demountable
    assert "tas isär" in demountable


def test_cli_no_analysis_skips_the_recommendation(tmp_path, big_box, capsys):
    model = tmp_path / "modell.stl"
    mesh_io.save_stl(big_box, model)
    out_dir = tmp_path / "ut"

    main(
        [
            "cut",
            str(model),
            "--printer",
            "Bambu P1S",
            "--out",
            str(out_dir),
            "--dry-run",
            "--no-analysis",
        ]
    )

    report = json.loads((out_dir / "split_report.json").read_text(encoding="utf-8"))
    assert all(cut["analysis"] is None for cut in report["plan"]["cuts"])


def test_report_contains_the_joints(tmp_path, big_box, printer):
    """Fas 3: rapporten ska visa vilka fogar som byggdes mellan vilka delar."""
    plan = plan_splits(big_box, printer, auto_orient=False, analyse=True)
    result = cut_mesh(big_box, plan, joints=True, printer=printer)

    export = exporter.export_parts(result, tmp_path / "ut", printer)
    report = json.loads(export.report_file.read_text(encoding="utf-8"))

    joints = report["result"]["joints"]
    assert joints
    for joint in joints:
        assert joint["applied"] is True
        assert joint["fell_back"] is False
        assert joint["joint_type"] in ("pins", "dovetail", "puzzle", "screw")
        assert joint["part_a"] < joint["part_b"]
        assert joint["warnings"] == []


def test_cli_forces_a_joint_type(tmp_path, big_box, capsys):
    model = tmp_path / "modell.stl"
    mesh_io.save_stl(big_box, model)
    out_dir = tmp_path / "ut"

    code = main(
        ["cut", str(model), "--printer", "Bambu P1S", "--out", str(out_dir), "--joint", "pins"]
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "fogar" in out
    report = json.loads((out_dir / "split_report.json").read_text(encoding="utf-8"))
    assert {j["joint_type"] for j in report["result"]["joints"]} == {"pins"}


def test_cli_can_skip_the_joints(tmp_path, big_box):
    model = tmp_path / "modell.stl"
    mesh_io.save_stl(big_box, model)
    out_dir = tmp_path / "ut"

    assert (
        main(
            [
                "cut",
                str(model),
                "--printer",
                "Bambu P1S",
                "--out",
                str(out_dir),
                "--no-joints",
            ]
        )
        == 0
    )

    report = json.loads((out_dir / "split_report.json").read_text(encoding="utf-8"))
    assert report["result"]["joints"] == []


def test_exported_parts_with_joints_reload_cleanly(tmp_path, big_box, printer):
    plan = plan_splits(big_box, printer, auto_orient=False, analyse=True)
    result = cut_mesh(big_box, plan, joints=True, printer=printer)

    export = exporter.export_parts(result, tmp_path / "ut", printer)

    for path in export.part_files:
        info = mesh_io.load_mesh(path)
        assert info.watertight, f"{path.name} är inte hel efter export"


# --------------------------------------------------------------------------
# Export utan att dela
# --------------------------------------------------------------------------


def test_exporting_one_object_writes_exactly_that_file(tmp_path):
    """Modellen ska gå att få ut som den är, utan att kapas."""
    import trimesh

    from stl_cutter.core import exporter as exporter_module

    mesh = trimesh.creation.box(extents=(40.0, 30.0, 20.0))

    written = exporter_module.export_model(mesh, tmp_path / "modell.stl")

    assert [p.name for p in written] == ["modell.stl"]
    back = trimesh.load(written[0])
    assert back.is_watertight
    assert back.extents == pytest.approx(mesh.extents, abs=0.01)


def test_exporting_several_objects_writes_one_file_each(tmp_path):
    """Varje objekt blir en egen utskrift och därmed en egen fil."""
    import trimesh

    from stl_cutter.core import exporter as exporter_module

    a = trimesh.creation.box(extents=(40.0, 30.0, 20.0))
    b = trimesh.creation.box(extents=(10.0, 10.0, 10.0))

    written = exporter_module.export_model([a, b], tmp_path / "delar.stl")

    assert [p.name for p in written] == ["delar_01.stl", "delar_02.stl"]
    assert all(p.exists() for p in written)


def test_the_export_format_follows_the_extension(tmp_path):
    import trimesh

    from stl_cutter.core import exporter as exporter_module

    mesh = trimesh.creation.box(extents=(20.0, 20.0, 20.0))

    written = exporter_module.export_model(mesh, tmp_path / "modell.3mf")

    assert written[0].suffix == ".3mf"


def test_an_unknown_export_format_is_refused(tmp_path):
    import trimesh

    from stl_cutter.core import exporter as exporter_module

    with pytest.raises(ValueError):
        exporter_module.export_model(
            trimesh.creation.box(extents=(10.0, 10.0, 10.0)),
            tmp_path / "modell.obj",
        )


def test_the_cli_exports_without_cutting(tmp_path, capsys):
    """`export` ska skriva modellen som den är, en fil per objekt."""
    import trimesh

    a = trimesh.creation.box(extents=(40.0, 30.0, 20.0))
    b = trimesh.creation.box(extents=(20.0, 20.0, 20.0))
    b.apply_translation([200.0, 0.0, 0.0])
    model = tmp_path / "tva.stl"
    trimesh.util.concatenate([a, b]).export(model)

    code = main(["export", str(model), "--out", str(tmp_path / "ut" / "modell.stl")])

    assert code == 0
    written = sorted(p.name for p in (tmp_path / "ut").glob("*.stl"))
    assert written == ["modell_01.stl", "modell_02.stl"]


def test_the_cli_can_keep_the_objects_in_one_file(tmp_path):
    import trimesh

    a = trimesh.creation.box(extents=(40.0, 30.0, 20.0))
    b = trimesh.creation.box(extents=(20.0, 20.0, 20.0))
    b.apply_translation([200.0, 0.0, 0.0])
    model = tmp_path / "tva.stl"
    trimesh.util.concatenate([a, b]).export(model)

    code = main(
        ["export", str(model), "--merge", "--out", str(tmp_path / "ut" / "allt.stl")]
    )

    assert code == 0
    assert (tmp_path / "ut" / "allt.stl").exists()
    assert sorted(p.name for p in (tmp_path / "ut").glob("*.stl")) == ["allt.stl"]


# --------------------------------------------------------------------------
# Export av kapade delar: vänd platt och dela upp lösa kroppar
# --------------------------------------------------------------------------


def _result_with(meshes):
    """Ett CutResult av färdiga meshar, utan att gå vägen via en kapning."""
    from stl_cutter.core.cutter import CutResult, Part
    from stl_cutter.core.planner import SplitPlan

    parts = [Part(index=i, mesh=m) for i, m in enumerate(meshes, start=1)]
    plan = SplitPlan(cuts=[], part_count=len(parts), part_boxes=[], orientation_name="test")
    return CutResult(
        parts=parts,
        original_volume_mm3=sum(abs(m.volume) for m in meshes),
        plan=plan,
    )


def test_exported_parts_are_laid_flat(tmp_path, printer):
    """En del på högkant ska ligga ner i filen."""
    import numpy as np
    import trimesh

    upright = trimesh.creation.box(extents=(120.0, 80.0, 10.0))
    upright.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))

    out = exporter.export_parts(_result_with([upright]), tmp_path / "ut", printer)

    written = trimesh.load(out.part_files[0])
    assert written.extents[2] == pytest.approx(10.0, abs=0.01)


def test_laying_flat_can_be_turned_off(tmp_path, printer):
    import numpy as np
    import trimesh

    upright = trimesh.creation.box(extents=(120.0, 80.0, 10.0))
    upright.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))

    out = exporter.export_parts(
        _result_with([upright]), tmp_path / "ut", printer, lay_flat=False
    )

    written = trimesh.load(out.part_files[0])
    assert written.extents[2] == pytest.approx(80.0, abs=0.01)


def test_loose_bodies_become_separate_files(tmp_path, printer):
    """En del som faller i två lösa klumpar ska bli två filer.

    Ligger de i samma fil ser slicern dem som ett objekt, och då går det
    varken att vända eller placera dem var för sig.
    """
    import trimesh

    a = trimesh.creation.box(extents=(40.0, 30.0, 20.0))
    b = trimesh.creation.box(extents=(20.0, 20.0, 20.0))
    b.apply_translation([200.0, 0.0, 0.0])
    loose = trimesh.util.concatenate([a, b])

    out = exporter.export_parts(_result_with([loose]), tmp_path / "ut", printer)

    assert sorted(p.name for p in out.part_files) == ["part_01a.stl", "part_01b.stl"]
    for path in out.part_files:
        assert trimesh.load(path).is_watertight


def test_a_part_in_one_piece_keeps_its_plain_name(tmp_path, printer):
    """Bokstavssuffixet ska bara dyka upp när det behövs."""
    import trimesh

    out = exporter.export_parts(
        _result_with([trimesh.creation.box(extents=(40.0, 30.0, 20.0))]),
        tmp_path / "ut",
        printer,
    )

    assert [p.name for p in out.part_files] == ["part_01.stl"]


def test_splitting_loose_bodies_can_be_turned_off(tmp_path, printer):
    import trimesh

    a = trimesh.creation.box(extents=(40.0, 30.0, 20.0))
    b = trimesh.creation.box(extents=(20.0, 20.0, 20.0))
    b.apply_translation([200.0, 0.0, 0.0])

    out = exporter.export_parts(
        _result_with([trimesh.util.concatenate([a, b])]),
        tmp_path / "ut",
        printer,
        split_bodies=False,
    )

    assert [p.name for p in out.part_files] == ["part_01.stl"]


def test_touching_bodies_in_a_part_are_not_split(tmp_path, printer):
    """Kroppar som möts är ett föremål och ska förbli en fil."""
    import trimesh

    a = trimesh.creation.box(extents=(20.0, 20.0, 20.0))
    b = trimesh.creation.box(extents=(20.0, 20.0, 20.0))
    b.apply_translation([20.0, 0.0, 0.0])  # yta mot yta

    out = exporter.export_parts(
        _result_with([trimesh.util.concatenate([a, b])]), tmp_path / "ut", printer
    )

    assert [p.name for p in out.part_files] == ["part_01.stl"]


def test_the_cli_takes_a_load_and_moves_the_cut(tmp_path, big_box, capsys):
    """Med en vikt angiven ska snittet flytta sig, och gissningen om
    upphängning ska stå i utskriften så att den går att rätta."""
    from stl_cutter.core import mesh_io

    model = tmp_path / "hylla.stl"
    mesh_io.save_stl(big_box, model)

    assert main(["cut", str(model), "--printer", "Bambu P1S", "--out", str(tmp_path / "a"),
                 "--dry-run", "--no-orient"]) == 0
    plain = capsys.readouterr().out

    assert main(["cut", str(model), "--printer", "Bambu P1S", "--out", str(tmp_path / "b"),
                 "--dry-run", "--no-orient", "--load-kg", "5",
                 "--support", "cantilever", "--load-axis", "x",
                 "--load-end", "low"]) == 0
    loaded = capsys.readouterr().out

    def position(text: str) -> float:
        line = next(rad for rad in text.splitlines() if "Snitt 1:" in rad)
        return float(line.split("=")[1].split("mm")[0])

    assert position(loaded) > position(plain) + 10.0
    assert "Belastning:" in loaded
    assert "Utskriftsinställningar" in loaded
    assert "tumregler" in loaded


def test_the_cli_says_when_the_support_is_only_a_guess(tmp_path, big_box, capsys):
    from stl_cutter.core import mesh_io

    model = tmp_path / "hylla.stl"
    mesh_io.save_stl(big_box, model)

    assert main(["cut", str(model), "--printer", "Bambu P1S", "--out", str(tmp_path / "ut"),
                 "--dry-run", "--load-kg", "5"]) == 0

    out = capsys.readouterr().out
    assert "Gissat:" in out
    assert "--support" in out, "gissningen måste gå att rätta, och det ska stå hur"


def test_export_without_cutting_lays_the_model_flat(tmp_path):
    """En platta som står upp i CAD-filen ska inte komma ut stående.

    Verkligt fall: hyllplattan 270 x 10 x 180 mm exporterades i modellens eget
    läge, och slicern fick den på högkant - stöd överallt och lagren tvärs den
    riktning lasten böjer den.
    """
    import trimesh

    plate = trimesh.creation.box(extents=(270.0, 10.0, 180.0))

    written = exporter.export_model(plate, tmp_path / "platta.stl")

    back = trimesh.load(written[0])
    assert back.extents[2] == pytest.approx(10.0, abs=0.01), "plattan står fortfarande upp"
    assert sorted(round(float(v)) for v in back.extents) == [10, 180, 270]
    assert back.volume == pytest.approx(plate.volume, rel=1e-4)


def test_export_can_keep_the_models_own_orientation(tmp_path):
    """Ska filen tillbaka in i CAD vill man ha den orörd."""
    import trimesh

    plate = trimesh.creation.box(extents=(270.0, 10.0, 180.0))

    written = exporter.export_model(plate, tmp_path / "platta.stl", lay_flat=False)

    assert trimesh.load(written[0]).extents[2] == pytest.approx(180.0, abs=0.01)


def test_the_cli_export_lays_flat_unless_told_otherwise(tmp_path, capsys):
    import trimesh

    from stl_cutter.core import mesh_io

    model = tmp_path / "platta.stl"
    mesh_io.save_stl(trimesh.creation.box(extents=(270.0, 10.0, 180.0)), model)

    assert main(["export", str(model), "--out", str(tmp_path / "ut" / "a.stl")]) == 0
    assert trimesh.load(tmp_path / "ut" / "a.stl").extents[2] == pytest.approx(10.0, abs=0.01)

    assert main(["export", str(model), "--out", str(tmp_path / "ut" / "b.stl"),
                 "--no-lay-flat"]) == 0
    assert trimesh.load(tmp_path / "ut" / "b.stl").extents[2] == pytest.approx(180.0, abs=0.01)


def test_the_cli_writes_a_slicer_profile(tmp_path, capsys):
    import json

    assert main(["profile", "--load-kg", "5", "--base-profile",
                 "0.20mm Standard @FF C5", "--out", str(tmp_path / "prof")]) == 0

    written = list((tmp_path / "prof").glob("*.json"))
    assert len(written) == 1
    data = json.loads(written[0].read_text(encoding="utf-8"))
    assert data["inherits"] == "0.20mm Standard @FF C5"
    assert data["wall_loops"] == "5"

    out = capsys.readouterr().out
    assert "Importera" in out, "utan importinstruktion är filen svår att använda"


def test_the_cli_profile_needs_a_base(tmp_path, capsys):
    code = main(["profile", "--load-kg", "5", "--base-profile", " ",
                 "--out", str(tmp_path / "prof")])

    assert code == 2
    assert "rullgardin" in capsys.readouterr().err


def test_the_cli_runs_a_saved_project(tmp_path, big_box, capsys):
    """Samma projekt ska ge samma delar, utan att någon klickar."""
    from stl_cutter.core import project as project_core
    from stl_cutter.core.printers import get_printer

    saved = project_core.save_project(
        project_core.Project(
            mesh=big_box,
            printer=get_printer("Bambu P1S"),
            source=str(tmp_path / "modell.stl"),
            assembly_intent="demountable",
            cuts=[project_core.ProjectCut(axis=0, position_mm=0.0, joint_type="dovetail")],
            output_dir=str(tmp_path / "ut"),
        ),
        tmp_path / "jobb",
    )

    assert main(["project", str(saved)]) == 0

    written = sorted((tmp_path / "ut").glob("part_*.stl"))
    assert len(written) == 2
    out = capsys.readouterr().out
    assert "dovetail" in out and "Kapade i 2 delar" in out


def test_the_cli_can_just_show_the_project(tmp_path, big_box, capsys):
    from stl_cutter.core import project as project_core
    from stl_cutter.core.printers import get_printer

    saved = project_core.save_project(
        project_core.Project(mesh=big_box, printer=get_printer("Bambu P1S")),
        tmp_path / "jobb",
    )

    assert main(["project", str(saved), "--info"]) == 0

    assert not list(tmp_path.glob("part_*.stl")), "--info får inte skriva några delar"


def test_a_broken_project_is_a_friendly_error(tmp_path, capsys):
    assert main(["project", str(tmp_path / "finns-inte.stlcut")]) == 2
    assert "Hittar ingen" in capsys.readouterr().err
