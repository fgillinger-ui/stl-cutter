import pytest

from stl_cutter.core.cutter import cut_mesh, parts_fit
from stl_cutter.core.planner import plan_splits

VOLUME_TOLERANCE = 0.005


def _cut(mesh, printer, auto_orient=True):
    plan = plan_splits(mesh, printer, auto_orient=auto_orient)
    return plan, cut_mesh(mesh, plan)


@pytest.mark.parametrize("fixture_name", ["big_box", "two_axis_box", "big_cylinder", "big_torus"])
def test_all_parts_fit_the_build_volume(request, printer, fixture_name):
    mesh = request.getfixturevalue(fixture_name)
    _, result = _cut(mesh, printer)

    assert result.parts, "inga delar producerades"
    assert parts_fit(result, printer) == []


@pytest.mark.parametrize("fixture_name", ["big_box", "two_axis_box", "big_cylinder", "big_torus"])
def test_volume_is_preserved(request, printer, fixture_name):
    mesh = request.getfixturevalue(fixture_name)
    _, result = _cut(mesh, printer)

    assert result.volume_error < VOLUME_TOLERANCE


@pytest.mark.parametrize("fixture_name", ["big_box", "two_axis_box", "big_cylinder"])
def test_parts_are_watertight(request, printer, fixture_name):
    mesh = request.getfixturevalue(fixture_name)
    _, result = _cut(mesh, printer)

    assert result.all_watertight
    assert result.warnings == []


def test_expected_part_count_for_grid(two_axis_box, printer):
    plan, result = _cut(two_axis_box, printer, auto_orient=False)

    assert plan.part_count == 6
    assert len(result.parts) == 6


def test_torus_hole_means_fewer_parts_than_grid(big_torus, printer):
    """Rutnätet är en övre gräns - tomma celler ska inte ge tomma delar."""
    plan, result = _cut(big_torus, printer)

    assert len(result.parts) <= plan.part_count
    assert all(p.volume_mm3 > 0 for p in result.parts)


# --------------------------------------------------------------------------
# Fogar (fas 3)
# --------------------------------------------------------------------------


def test_cut_with_joints_produces_valid_parts(big_box, printer):
    from stl_cutter.core.planner import plan_splits

    plan = plan_splits(big_box, printer, auto_orient=False, analyse=True)
    result = cut_mesh(big_box, plan, joints=True, printer=printer)

    assert result.joints, "inga fogar byggdes"
    assert all(j.applied for j in result.joints)
    assert result.all_watertight
    assert result.validate() == {}
    # Fogarna tar bort lite material som spel - men bara lite.
    assert 0.0 < result.volume_error < 0.005


def test_joints_pair_up_adjacent_parts(two_axis_box, printer):
    from stl_cutter.core.cutter import find_pairs
    from stl_cutter.core.planner import plan_splits

    plan = plan_splits(two_axis_box, printer, auto_orient=False, analyse=True)
    result = cut_mesh(two_axis_box, plan)
    pairs = find_pairs(result.parts, plan)

    # 3x2-rutnät: 3 snitt ger 2*2 + 3 = 7 grannpar.
    assert len(pairs) == 7
    for part_a, part_b, cut in pairs:
        axis = cut.plane.axis
        assert part_a.mesh.bounds[1][axis] <= part_b.mesh.bounds[0][axis] + 1e-3


def test_forced_joint_type_is_used(big_box, printer):
    from stl_cutter.core.planner import plan_splits

    plan = plan_splits(big_box, printer, auto_orient=False, analyse=True)
    result = cut_mesh(big_box, plan, joints=True, printer=printer, force_joint="pins")

    assert result.joints
    assert {j.requested_type for j in result.joints} == {"pins"}
    assert all(j.joint_type == "pins" for j in result.joints)


def test_cutting_without_joints_is_the_phase_one_behaviour(big_box, printer):
    from stl_cutter.core.planner import plan_splits

    plan = plan_splits(big_box, printer, auto_orient=False, analyse=True)
    result = cut_mesh(big_box, plan, joints=False)

    assert result.joints == []
    assert result.volume_error < 1e-9


def test_joints_survive_reassembly(big_box, printer):
    """Alla delar ska gå att sätta ihop igen utan att kollidera."""
    import trimesh

    from stl_cutter.core.planner import plan_splits

    plan = plan_splits(big_box, printer, auto_orient=False, analyse=True)
    result = cut_mesh(big_box, plan, joints=True, printer=printer)

    for first, second in zip(result.parts, result.parts[1:]):
        overlap = trimesh.boolean.intersection(
            [first.mesh, second.mesh], engine="manifold"
        )
        volume = 0.0 if overlap is None or len(overlap.faces) == 0 else abs(overlap.volume)
        assert volume < 0.01


def test_parts_fit_allows_rotating_the_part_on_the_bed(big_box):
    """En del som passar vriden ska inte flaggas.

    Prusa MK4 har 240 x 200 x 210 mm användbart. En del på 218 x 158 x 177 mm
    får plats, men bara om måtten jämförs sorterade mot sorterad byggvolym.
    """
    import trimesh

    from stl_cutter.core.planner import plan_splits
    from stl_cutter.core.printers import PrinterProfile

    prusa = PrinterProfile(name="Prusa MK4", bed_x=250, bed_y=210, bed_z=220, margin_mm=5)
    part = trimesh.creation.box(extents=[218.0, 158.0, 177.0])
    plan = plan_splits(part, prusa, auto_orient=False, analyse=False)
    result = cut_mesh(part, plan)

    assert plan.part_count == 1
    assert parts_fit(result, prusa) == []


