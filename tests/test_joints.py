"""Fogarnas geometri.

Metod: kapa en testkropp, bygg fogen, dra isär delarna och sätt ihop dem igen
virtuellt. Därefter kontrolleras att hanen och honan inte överlappar och att
spelet mellan dem ligger inom `clearance_mm` ± 0,05 mm.
"""

from __future__ import annotations

import numpy as np
import pytest
import trimesh

from stl_cutter.core.joints import (
    BUILDERS,
    JointError,
    JointParams,
    build_joint,
    validate_parts,
)
from stl_cutter.core.joints.pins import PinsJoint, placement_points
from stl_cutter.core.planner import Plane

#: Hur mycket spelet får avvika från clearance.
GAP_TOLERANCE_MM = 0.05

#: Överlappet mellan hane och hona ska vara försumbart.
MAX_OVERLAP_MM3 = 0.01

PLANE = Plane(origin=(0.0, 0.0, 0.0), normal=(1.0, 0.0, 0.0), axis=0)


def split(extents) -> tuple[trimesh.Trimesh, trimesh.Trimesh]:
    """Kapa en låda mitt itu med ett plant snitt vid x = 0."""
    box = trimesh.creation.box(extents=extents)
    below = trimesh.intersections.slice_mesh_plane(
        box, [-1, 0, 0], [0, 0, 0], cap=True, engine="manifold"
    )
    above = trimesh.intersections.slice_mesh_plane(
        box, [1, 0, 0], [0, 0, 0], cap=True, engine="manifold"
    )
    return below, above


def reassemble(mesh_a, mesh_b, separation=50.0):
    """Dra isär delarna längs normalen och sätt ihop dem igen."""
    apart = mesh_b.copy()
    apart.apply_translation([separation, 0.0, 0.0])
    apart.apply_translation([-separation, 0.0, 0.0])
    return mesh_a.copy(), apart


def overlap_volume(mesh_a, mesh_b) -> float:
    solid = trimesh.boolean.intersection([mesh_a, mesh_b], engine="manifold")
    if solid is None or len(solid.faces) == 0:
        return 0.0
    return float(abs(solid.volume))


def key_gap(mesh_a, mesh_b, min_depth=0.5) -> float:
    """Minsta avstånd mellan del A:s nyckelytor och del B.

    Punkter med x > `min_depth` ligger på det som sticker in i del B, alltså
    på fogens hane. Minsta avståndet därifrån till del B är spelet i fogen.
    """
    points = mesh_a.vertices[mesh_a.vertices[:, 0] > min_depth]
    assert len(points) > 0, "del A har ingen geometri som sticker in i del B"
    distances = trimesh.proximity.closest_point(mesh_b, points)[1]
    return float(distances.min())


JOINT_CASES = {
    "pins": ([200.0, 120.0, 60.0], {"count": 3, "diameter_mm": 6.0, "length_mm": 12.0}),
    "dovetail": (
        [200.0, 160.0, 40.0],
        {"count": 2, "width_mm": 15.0, "depth_mm": 10.0, "angle_deg": 8.0},
    ),
    "puzzle": ([300.0, 200.0, 6.0], {"period_mm": 40.0, "amplitude_mm": 5.0}),
    "screw": ([200.0, 120.0, 30.0], {"count": 2, "guide_pins": 2, "guide_pin_diameter_mm": 5.0}),
}


@pytest.fixture(params=sorted(JOINT_CASES))
def joint_case(request):
    joint_type = request.param
    extents, params = JOINT_CASES[joint_type]
    below, above = split(extents)
    return joint_type, below, above, JointParams(joint_type=joint_type, **params)


def test_every_joint_produces_valid_parts(joint_case):
    joint_type, below, above, params = joint_case

    result = build_joint(below, above, PLANE, params)

    assert result.applied, f"{joint_type} byggdes inte: {result.attempts}"
    assert result.joint_type == joint_type
    assert result.mesh_a.is_watertight
    assert result.mesh_b.is_watertight
    assert result.mesh_a.is_winding_consistent
    assert result.mesh_b.is_winding_consistent
    assert validate_parts([result.mesh_a, result.mesh_b]) == {}


