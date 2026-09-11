"""Poängsättning av kandidatplan i planner."""

import numpy as np

from stl_cutter.core.analysis import SectionAnalysis, analyse_section
from stl_cutter.core.planner import (
    SCORE_CONFIG,
    candidate_positions,
    plan_splits,
    score_candidate,
)


def _analysis(**overrides) -> SectionAnalysis:
    base = dict(
        position_mm=0.0,
        axis=0,
        area_mm2=3600.0,
        perimeter_mm=240.0,
        contour_count=1,
        min_wall_mm=60.0,
        roundness=0.79,
        aspect_ratio=1.0,
        bbox_mm=(60.0, 60.0),
    )
    base.update(overrides)
    return SectionAnalysis(**base)


def _score(analysis, position=0.0, nominal=0.0, slabs=(0.33, 0.33), length=300.0):
    return score_candidate(analysis, position, nominal, slabs, length)


def test_a_good_cut_is_only_penalised_for_offset():
    score = _score(_analysis())
    assert score.feasible
    assert score.total == 0.0
    assert score.penalties == {"offset": 0.0}


def test_thin_wall_is_penalised():
    thin = _score(_analysis(min_wall_mm=1.0, area_mm2=300.0))
    thick = _score(_analysis())

    assert thin.total > thick.total
    assert thin.penalties["thin_wall"] > 0


def test_extra_contours_are_penalised():
    one = _score(_analysis(contour_count=1))
    many = _score(_analysis(contour_count=4))

    assert many.total > one.total
    assert many.penalties["contours"] > 0


def test_tiny_and_huge_areas_are_both_penalised():
    tiny = _score(_analysis(area_mm2=10.0))
    good = _score(_analysis(area_mm2=3600.0))
    huge = _score(_analysis(area_mm2=200000.0))

    assert tiny.total > good.total
    assert huge.total > good.total


def test_a_sliver_part_is_penalised():
    sliver = _score(_analysis(), slabs=(0.005, 0.6))
    even = _score(_analysis(), slabs=(0.33, 0.33))

    assert sliver.total > even.total
    assert sliver.penalties["small_part"] > 0


def test_moving_away_from_nominal_costs_something():
    near = _score(_analysis(), position=1.0, nominal=0.0)
    far = _score(_analysis(), position=40.0, nominal=0.0)

    assert far.total > near.total


def test_empty_section_is_infeasible():
    score = _score(_analysis(empty=True))
    assert not score.feasible
    assert score.total >= 1000.0


def test_candidate_positions_respect_the_window_and_step():
    positions = candidate_positions(100.0, 400.0, window=(0.0, 1000.0))

    assert min(positions) >= 100.0 - 0.15 * 400.0 - 1e-9
    assert max(positions) <= 100.0 + 0.15 * 400.0 + 1e-9
    assert 100.0 in positions
    assert abs((positions[1] - positions[0]) - SCORE_CONFIG["step_mm"]) < 1e-9


def test_candidate_positions_clamped_by_a_narrow_window():
    positions = candidate_positions(100.0, 400.0, window=(98.0, 102.0))
    assert min(positions) >= 98.0
    assert max(positions) <= 102.0


def test_planner_moves_the_cut_away_from_a_thin_neck(necked_bar, printer):
    """Det jämnt fördelade snittet hamnar mitt i ett 3 mm midjeparti.
    Poängsättningen ska flytta snittet till fullt material."""
    plan = plan_splits(necked_bar, printer, auto_orient=False, analyse=True)

    first = plan.cuts[0]
    nominal = first.nominal_position_mm

    naive = analyse_section(necked_bar, [nominal, 0, 0], [1, 0, 0], axis=0)
    assert naive.min_wall_mm < 4.0, "testgeometrin ska ha en tunn midja i nominalläget"

    assert abs(first.plane.position - nominal) > 10.0
    assert first.analysis.min_wall_mm > 4.0
    assert first.recommendation.joint_type != "none"


def test_part_count_is_never_increased_by_the_optimisation(necked_bar, printer):
    optimised = plan_splits(necked_bar, printer, auto_orient=False, analyse=True)
    naive = plan_splits(necked_bar, printer, auto_orient=False, analyse=False)

    assert optimised.part_count == naive.part_count
    assert len(optimised.cuts) == len(naive.cuts)


def test_optimised_parts_still_fit_the_build_volume(necked_bar, printer):
    plan = plan_splits(necked_bar, printer, auto_orient=False, analyse=True)

    for box in plan.part_boxes:
        assert printer.fits(box.size_mm), f"del {box.index} får inte plats: {box.size_mm}"


def test_weights_are_configurable(necked_bar, printer):
    """Nollställd tunnväggsvikt gör att snittet kan ligga kvar i midjan."""
    relaxed = plan_splits(
        necked_bar,
        printer,
        auto_orient=False,
        analyse=True,
        weights={"thin_wall": 0.0, "area": 0.0, "contours": 0.0},
    )
    strict = plan_splits(necked_bar, printer, auto_orient=False, analyse=True)

    nominal = strict.cuts[0].nominal_position_mm
    assert abs(relaxed.cuts[0].plane.position - nominal) < abs(
        strict.cuts[0].plane.position - nominal
    )


def test_plan_carries_analysis_and_recommendations(big_box, printer):
    plan = plan_splits(big_box, printer, auto_orient=False, analyse=True)

    assert plan.analysed
    for cut in plan.cuts:
        assert cut.analysis is not None
        assert cut.score is not None
        assert cut.recommendation is not None
        assert len(cut.alternatives) >= 1
        assert cut.alternatives[0].joint_type == cut.recommendation.joint_type


def test_plan_without_analysis_is_the_phase_one_behaviour(big_box, printer):
    plan = plan_splits(big_box, printer, auto_orient=False, analyse=False)

    assert not plan.analysed
    positions = sorted(c.plane.position for c in plan.cuts)
    lower, upper = plan.bounds[0][0], plan.bounds[1][0]
    expected = [lower + (upper - lower) * i / 3.0 for i in (1, 2)]
    assert np.allclose(positions, expected)


def test_a_cut_that_fills_the_build_plate_is_penalised():
    """Delen måste få växa några millimeter - fogen ska sticka ut."""
    tight = score_candidate(
        _analysis(), 0.0, 0.0, (0.33, 0.33), 300.0, slabs_mm=(245.0, 200.0), usable_mm=246.0
    )
    roomy = score_candidate(
        _analysis(), 0.0, 0.0, (0.33, 0.33), 300.0, slabs_mm=(200.0, 200.0), usable_mm=246.0
    )

    assert tight.total > roomy.total
    assert tight.penalties["joint_room"] > 0
    assert "joint_room" not in roomy.penalties


def test_joint_room_is_off_without_build_volume_data():
    score = score_candidate(_analysis(), 0.0, 0.0, (0.33, 0.33), 300.0)

    assert "joint_room" not in score.penalties


def test_the_planner_leaves_room_for_the_joint(printer):
    """Snitten ska inte läggas så att en del fyller plattan helt."""
    import trimesh

    model = trimesh.creation.box(extents=[620.0, 150.0, 40.0])
    plan = plan_splits(model, printer, auto_orient=False, analyse=True)

    usable = printer.usable[0]
    for box in plan.part_boxes:
        assert box.size_mm[0] <= usable
        assert usable - box.size_mm[0] >= 5.0, "ingen plats kvar för fogen"
