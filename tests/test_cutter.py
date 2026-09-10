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