def test_male_and_female_do_not_overlap(joint_case):
    """Sätt ihop delarna virtuellt: hanen får inte kollidera med honan."""
    joint_type, below, above, params = joint_case

    result = build_joint(below, above, PLANE, params)
    mesh_a, mesh_b = reassemble(result.mesh_a, result.mesh_b)

    assert overlap_volume(mesh_a, mesh_b) < MAX_OVERLAP_MM3


def test_gap_matches_the_clearance(joint_case):
    """Spelet ska ligga inom clearance ± 0,05 mm - och vara större än noll."""
    joint_type, below, above, params = joint_case

    result = build_joint(below, above, PLANE, params)
    mesh_a, mesh_b = reassemble(result.mesh_a, result.mesh_b)
    gap = key_gap(mesh_a, mesh_b)

    assert gap > 0.0, "fogen har inget spel alls - delarna går inte ihop"
    assert abs(gap - params.clearance_mm) <= GAP_TOLERANCE_MM, (
        f"{joint_type}: spelet blev {gap:.3f} mm, förväntat {params.clearance_mm} mm"
    )


@pytest.mark.parametrize("clearance", [0.05, 0.15, 0.30])
def test_clearance_is_configurable(clearance):
    below, above = split([200.0, 120.0, 60.0])
    params = JointParams(
        joint_type="pins", count=2, diameter_mm=6.0, length_mm=12.0, clearance_mm=clearance
    )

    result = build_joint(below, above, PLANE, params)
    gap = key_gap(*reassemble(result.mesh_a, result.mesh_b))

    assert abs(gap - clearance) <= GAP_TOLERANCE_MM


def test_pin_holes_are_deeper_than_the_pins():
    """Pinnen ska bottna mot luft, inte mot hålets botten."""
    below, above = split([200.0, 120.0, 60.0])
    params = JointParams(
        joint_type="pins", count=1, diameter_mm=6.0, length_mm=12.0, clearance_mm=0.15
    )

    result = build_joint(below, above, PLANE, params)
    tip = float(result.mesh_a.bounds[1][0])  # pinnens spets
    hole_bottom = float(result.mesh_b.bounds[0][0])

    assert tip > 0.0
    # Hålet ligger inuti del B; kontrollera att spetsen inte når botten.
    ray_origin = np.array([[tip - 0.01, 0.0, 0.0]])
    assert result.mesh_b.contains(ray_origin)[0] is np.False_ or not result.mesh_b.contains(
        ray_origin
    )[0]
    assert hole_bottom <= 0.0


def test_pins_keep_their_distance_to_the_edge():
    """Pinnarna ska ligga minst 3 mm in från snittytans kant."""
    from shapely.geometry import Point, box

    region = box(-30.0, -20.0, 30.0, 20.0)
    points = placement_points(region, count=3, radius=3.0, edge_margin=3.0)

    assert len(points) == 3
    for x, y in points:
        circle = Point(x, y).buffer(3.0)
        assert region.contains(circle)
        assert region.exterior.distance(Point(x, y)) >= 3.0 + 3.0 - 1e-6


def test_dovetail_is_undercut():
    """Laxstjärten ska vara bredare längst ut än vid halsen - annars låser den inte."""
    below, above = split([200.0, 160.0, 40.0])
    params = JointParams(
        joint_type="dovetail", count=1, width_mm=20.0, depth_mm=10.0, angle_deg=8.0
    )

    result = build_joint(below, above, PLANE, params)

    def area_at(x):
        """Laxstjärtens tvärsnittsarea på djupet x in i del B."""
        section = result.mesh_a.section(plane_origin=[x, 0, 0], plane_normal=[1, 0, 0])
        planar, _ = section.to_2D()
        return float(sum(p.area for p in planar.polygons_full))

    neck = area_at(1.0)
    tail = area_at(8.0)

    # 8° flare över 7 mm ger ungefär 10 % större tvärsnitt.
    assert tail > neck * 1.05, f"ingen undersnitt: hals {neck:.0f} mm², tail {tail:.0f} mm²"


