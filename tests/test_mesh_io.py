import trimesh

from stl_cutter.core import mesh_io


def test_load_binary_stl_roundtrip(tmp_path, small_box):
    path = tmp_path / "box.stl"
    mesh_io.save_stl(small_box, path)

    info = mesh_io.load_mesh(path)
    assert info.watertight
    assert info.volume_mm3 == 100 * 80 * 60
    assert [round(v) for v in info.extents_mm] == [100, 80, 60]
    assert "mm" in info.summary()


def test_load_ascii_stl(tmp_path, small_box):
    path = tmp_path / "box_ascii.stl"
    path.write_text(trimesh.exchange.stl.export_stl_ascii(small_box), encoding="utf-8")

    info = mesh_io.load_mesh(path)
    assert info.watertight
    assert abs(info.volume_mm3 - 100 * 80 * 60) < 1e-3


def test_repair_merges_duplicate_vertices(small_box):
    messy = small_box.copy()
    messy.unmerge_vertices()
    before = len(messy.vertices)

    actions = mesh_io.repair_mesh(messy)

    assert len(messy.vertices) < before
    assert any("vertices" in a for a in actions)


def test_3mf_export_falls_back_to_stl_when_unsupported(tmp_path, small_box, monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("inget 3mf-stöd")

    monkeypatch.setattr(trimesh.exchange.export, "export_scene", boom)
    written = mesh_io.save_3mf(small_box, tmp_path / "box.3mf")

    assert written.suffix == ".stl"
    assert written.exists()


def test_rejects_unknown_format(tmp_path):
    path = tmp_path / "modell.obj"
    path.write_text("v 0 0 0\n", encoding="utf-8")
    try:
        mesh_io.load_mesh(path)
    except ValueError as exc:
        assert ".obj" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("förväntade ValueError")
