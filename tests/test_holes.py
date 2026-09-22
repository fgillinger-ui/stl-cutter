"""Borrade hål.

Metod: borra i en låda med kända mått och mät hur mycket material som
försvann. En cylinder har en känd volym, så resultatet går att jämföra mot en
siffra - inte mot en bild.
"""

from __future__ import annotations

import numpy as np
import pytest
import trimesh

from stl_cutter.core.holes import (
    MIN_DIAMETER_MM,
    SCREWS,
    Hole,
    HoleError,
    axis_direction,
    drill,
    hole_solid,
    screw_hole,
    surface_hole,
    with_screw,
)

#: Cylindern är månghörnig, inte perfekt rund - volymen blir någon promille
#: mindre än den matematiska.
FACET_TOLERANCE = 0.01


@pytest.fixture
def plate() -> trimesh.Trimesh:
    """En platta: 60 x 40 x 12 mm, centrerad i origo."""
    return trimesh.creation.box(extents=[60.0, 40.0, 12.0])


def cylinder_volume(diameter: float, length: float) -> float:
    return np.pi * (diameter / 2.0) ** 2 * length


# --------------------------------------------------------------------------
# Genomgående hål
# --------------------------------------------------------------------------


def test_a_through_hole_removes_a_cylinder_of_material(plate):
    hole = Hole(point=(0.0, 0.0, 6.0), direction=(0.0, 0.0, -1.0), diameter_mm=5.0)

    drilled = drill(plate, [hole])

    removed = float(abs(plate.volume) - abs(drilled.volume))
    assert removed == pytest.approx(cylinder_volume(5.0, 12.0), rel=FACET_TOLERANCE)
    assert drilled.is_watertight


def test_a_through_hole_goes_all_the_way_even_from_the_side(plate):
    """Genomgående mäts mot modellens diagonal, inte mot ett gissat djup."""
    hole = Hole(point=(-30.0, 0.0, 0.0), direction=(1.0, 0.0, 0.0), diameter_mm=6.0)

    drilled = drill(plate, [hole])

    removed = float(abs(plate.volume) - abs(drilled.volume))
    assert removed == pytest.approx(cylinder_volume(6.0, 60.0), rel=FACET_TOLERANCE)


def test_a_blind_hole_stops_at_its_depth(plate):
    hole = Hole(
        point=(0.0, 0.0, 6.0), direction=(0.0, 0.0, -1.0), diameter_mm=4.0, depth_mm=5.0
    )

    drilled = drill(plate, [hole])

    removed = float(abs(plate.volume) - abs(drilled.volume))
    assert removed == pytest.approx(cylinder_volume(4.0, 5.0), rel=FACET_TOLERANCE)
    # Botten finns kvar: modellen är fortfarande hel, och tunnare än plattan.
    assert drilled.is_watertight
    assert drilled.extents[2] == pytest.approx(12.0)


def test_several_holes_are_drilled_in_one_go(plate):
    holes = [
        Hole(point=(x, 0.0, 6.0), direction=(0.0, 0.0, -1.0), diameter_mm=4.0)
        for x in (-20.0, 0.0, 20.0)
    ]

    drilled = drill(plate, holes)

    removed = float(abs(plate.volume) - abs(drilled.volume))
    assert removed == pytest.approx(3 * cylinder_volume(4.0, 12.0), rel=FACET_TOLERANCE)
    assert drilled.is_watertight


def test_no_holes_leaves_the_model_alone(plate):
    assert drill(plate, []) is plate


# --------------------------------------------------------------------------
# Skruvhål
# --------------------------------------------------------------------------


def test_a_screw_hole_gets_the_clearance_of_that_screw():
    hole = screw_hole((0.0, 0.0, 0.0), (0.0, 0.0, -1.0), "M4")

    assert hole.diameter_mm == pytest.approx(SCREWS["M4"].clearance_mm)
    assert hole.screw == "M4"
    assert "M4" in hole.describe()


