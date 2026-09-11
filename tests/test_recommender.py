from stl_cutter.core.analysis import analyse_section
from stl_cutter.core.recommender import (
    LARGE_AREA_MM2,
    PIN_DIAMETER_MAX_MM,
    recommend_joint,
)


def _section(mesh, axis=0):
    normal = [0.0, 0.0, 0.0]
    normal[axis] = 1.0
    return analyse_section(mesh, [0.0, 0.0, 0.0], normal, axis=axis)


def test_long_rod_gets_a_mechanical_joint(long_rod):
    """Kompakt, tjockt snitt: laxstjärt eller pinnar - aldrig bara lim."""
    best, alternatives = recommend_joint(_section(long_rod), intent="glue")

    assert best.joint_type in ("dovetail", "pins")
    assert best.confidence >= 0.8
    assert "mm" in best.motivation
    assert len(alternatives) == 3
    assert alternatives[0].joint_type == best.joint_type


def test_thin_plate_gets_no_joint(thin_plate):
    """3 mm är för tunt för en fog - plan limfog."""
    best, _ = recommend_joint(_section(thin_plate), intent="glue")

    assert best.joint_type == "none"
    assert "för tunt" in best.motivation


def test_medium_plate_gets_a_puzzle(medium_plate):
    """6 mm och platt snitt hamnar i pusselintervallet."""
    best, _ = recommend_joint(_section(medium_plate), intent="glue")

    assert best.joint_type == "puzzle"
    assert best.params["thickness_mm"] > 0
    assert best.params["amplitude_mm"] > 0


def test_round_section_gets_pins(big_sphere):
    best, _ = recommend_joint(_section(big_sphere), intent="glue")

    assert best.joint_type == "pins"
    assert 2 <= best.params["count"] <= 4
    assert best.params["diameter_mm"] <= PIN_DIAMETER_MAX_MM
    assert best.params["edge_margin_mm"] == 3.0


def test_demountable_thick_section_gets_a_screw(long_rod):
    best, _ = recommend_joint(_section(long_rod), intent="demountable")

    assert best.joint_type == "screw"
    assert best.params["screw"] == "M3"
    assert best.params["guide_pins"] == 2
    assert "tas isär" in best.motivation


def test_demountable_thin_plate_still_gets_no_joint(thin_plate):
    """Även demonterbart måste ge vika för att materialet är för tunt."""
    best, _ = recommend_joint(_section(thin_plate), intent="demountable")
    assert best.joint_type == "none"


def test_large_area_adds_guide_pins():
    """Bred 6 mm-platta: snittytan blir 1000 x 6 mm = 6000 mm²."""
    import trimesh

    wide_plate = trimesh.creation.box(extents=[600.0, 1000.0, 6.0])
    section = _section(wide_plate)
    assert section.area_mm2 > LARGE_AREA_MM2

    best, _ = recommend_joint(section, intent="glue")

    assert best.joint_type == "puzzle"
    assert best.params["guide_pins"] == 2
    assert "styrpinnar" in best.motivation


def test_pin_diameter_is_capped(big_sphere):
    """20 % av 400 mm vore 80 mm - taket är 8 mm."""
    best, _ = recommend_joint(_section(big_sphere), intent="glue")
    assert best.params["diameter_mm"] == PIN_DIAMETER_MAX_MM


def test_clearance_comes_from_the_printer(long_rod, printer):
    best, _ = recommend_joint(_section(long_rod), intent="glue", printer=printer)
    assert best.params["clearance_mm"] == printer.clearance_mm


def test_empty_section_needs_no_joint(long_rod):
    empty = analyse_section(long_rod, [400.0, 0.0, 0.0], [1.0, 0.0, 0.0], axis=0)
    best, _ = recommend_joint(empty)

    assert best.joint_type == "none"
    assert "ingen geometri" in best.motivation


def test_a_thin_flange_does_not_decide_the_joint_for_a_thick_cut(long_rod):
    """Fogen byggs på den största ytan - den ska också bestämma fogtypen."""
    import trimesh

    flange = trimesh.creation.box(extents=[200.0, 60.0, 2.5])
    flange.apply_translation([0.0, 200.0, 0.0])
    both = trimesh.util.concatenate([long_rod, flange])
    section = _section(both)

    best, _ = recommend_joint(section, intent="glue")

    assert section.min_wall_mm < 4.0, "det finns en tunn flik i snittet"
    assert best.joint_type in ("dovetail", "pins"), "men den tjocka delen bär fogen"


def test_a_genuinely_thin_cut_still_gets_no_joint(thin_plate):
    best, _ = recommend_joint(_section(thin_plate), intent="glue")

    assert best.joint_type == "none"