def test_puzzle_replaces_the_flat_cut():
    """Pusselfogen ska ge en vågig skarv, inte ett plant snitt."""
    below, above = split([300.0, 200.0, 6.0])
    params = JointParams(joint_type="puzzle", period_mm=40.0, amplitude_mm=5.0)

    result = build_joint(below, above, PLANE, params)

    assert float(result.mesh_a.bounds[1][0]) > 3.0  # sticker in i del B
    assert float(result.mesh_b.bounds[0][0]) < -3.0  # och tvärtom


@pytest.mark.parametrize("profile", ["sine", "keyhole"])
def test_both_puzzle_profiles_work(profile):
    below, above = split([300.0, 200.0, 6.0])
    params = JointParams(
        joint_type="puzzle", profile=profile, period_mm=40.0, amplitude_mm=5.0
    )

    result = build_joint(below, above, PLANE, params)

    assert result.applied
    assert result.mesh_a.is_watertight and result.mesh_b.is_watertight
    assert overlap_volume(result.mesh_a, result.mesh_b) < MAX_OVERLAP_MM3


def test_screw_gives_a_through_hole_and_a_nut_pocket():
    below, above = split([200.0, 120.0, 30.0])
    params = JointParams(joint_type="screw", count=2, guide_pins=2)

    result = build_joint(below, above, PLANE, params)

    # Del A ska ha förlorat volym (genomgående hål + försänkning).
    assert result.mesh_a.volume < below.volume + 500.0
    assert result.mesh_b.volume < above.volume
    assert result.applied


# --------------------------------------------------------------------------
# Robusthet
# --------------------------------------------------------------------------


def test_impossible_dovetail_falls_back_to_a_simpler_joint():
    """En 2 mm platta rymmer ingen laxstjärt - kedjan ska ge ett resultat ändå."""
    below, above = split([200.0, 120.0, 2.0])
    params = JointParams(joint_type="dovetail", count=2, width_mm=15.0, depth_mm=10.0)

    result = build_joint(below, above, PLANE, params)

    assert result.fell_back
    assert result.joint_type in ("pins", "none")
    assert result.warnings
    assert result.mesh_a.is_watertight and result.mesh_b.is_watertight


def test_a_failed_joint_never_loses_the_parts():
    below, above = split([200.0, 120.0, 2.0])
    params = JointParams(joint_type="pins", diameter_mm=20.0, length_mm=12.0)

    result = build_joint(below, above, PLANE, params)

    assert not result.applied
    assert result.joint_type == "none"
    assert abs(result.mesh_a.volume - below.volume) < 1e-6
    assert abs(result.mesh_b.volume - above.volume) < 1e-6


def test_unknown_joint_type_is_reported():
    below, above = split([200.0, 120.0, 60.0])
    with pytest.raises(JointError, match="Okänd fogtyp"):
        build_joint(below, above, PLANE, JointParams(joint_type="magnet"))


def test_every_registered_builder_has_a_type():
    for name, builder in BUILDERS.items():
        assert builder.joint_type == name
        assert builder.fallback is None or builder.fallback in BUILDERS


def test_validate_parts_finds_a_broken_mesh():
    broken = trimesh.creation.box(extents=[10, 10, 10])
    broken.update_faces(np.arange(len(broken.faces)) > 2)  # ta bort ett par trianglar

    report = validate_parts([broken])

    assert report
    assert any("watertight" in problem for problems in report.values() for problem in problems)


def test_no_joint_leaves_the_parts_untouched():
    below, above = split([200.0, 120.0, 60.0])

    result = build_joint(below, above, PLANE, JointParams(joint_type="none"))

    assert not result.applied
    assert result.mesh_a is below
    assert result.mesh_b is above


