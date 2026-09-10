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