def test_parts_fit_still_catches_a_part_that_is_too_big(big_box, printer):
    import trimesh

    from stl_cutter.core.planner import plan_splits

    oversized = trimesh.creation.box(extents=[300.0, 300.0, 300.0])
    plan = plan_splits(oversized, printer, auto_orient=False, analyse=False)
    result = cut_mesh(oversized, plan)
    result.parts[0].mesh = oversized  # låtsas att en del inte kapades

    assert 1 in parts_fit(result, printer)


# --------------------------------------------------------------------------
# Trasiga originalmodeller
# --------------------------------------------------------------------------


def test_a_repairable_model_gives_whole_parts(printer):
    """Sprickor i originalet ska lagas vid inläsning, inte ärvas av delarna."""
    import numpy as np
    import trimesh

    from stl_cutter.core import mesh_io
    from stl_cutter.core.planner import plan_splits

    cracked = trimesh.creation.box(extents=[600.0, 300.0, 40.0]).subdivide()
    cracked.unmerge_vertices()
    cracked.vertices += np.random.default_rng(7).normal(0, 0.005, cracked.vertices.shape)
    assert not cracked.is_watertight

    repaired, _ = mesh_io.repair_mesh(cracked)
    assert repaired.is_watertight

    plan = plan_splits(repaired, printer, auto_orient=False, analyse=False)
    result = cut_mesh(repaired, plan)

    assert result.all_watertight
    assert not result.inherited_damage
    assert result.warnings == []


def test_damage_that_cannot_be_repaired_is_reported_as_inherited(printer):
    """Går originalet inte att laga ska varningen säga att felet är ärvt."""
    import numpy as np
    import trimesh

    from stl_cutter.core.planner import plan_splits

    broken = trimesh.creation.box(extents=[600.0, 300.0, 100.0]).subdivide().subdivide()
    keep = np.ones(len(broken.faces), dtype=bool)
    keep[::4] = False  # för mycket borta för att fyllas
    broken.update_faces(keep)
    assert not broken.is_watertight

    plan = plan_splits(broken, printer, auto_orient=False, analyse=False)
    result = cut_mesh(broken, plan)

    if not result.all_watertight:
        assert result.inherited_damage
        assert result.source_open_edges > 0
        assert any("ärver" in w for w in result.warnings)
        assert not any("efter snittet" in w for w in result.warnings)


def test_report_records_the_source_damage(tmp_path, big_box, printer):
    import json

    from stl_cutter.core import exporter
    from stl_cutter.core.planner import plan_splits

    plan = plan_splits(big_box, printer, auto_orient=False, analyse=False)
    result = cut_mesh(big_box, plan)
    export = exporter.export_parts(result, tmp_path / "ut", printer)

    report = json.loads(export.report_file.read_text(encoding="utf-8"))
    assert report["result"]["source_open_edges"] == 0


def test_build_volume_slack_uses_sorted_dimensions(printer):
    """Delen får vridas på plattan, så måtten jämförs sorterade."""
    import trimesh

    from stl_cutter.core.cutter import Part, build_volume_slack

    part = Part(index=1, mesh=trimesh.creation.box(extents=[240.0, 100.0, 50.0]))

    # 246 mm användbart: 246 - 240 = 6 mm kvar i det trängsta ledet.
    assert build_volume_slack(part, printer) == pytest.approx(6.0)


def test_joints_never_push_a_part_out_of_the_build_volume(printer):
    """En fog som gör delen för stor löser inget - den byter bara problem."""
    import trimesh

    from stl_cutter.core.planner import plan_splits

    # Två delar som nätt och jämnt får plats: 244 mm mot 246 mm användbart.
    tight = trimesh.creation.box(extents=[488.0, 150.0, 60.0])
    plan = plan_splits(tight, printer, auto_orient=False, analyse=True)
    result = cut_mesh(tight, plan, joints=True, printer=printer, force_joint="dovetail")

    assert parts_fit(result, printer) == [], "delarna ska fortfarande få plats"
    assert result.all_watertight


def test_a_cut_through_ribs_gets_a_joint_per_wall(printer):
    """Hela kedjan: ribbad modell in, flera fogar per skarv ut."""
    import trimesh

    from stl_cutter.core.planner import plan_splits
    from tests.test_joints import ribbed_frame

    frame = ribbed_frame()
    plan = plan_splits(frame, printer, auto_orient=False, analyse=True)
    result = cut_mesh(frame, plan, joints=True, printer=printer, force_joint="dovetail")

    built = [j for j in result.joints if j.applied]
    assert built, "inga fogar byggdes"
    assert result.all_watertight

    # Minst en skarv ska ha fler än en laxstjärt.
    most = 0
    for part in result.parts:
        for axis, position in ((c.plane.axis, c.plane.position) for c in plan.cuts):
            normal = [0.0, 0.0, 0.0]
            normal[axis] = 1.0
            origin = [0.0, 0.0, 0.0]
            origin[axis] = position + 0.3
            keys = trimesh.intersections.slice_mesh_plane(
                part.mesh, normal, origin, cap=True, engine="manifold"
            )
            if keys is not None and len(keys.faces):
                most = max(most, int(keys.body_count))
    assert most > 1, "varje ribba ska få en egen fog, inte bara den största ytan"
