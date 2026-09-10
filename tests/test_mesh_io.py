import pytest
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

    repaired, actions = mesh_io.repair_mesh(messy)

    assert len(repaired.vertices) < before
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


# --------------------------------------------------------------------------
# Reparation av trasiga meshar
# --------------------------------------------------------------------------


def _cracked_box(sigma_mm: float, seed: int = 0) -> trimesh.Trimesh:
    """Låda vars trianglar glidit isär en aning, som från en CAD-export."""
    import numpy as np

    mesh = trimesh.creation.box(extents=[100.0, 60.0, 40.0]).subdivide().subdivide()
    mesh.unmerge_vertices()
    mesh.vertices += np.random.default_rng(seed).normal(0, sigma_mm, mesh.vertices.shape)
    return mesh


def _holed_box() -> trimesh.Trimesh:
    """Låda där tre trianglar saknas."""
    import numpy as np

    mesh = trimesh.creation.box(extents=[100.0, 60.0, 40.0]).subdivide().subdivide()
    keep = np.ones(len(mesh.faces), dtype=bool)
    keep[[3, 17, 40]] = False
    mesh.update_faces(keep)
    return mesh


def test_open_edge_count_matches_the_damage():
    assert mesh_io.open_edge_count(trimesh.creation.box(extents=[10, 10, 10])) == 0
    assert mesh_io.open_edge_count(_holed_box()) == 9


def test_repair_welds_hairline_cracks():
    """Sprickor som merge_vertices missar ska svetsas ihop."""
    broken = _cracked_box(2e-4)
    assert not broken.is_watertight

    naive = broken.copy()
    naive.merge_vertices()
    assert not naive.is_watertight, "merge_vertices ensamt ska inte räcka här"

    repaired, actions = mesh_io.repair_mesh(broken)

    assert repaired.is_watertight
    assert mesh_io.open_edge_count(repaired) == 0
    assert any("svetsade" in a for a in actions)
    assert abs(repaired.volume - 100 * 60 * 40) / (100 * 60 * 40) < 0.001


def test_repair_welds_wider_cracks():
    repaired, _ = mesh_io.repair_mesh(_cracked_box(0.02, seed=1))

    assert repaired.is_watertight
    assert abs(repaired.volume - 100 * 60 * 40) / (100 * 60 * 40) < 0.01


def test_repair_fills_holes():
    repaired, actions = mesh_io.repair_mesh(_holed_box())

    assert repaired.is_watertight
    assert any("hål" in a for a in actions)
    assert repaired.volume == pytest.approx(100 * 60 * 40, rel=1e-6)


def test_repair_leaves_a_healthy_mesh_alone():
    good = trimesh.creation.box(extents=[100.0, 60.0, 40.0])
    volume = good.volume

    repaired, actions = mesh_io.repair_mesh(good)

    assert repaired.is_watertight
    assert repaired.volume == pytest.approx(volume)
    assert actions == []


def test_repair_never_collapses_a_thin_part():
    """Svetstoleransen går upp till 0,1 mm - en tunnare detalj får inte plattas."""
    thin = trimesh.creation.box(extents=[100.0, 60.0, 0.06])
    volume = thin.volume

    repaired, _ = mesh_io.repair_mesh(thin)

    assert repaired.volume == pytest.approx(volume, rel=0.01)
    assert repaired.is_watertight


def test_welding_is_a_no_op_without_close_vertices():
    good = trimesh.creation.box(extents=[100.0, 60.0, 40.0])

    welded = mesh_io.weld_vertices(good, 0.001)

    assert welded.volume == pytest.approx(good.volume)


def test_loading_a_broken_file_reports_and_repairs_it(tmp_path):
    path = tmp_path / "trasig.stl"
    mesh_io.save_stl(_cracked_box(2e-4), path)

    info = mesh_io.load_mesh(path)

    assert info.watertight
    assert info.open_edges == 0
    assert info.repairs
    assert "hel" in info.summary()


def test_summary_counts_remaining_open_edges(tmp_path):
    """En modell som inte går att laga ska rapportera hur illa det är."""
    import numpy as np

    mesh = trimesh.creation.box(extents=[100.0, 60.0, 40.0]).subdivide()
    keep = np.ones(len(mesh.faces), dtype=bool)
    keep[::3] = False  # halva ytan borta - går inte att fylla
    mesh.update_faces(keep)
    path = tmp_path / "hopplos.stl"
    mesh_io.save_stl(mesh, path)

    info = mesh_io.load_mesh(path)

    if not info.watertight:
        assert info.open_edges > 0
        assert "öppna kanter" in info.summary()
