import numpy as np
import pytest
import trimesh

from stl_cutter.core.planner import (
    best_fit_orientation,
    divisions_for,
    part_count_for,
    plan_splits,
)


def test_small_model_needs_no_cuts(small_box, printer):
    plan = plan_splits(small_box, printer)
    assert plan.part_count == 1
    assert plan.planes == []
    assert not plan.needs_cutting


def test_divisions_use_ceiling_of_usable_size(printer):
    # 246 mm användbart: 600 mm -> 3 delar, 200 -> 1, 100 -> 1
    assert divisions_for((600, 200, 100), printer) == (3, 1, 1)
    assert part_count_for((600, 200, 100), printer) == 3


def test_exactly_fitting_size_is_not_split(printer):
    assert divisions_for(printer.usable, printer) == (1, 1, 1)


def test_two_oversize_axes_give_grid(two_axis_box, printer):
    plan = plan_splits(two_axis_box, printer, auto_orient=False)
    assert plan.divisions == (3, 2, 1)
    assert plan.part_count == 6
    assert len(plan.planes) == 3
    assert sorted(p.axis for p in plan.planes) == [0, 0, 1]


def test_planes_lie_inside_the_bounding_box(big_box, printer):
    plan = plan_splits(big_box, printer, auto_orient=False)
    lower, upper = plan.bounds
    for plane in plan.planes:
        pos = plane.origin[plane.axis]
        assert lower[plane.axis] < pos < upper[plane.axis]


def test_best_fit_orientation_reduces_part_count(printer):
    """En stav som ligger snett ska rätas upp och då kräva färre delar."""
    rod = trimesh.creation.box(extents=[500.0, 60.0, 60.0])
    rod.apply_transform(trimesh.transformations.rotation_matrix(np.radians(45), [0, 0, 1]))

    naive = part_count_for(rod.extents, printer)
    _, name, count = best_fit_orientation(rod, printer)

    assert count < naive
    assert count == 3
    assert name != "original"


def test_plan_is_json_serialisable(big_box, printer):
    import json

    plan = plan_splits(big_box, printer)
    payload = json.loads(json.dumps(plan.to_dict()))
    assert payload["part_count"] == plan.part_count
    assert len(payload["planes"]) == len(plan.planes)


# --------------------------------------------------------------------------
# Manuella snitt
# --------------------------------------------------------------------------


def test_make_cut_analyses_the_requested_position(big_box, printer):
    from stl_cutter.core.planner import make_cut

    cut = make_cut(big_box, axis=0, position=-120.0, index=3, printer=printer)

    assert cut.index == 3
    assert cut.plane.axis == 0
    assert cut.plane.position == pytest.approx(-120.0)
    assert cut.analysis is not None
    assert cut.analysis.position_mm == pytest.approx(-120.0)
    assert cut.recommendation is not None
    assert cut.alternatives


def test_plan_from_cuts_counts_parts_from_the_cuts(big_box, printer):
    from stl_cutter.core.planner import make_cut, plan_from_cuts

    cuts = [
        make_cut(big_box, 0, -150.0, 1, printer),
        make_cut(big_box, 0, 150.0, 2, printer),
        make_cut(big_box, 1, 0.0, 3, printer),
    ]

    plan = plan_from_cuts(big_box, printer, cuts)

    assert plan.divisions == (3, 2, 1)
    assert plan.part_count == 6
    assert len(plan.part_boxes) == 6
    assert plan.orientation_name == "manuell"


def test_plan_from_cuts_sorts_and_renumbers(big_box, printer):
    from stl_cutter.core.planner import make_cut, plan_from_cuts

    cuts = [
        make_cut(big_box, 0, 150.0, 1, printer),
        make_cut(big_box, 0, -150.0, 2, printer),
    ]

    plan = plan_from_cuts(big_box, printer, cuts)

    assert [c.index for c in plan.cuts] == [1, 2]
    assert [round(c.plane.position) for c in plan.cuts] == [-150, 150]


def test_plan_from_cuts_drops_cuts_outside_the_model(big_box, printer):
    from stl_cutter.core.planner import make_cut, plan_from_cuts

    inside = make_cut(big_box, 0, 0.0, 1, printer)
    outside = make_cut(big_box, 0, 9000.0, 2, printer)

    plan = plan_from_cuts(big_box, printer, [inside, outside])

    assert len(plan.cuts) == 1
    assert plan.part_count == 2


def test_plan_from_cuts_with_no_cuts_is_one_part(big_box, printer):
    from stl_cutter.core.planner import plan_from_cuts

    plan = plan_from_cuts(big_box, printer, [])

    assert plan.part_count == 1
    assert not plan.needs_cutting


def test_manual_part_boxes_follow_uneven_cuts(big_box, printer):
    """Egna snitt behöver inte vara jämnt fördelade."""
    from stl_cutter.core.planner import make_cut, plan_from_cuts

    # 600 mm modell, snitt vid -200 och +100: delar på 100, 300 och 200 mm.
    cuts = [make_cut(big_box, 0, -200.0, 1, printer), make_cut(big_box, 0, 100.0, 2, printer)]

    plan = plan_from_cuts(big_box, printer, cuts)

    widths = sorted(round(box.size_mm[0]) for box in plan.part_boxes)
    assert widths == [100, 200, 300]


def test_oriented_mesh_matches_the_plan(big_box, printer):
    import numpy as np

    from stl_cutter.core.planner import oriented_mesh

    plan = plan_splits(big_box, printer, auto_orient=True, analyse=False)
    oriented = oriented_mesh(big_box, plan)

    assert np.allclose(oriented.bounds, plan.bounds, atol=1e-6)