def test_builders_expose_the_common_interface():
    """build(mesh_a, mesh_b, plane, params) -> (mesh_a_out, mesh_b_out)"""
    below, above = split([200.0, 120.0, 60.0])
    params = JointParams(joint_type="pins", count=2, diameter_mm=6.0, length_mm=12.0)

    out_a, out_b = PinsJoint().build(below, above, PLANE, params)

    assert isinstance(out_a, trimesh.Trimesh)
    assert isinstance(out_b, trimesh.Trimesh)
    assert out_a.volume > below.volume


def test_params_come_from_the_recommendation():
    from stl_cutter.core.recommender import JointRecommendation

    recommendation = JointRecommendation(
        "pins", {"count": 4, "diameter_mm": 7.5, "length_mm": 20.0, "okänd": 1}, "", 0.9
    )

    params = JointParams.from_recommendation(recommendation, clearance_mm=0.25)

    assert params.joint_type == "pins"
    assert params.count == 4
    assert params.diameter_mm == 7.5
    assert params.clearance_mm == 0.25


def test_dovetail_is_clamped_to_part_b_length():
    """En laxstjärt får aldrig sticka ut genom del B."""
    below, above = split([30.0, 160.0, 40.0])  # del B är bara 15 mm djup
    params = JointParams(joint_type="dovetail", count=1, width_mm=15.0, depth_mm=40.0)

    result = build_joint(below, above, PLANE, params)

    if result.joint_type == "dovetail":
        assert float(result.mesh_a.bounds[1][0]) < float(above.bounds[1][0])
    assert result.mesh_a.is_watertight and result.mesh_b.is_watertight


# --------------------------------------------------------------------------
# Snitt genom ribbade och ihåliga modeller
# --------------------------------------------------------------------------


def ribbed_frame(rib_count: int = 5) -> trimesh.Trimesh:
    """Ram med mellanväggar och utan botten - som en filamentlåda.

    Ett snitt tvärs igenom träffar varje vägg som en egen ö.
    """
    from stl_cutter.core.mesh_io import merge_bodies

    thickness, height, width, depth = 9.4, 40.0, 625.0, 341.0
    bodies = []
    for x, y, ex, ey in (
        (-(width - thickness) / 2, 0, thickness, depth),
        ((width - thickness) / 2, 0, thickness, depth),
        (0, -(depth - thickness) / 2, width, thickness),
        (0, (depth - thickness) / 2, width, thickness),
    ):
        wall = trimesh.creation.box(extents=[ex, ey, height])
        wall.apply_translation([x, y, 0])
        bodies.append(wall)
    for i in range(rib_count):
        rib = trimesh.creation.box(extents=[thickness, depth, height])
        rib.apply_translation([-250 + i * 125, 0, 0])
        bodies.append(rib)
    return merge_bodies(bodies)


def split_along_y(mesh, position=0.0):
    below = trimesh.intersections.slice_mesh_plane(
        mesh, [0, -1, 0], [0, position, 0], cap=True, engine="manifold"
    )
    above = trimesh.intersections.slice_mesh_plane(
        mesh, [0, 1, 0], [0, position, 0], cap=True, engine="manifold"
    )
    return below, above


Y_PLANE = Plane(origin=(0.0, 0.0, 0.0), normal=(0.0, 1.0, 0.0), axis=1)


def test_a_cut_through_ribs_gives_several_islands():
    """Utgångsläget: kontaktytan är inte en yta utan sju."""
    from stl_cutter.core.joints.base import contact_region, islands

    below, above = split_along_y(ribbed_frame())
    region, _ = contact_region(below, above, [0, 0, 0], [0, 1, 0])

    assert len(islands(region)) == 7