def test_the_countersink_takes_more_material_than_the_shaft_alone(plate):
    plain = Hole(point=(0.0, 0.0, 6.0), direction=(0.0, 0.0, -1.0), diameter_mm=4.5)
    sunk = screw_hole((0.0, 0.0, 6.0), (0.0, 0.0, -1.0), "M4")

    without = float(abs(plate.volume) - abs(drill(plate, [plain]).volume))
    with_head = float(abs(plate.volume) - abs(drill(plate, [sunk]).volume))

    assert with_head > without
    # Konen rymmer skallen: bredden vid ytan är skallens diameter.
    solid = hole_solid(sunk, span_mm=100.0)
    width = float(solid.extents[0])
    assert width == pytest.approx(SCREWS["M4"].head_diameter_mm, abs=1.5)


def test_the_countersink_is_as_deep_as_the_head_needs():
    """90°-kon: djupet följer av skallens och hålets diameter."""
    for name, screw in SCREWS.items():
        expected = (screw.head_diameter_mm - screw.clearance_mm) / 2.0
        assert screw.head_depth_mm == pytest.approx(expected), name


def test_a_counterbore_is_a_flat_pocket(plate):
    hole = screw_hole((0.0, 0.0, 6.0), (0.0, 0.0, -1.0), "M4", head="counterbore")

    drilled = drill(plate, [hole])

    removed = float(abs(plate.volume) - abs(drilled.volume))
    spec = SCREWS["M4"]
    shaft = cylinder_volume(spec.clearance_mm, 12.0)
    pocket = cylinder_volume(spec.socket_diameter_mm, spec.socket_depth_mm)
    # Fickan ersätter axeln på sitt djup, den läggs inte till ovanpå.
    overlap = cylinder_volume(spec.clearance_mm, spec.socket_depth_mm)
    assert removed == pytest.approx(shaft + pocket - overlap, rel=0.02)
    assert drilled.is_watertight


def test_an_unknown_screw_is_refused_by_name():
    with pytest.raises(HoleError, match="M2"):
        screw_hole((0.0, 0.0, 0.0), (0.0, 0.0, -1.0), "M2")


def test_changing_the_screw_changes_the_diameter_too():
    hole = screw_hole((0.0, 0.0, 0.0), (0.0, 0.0, -1.0), "M3")

    bigger = with_screw(hole, "M5")

    assert bigger.diameter_mm == pytest.approx(SCREWS["M5"].clearance_mm)
    assert with_screw(hole, "").screw == ""


# --------------------------------------------------------------------------
# Att peka i vyn
# --------------------------------------------------------------------------


def test_clicking_a_surface_drills_straight_into_it(plate):
    """Hålet borras vinkelrätt in i ytan man pekar på."""
    hole = surface_hole(plate, origin=(5.0, 5.0, 100.0), ray_direction=(0.0, 0.0, -1.0))

    assert hole.point == pytest.approx((5.0, 5.0, 6.0))
    assert hole.unit_direction == pytest.approx((0.0, 0.0, -1.0))


def test_clicking_from_the_side_drills_sideways(plate):
    hole = surface_hole(plate, origin=(-100.0, 0.0, 0.0), ray_direction=(1.0, 0.0, 0.0))

    assert hole.point == pytest.approx((-30.0, 0.0, 0.0))
    assert hole.unit_direction == pytest.approx((1.0, 0.0, 0.0))


def test_clicking_a_screw_hole_uses_the_screws_diameter(plate):
    """Ett M5-hål ska vara 5,5 mm - inte den förvalda diametern."""
    hole = surface_hole(plate, origin=(0.0, 0.0, 100.0), ray_direction=(0.0, 0.0, -1.0), screw="M5")

    assert hole.diameter_mm == pytest.approx(SCREWS["M5"].clearance_mm)


def test_clicking_past_the_model_says_so(plate):
    with pytest.raises(HoleError, match="träffade inte"):
        surface_hole(plate, origin=(500.0, 500.0, 500.0), ray_direction=(0.0, 0.0, -1.0))


# --------------------------------------------------------------------------
# Fel som går att åtgärda
# --------------------------------------------------------------------------


