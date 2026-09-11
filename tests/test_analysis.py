import math

import pytest
import trimesh

from stl_cutter.core.analysis import (
    THIN_WALL_MM,
    analyse_section,
    largest_inscribed_diameter,
)


def _section(mesh, position=0.0):
    return analyse_section(mesh, [position, 0.0, 0.0], [1.0, 0.0, 0.0], axis=0)


def test_area_and_bbox_of_a_box_section(long_rod):
    result = _section(long_rod)
    assert abs(result.area_mm2 - 60 * 60) < 1e-6
    assert [round(v) for v in result.bbox_mm] == [60, 60]
    assert result.contour_count == 1
    assert not result.empty


def test_min_wall_matches_the_real_thickness(thin_plate):
    result = _section(thin_plate)
    # 3 mm platta: rastreringen ska ligga inom en tiondels mm.
    assert abs(result.min_wall_mm - 3.0) < 0.1
    assert result.cuts_thin_detail
    assert result.min_wall_mm < THIN_WALL_MM


def test_roundness_separates_circle_from_strip(big_sphere, thin_plate):
    circle = _section(big_sphere)
    strip = _section(thin_plate)

    assert circle.roundness > 0.95  # en cirkel har rundhet 1.0
    assert circle.is_round
    assert strip.roundness < 0.1
    assert strip.is_flat


def test_aspect_ratio_of_a_strip(thin_plate):
    result = _section(thin_plate)
    assert abs(result.aspect_ratio - 300.0 / 3.0) < 1.0
    assert result.is_elongated


def test_two_islands_are_counted(long_rod):
    """Ett rör ger två öar i snittet: ytterväggen och hålet räknas som en ö,
    men två separata rör ger två."""
    second = long_rod.copy()
    second.apply_translation([0.0, 200.0, 0.0])
    both = trimesh.util.concatenate([long_rod, second])

    result = _section(both)

    assert result.contour_count == 2
    assert abs(result.area_mm2 - 2 * 60 * 60) < 1e-6


def test_hollow_tube_has_one_contour_with_a_hole():
    tube = trimesh.creation.annulus(r_min=30.0, r_max=40.0, height=200.0)
    result = analyse_section(tube, [0.0, 0.0, 0.0], [0.0, 0.0, 1.0], axis=2)

    assert result.contour_count == 1
    assert abs(result.min_wall_mm - 10.0) < 0.3  # väggtjockleken, inte ytterdiametern
    assert abs(result.area_mm2 - math.pi * (40**2 - 30**2)) / result.area_mm2 < 0.01


def test_section_outside_the_model_is_empty(long_rod):
    result = _section(long_rod, position=400.0)
    assert result.empty
    assert result.area_mm2 == 0.0
    assert "Tomt snitt" in result.describe()


def test_largest_inscribed_diameter_of_a_circle():
    from shapely.geometry import Point

    assert abs(largest_inscribed_diameter(Point(0, 0).buffer(25.0)) - 50.0) < 0.5


def test_the_main_wall_is_the_biggest_island_not_the_thinnest(long_rod):
    """En tunn flik i kanten ska inte avgöra hur hela snittet bedöms."""
    thin = trimesh.creation.box(extents=[200.0, 60.0, 2.5])
    thin.apply_translation([0.0, 200.0, 0.0])
    both = trimesh.util.concatenate([long_rod, thin])

    result = _section(both)

    assert result.contour_count == 2
    assert result.min_wall_mm == pytest.approx(2.5, abs=0.15)
    assert result.main_wall_mm == pytest.approx(60.0, abs=0.3)
    assert result.has_thinner_islands


def test_a_single_island_has_no_thinner_neighbours(long_rod):
    result = _section(long_rod)

    assert result.main_wall_mm == pytest.approx(result.min_wall_mm)
    assert not result.has_thinner_islands
