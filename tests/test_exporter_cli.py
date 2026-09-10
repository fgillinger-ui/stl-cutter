import json

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