def test_a_hole_that_misses_the_model_is_not_silently_accepted(plate):
    """Ett hål som inte tar bort något material är alltid ett misstag.

    Det sägs per hål, med hålets mått och läge, så att man vet vilket av fem
    som är fel - inte bara att "något" gick fel.
    """
    beside = Hole(point=(500.0, 0.0, 0.0), direction=(0.0, 0.0, -1.0), diameter_mm=5.0)

    # Felet pekar ut vilket hål det gäller, inte bara att något gick fel.
    with pytest.raises(HoleError, match="Hål 1"):
        drill(plate, [beside])


def test_a_hole_pointing_away_from_the_model_is_caught(plate):
    outward = Hole(point=(0.0, 0.0, 6.0), direction=(0.0, 0.0, 1.0), diameter_mm=5.0)

    with pytest.raises(HoleError, match="pekar ut"):
        drill(plate, [outward])


def test_a_hole_without_a_direction_says_what_to_do(plate):
    nowhere = Hole(point=(0.0, 0.0, 6.0), direction=(0.0, 0.0, 0.0))

    with pytest.raises(HoleError, match="riktning"):
        drill(plate, [nowhere])


def test_a_hole_too_small_to_print_is_refused(plate):
    tiny = Hole(point=(0.0, 0.0, 6.0), direction=(0.0, 0.0, -1.0), diameter_mm=0.4)

    with pytest.raises(HoleError, match="för litet"):
        drill(plate, [tiny])
    assert MIN_DIAMETER_MM >= 1.0


def test_a_zero_depth_is_through_not_nothing():
    hole = Hole(point=(0.0, 0.0, 0.0), direction=(0.0, 0.0, -1.0), depth_mm=0.0)

    assert hole.through
    assert "genomgående" in hole.describe()


# --------------------------------------------------------------------------
# Spara och läsa
# --------------------------------------------------------------------------


def test_a_hole_survives_a_roundtrip_through_a_dict():
    hole = screw_hole((1.5, -2.5, 3.0), (0.0, -1.0, 0.0), "M6", depth_mm=8.0)

    back = Hole.from_dict(hole.to_dict())

    assert back.point == pytest.approx(hole.point)
    assert back.direction == pytest.approx(hole.direction)
    assert back.diameter_mm == pytest.approx(hole.diameter_mm)
    assert back.depth_mm == pytest.approx(8.0)
    assert back.screw == "M6"


def test_axis_direction_points_into_the_model():
    assert axis_direction(2) == (0.0, 0.0, -1.0)
    assert axis_direction(0, positive=True) == (1.0, 0.0, 0.0)


# --------------------------------------------------------------------------
# Kommandoraden
# --------------------------------------------------------------------------


def test_the_cli_drills_and_writes_a_new_file(tmp_path):
    from stl_cutter.cli import main
    from stl_cutter.core import mesh_io

    model = tmp_path / "platta.stl"
    mesh_io.save_stl(trimesh.creation.box(extents=[60.0, 40.0, 12.0]), model)
    out = tmp_path / "borrad.stl"

    code = main(
        ["drill", str(model), "--hole", "10,0,6", "--hole=-10,0,6", "--screw", "M4", "--out", str(out)]
    )

    assert code == 0
    drilled = mesh_io.load_mesh(out)
    assert drilled.volume_mm3 < 60.0 * 40.0 * 12.0
    assert drilled.watertight


def test_the_cli_says_what_a_bad_coordinate_should_look_like(tmp_path, capsys):
    from stl_cutter.cli import main
    from stl_cutter.core import mesh_io

    model = tmp_path / "platta.stl"
    mesh_io.save_stl(trimesh.creation.box(extents=[60.0, 40.0, 12.0]), model)

    code = main(["drill", str(model), "--hole", "10,0"])

    assert code == 2
    assert "X,Y,Z" in capsys.readouterr().err


def test_the_cli_asks_for_at_least_one_hole(tmp_path, capsys):
    from stl_cutter.cli import main
    from stl_cutter.core import mesh_io

    model = tmp_path / "platta.stl"
    mesh_io.save_stl(trimesh.creation.box(extents=[60.0, 40.0, 12.0]), model)

    code = main(["drill", str(model)])

    assert code == 2
    assert "--hole" in capsys.readouterr().err
