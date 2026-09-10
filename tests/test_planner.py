import numpy as np
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