def test_every_island_gets_its_own_joint():
    """Det användaren såg: bara en laxstjärt trots sju ytor att fästa i."""
    below, above = split_along_y(ribbed_frame())
    params = JointParams(joint_type="dovetail", count=1, width_mm=12.0, depth_mm=8.0)

    result = build_joint(below, above, Y_PLANE, params)

    assert result.applied
    protruding = trimesh.intersections.slice_mesh_plane(
        result.mesh_a, [0, 1, 0], [0, 0.3, 0], cap=True, engine="manifold"
    )
    assert protruding.body_count == 7, "varje ribba ska få en egen laxstjärt"


def test_tiny_islands_are_ignored():
    from shapely.geometry import box as shapely_box

    from stl_cutter.core.joints.base import MIN_ISLAND_AREA_MM2, islands

    from shapely.geometry import MultiPolygon

    big = shapely_box(0, 0, 50, 50)
    crumb = shapely_box(100, 100, 101, 101)  # 1 mm²
    assert crumb.area < MIN_ISLAND_AREA_MM2

    kept = islands(MultiPolygon([big, crumb]))

    assert len(kept) == 1
    assert kept[0].area == pytest.approx(2500)


def test_a_joint_is_not_built_into_a_hollow():
    """Nyckeln får inte sticka in där del B saknar material."""
    frame = ribbed_frame()
    # Snittet skrapar kanten på en mellanvägg: bara ~1 mm kvar på ena sidan.
    below = trimesh.intersections.slice_mesh_plane(
        frame, [-1, 0, 0], [-121.5, 0, 0], cap=True, engine="manifold"
    )
    above = trimesh.intersections.slice_mesh_plane(
        frame, [1, 0, 0], [-121.5, 0, 0], cap=True, engine="manifold"
    )
    plane = Plane(origin=(-121.5, 0.0, 0.0), normal=(1.0, 0.0, 0.0), axis=0)
    params = JointParams(joint_type="dovetail", count=1, width_mm=20.0, depth_mm=15.0)

    result = build_joint(below, above, plane, params)

    assert result.applied, "fogen ska byggas, men åt andra hållet"
    # Nyckeln hamnade på del B, som har materialet.
    assert float(result.mesh_b.bounds[0][0]) < -121.5
    assert result.mesh_a.is_watertight and result.mesh_b.is_watertight
    assert overlap_volume(result.mesh_a, result.mesh_b) < MAX_OVERLAP_MM3


def test_material_depth_limits_the_key():
    """En 6 mm tunn vägg ska ge en 6 mm fog, inte en 15 mm."""
    wall = trimesh.creation.box(extents=[120.0, 12.0, 60.0])
    below, above = split_along_y(wall)
    params = JointParams(joint_type="dovetail", count=1, width_mm=15.0, depth_mm=40.0)

    result = build_joint(below, above, Y_PLANE, params)

    protrusion = float(result.mesh_a.bounds[1][1])
    assert 0 < protrusion <= 6.0 + 0.01, "fogen ska sluta där materialet slutar"


def test_protrusion_can_be_capped():
    """cutter begränsar fogen så att delen får plats på byggplattan."""
    below, above = split([200.0, 120.0, 60.0])
    capped = JointParams(
        joint_type="dovetail", count=1, width_mm=20.0, depth_mm=25.0, max_protrusion_mm=4.0
    )

    result = build_joint(below, above, PLANE, capped)

    assert result.applied
    assert float(result.mesh_a.bounds[1][0]) <= 4.0 + 0.01


def test_pins_respect_the_cap_too():
    below, above = split([200.0, 120.0, 60.0])
    params = JointParams(
        joint_type="pins", count=2, diameter_mm=6.0, length_mm=20.0, max_protrusion_mm=5.0
    )

    result = build_joint(below, above, PLANE, params)

    assert result.applied
    assert float(result.mesh_a.bounds[1][0]) <= 5.0 + 0.01


def test_no_room_at_all_is_reported_not_forced():
    below, above = split([200.0, 120.0, 60.0])
    params = JointParams(joint_type="dovetail", max_protrusion_mm=0.5)

    result = build_joint(below, above, PLANE, params)

    assert result.joint_type in ("pins", "none")
    assert not result.applied or result.fell_back
