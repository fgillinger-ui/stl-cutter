"""Last som flyttar snittet.

Modulen räknar inte ut hur mycket en hylla bär - den avgör var den *inte* ska
kapas. Testerna mäter därför lägen och riktningar, aldrig spänningar.
"""

from __future__ import annotations

import numpy as np
import pytest
import trimesh

from stl_cutter.core import load as load_core
from stl_cutter.core.planner import plan_splits, score_candidate
from stl_cutter.core.printers import PrinterProfile


def beam(length: float = 400.0, width: float = 100.0, height: float = 10.0):
    """En rak balk som bara får plats i skrivaren om den kapas."""
    mesh = trimesh.creation.box(extents=(length, width, height))
    mesh.apply_translation(-mesh.bounds[0])
    return mesh


def small_printer() -> PrinterProfile:
    return PrinterProfile(name="Test", bed_x=256, bed_y=256, bed_z=256, margin_mm=5.0)


# --------------------------------------------------------------------------
# Momentkurvan
# --------------------------------------------------------------------------


def test_a_cantilever_is_worst_at_the_wall():
    case = load_core.LoadCase(mass_kg=5.0, support="cantilever", axis=0, fixed_at_low=True)

    assert load_core.relative_moment(case, 0.0, 0.0, 100.0) == pytest.approx(1.0)
    assert load_core.relative_moment(case, 100.0, 0.0, 100.0) == pytest.approx(0.0)
    assert load_core.relative_moment(case, 50.0, 0.0, 100.0) == pytest.approx(0.25)


def test_turning_the_cantilever_around_turns_the_curve():
    high = load_core.LoadCase(mass_kg=5.0, support="cantilever", axis=0, fixed_at_low=False)

    assert load_core.relative_moment(high, 0.0, 0.0, 100.0) == pytest.approx(0.0)
    assert load_core.relative_moment(high, 100.0, 0.0, 100.0) == pytest.approx(1.0)


def test_a_shelf_on_two_supports_is_worst_in_the_middle():
    case = load_core.LoadCase(mass_kg=5.0, support="both_ends", axis=0)

    assert load_core.relative_moment(case, 50.0, 0.0, 100.0) == pytest.approx(1.0)
    assert load_core.relative_moment(case, 0.0, 0.0, 100.0) == pytest.approx(0.0)
    assert load_core.relative_moment(case, 100.0, 0.0, 100.0) == pytest.approx(0.0)


def test_no_weight_means_no_moment():
    case = load_core.LoadCase(mass_kg=0.0, support="cantilever", axis=0)

    assert not case.active
    assert load_core.relative_moment(case, 10.0, 0.0, 100.0) == 0.0


# --------------------------------------------------------------------------
# Gissningen
# --------------------------------------------------------------------------


def test_the_span_axis_is_the_longest_horizontal_one():
    case = load_core.guess_load_case(beam(length=400.0, width=100.0), 5.0)

    assert case.axis == 0


def test_the_thick_end_is_taken_for_the_wall():
    """Gaveln sitter där det finns mest material - det är hela regeln."""
    shelf = trimesh.creation.box(extents=(300.0, 100.0, 10.0))
    wall = trimesh.creation.box(extents=(20.0, 100.0, 120.0))
    wall.apply_translation([-140.0, 0.0, 55.0])
    mesh = trimesh.boolean.union([shelf, wall], engine="manifold")

    case = load_core.guess_load_case(mesh, 5.0)

    assert case.axis == 0
    assert case.fixed_at_low is True
    assert "tvärsnittet är" in case.guessed_from


def test_a_guess_that_cannot_be_trusted_says_so():
    """Två lika ändar går inte att skilja åt, och det ska stå i klartext i
    stället för att se ut som ett svar."""
    case = load_core.guess_load_case(beam(), 5.0)

    assert "ren gissning" in case.guessed_from


# --------------------------------------------------------------------------
# Lastfallet genom en rotation
# --------------------------------------------------------------------------


def test_the_load_follows_the_model_when_it_is_rotated():
    """Planeraren vrider modellen för bästa passform. Följer inte lastfallet
    med hamnar straffet på fel axel, och snittet flyttas åt fel håll."""
    case = load_core.LoadCase(mass_kg=5.0, support="cantilever", axis=0, fixed_at_low=True)
    # 90° runt Z: X blir Y.
    turn = trimesh.transformations.rotation_matrix(np.pi / 2, [0, 0, 1])

    moved = load_core.transformed_load(case, turn)

    assert moved.axis == 1
    assert moved.fixed_at_low is True


def test_a_mirrored_axis_swaps_which_end_is_the_wall():
    case = load_core.LoadCase(mass_kg=5.0, support="cantilever", axis=0, fixed_at_low=True)
    turn = trimesh.transformations.rotation_matrix(np.pi, [0, 0, 1])

    moved = load_core.transformed_load(case, turn)

    assert moved.axis == 0
    assert moved.fixed_at_low is False


# --------------------------------------------------------------------------
# Poängsättningen
# --------------------------------------------------------------------------


def _score(position: float, load=None):
    from stl_cutter.core.analysis import analyse_section

    mesh = beam()
    plane_origin = [position, 50.0, 5.0]
    analysis = analyse_section(mesh, plane_origin, [1.0, 0.0, 0.0], axis=0)
    return score_candidate(
        analysis,
        position,
        200.0,
        (0.5, 0.5),
        400.0,
        load=load,
        axis=0,
        axis_range=(0.0, 400.0),
    )


def test_without_a_load_the_score_is_unchanged():
    assert "load" not in _score(200.0).penalties


def test_the_moment_shows_up_as_a_penalty():
    case = load_core.LoadCase(mass_kg=5.0, support="cantilever", axis=0, fixed_at_low=True)

    near_wall = _score(100.0, case)
    far_out = _score(300.0, case)

    assert near_wall.penalties["load"] > far_out.penalties["load"]


def test_a_load_along_another_axis_is_ignored():
    """Ett snitt tvärs lasten böjs inte isär av den, och ska inte straffas."""
    case = load_core.LoadCase(mass_kg=5.0, support="cantilever", axis=2, fixed_at_low=True)

    assert "load" not in _score(100.0, case).penalties


# --------------------------------------------------------------------------
# Hela planeringen
# --------------------------------------------------------------------------


def test_the_cut_moves_away_from_the_wall():
    """Det som funktionen finns för: samma modell, samma skrivare, men med en
    känd last hamnar snittet längre ut där böjmomentet är mindre."""
    mesh = beam()
    printer = small_printer()
    case = load_core.LoadCase(mass_kg=5.0, support="cantilever", axis=0, fixed_at_low=True)

    without = plan_splits(mesh, printer, auto_orient=False)
    with_load = plan_splits(mesh, printer, auto_orient=False, load=case)

    assert len(with_load.cuts) == len(without.cuts) == 1
    moved = with_load.cuts[0].plane.position - without.cuts[0].plane.position
    assert moved > 20.0, f"snittet flyttade bara {moved:.1f} mm"
    assert with_load.cuts[0].score.penalties.get("load", 0.0) > 0.0


def test_a_shelf_on_two_supports_is_not_cut_mid_span():
    mesh = beam()
    printer = small_printer()
    case = load_core.LoadCase(mass_kg=5.0, support="both_ends", axis=0)

    plan = plan_splits(mesh, printer, auto_orient=False, load=case)

    assert abs(plan.cuts[0].plane.position - 200.0) > 20.0


def test_the_part_count_is_not_allowed_to_grow():
    """Snittet får flytta, men aldrig så långt att en del inte får plats."""
    mesh = beam()
    printer = small_printer()
    case = load_core.LoadCase(mass_kg=5.0, support="cantilever", axis=0, fixed_at_low=True)

    plan = plan_splits(mesh, printer, auto_orient=False, load=case)

    for box in plan.part_boxes:
        assert box.size_mm[0] <= printer.usable[0] + 1e-6


def test_the_plan_carries_the_load_case():
    mesh = beam()
    case = load_core.LoadCase(mass_kg=5.0, support="cantilever", axis=0, fixed_at_low=True)

    plan = plan_splits(mesh, small_printer(), auto_orient=False, load=case)

    assert plan.load is not None and plan.load.active
    assert plan.to_dict()["load"]["mass_kg"] == pytest.approx(5.0)
    assert "Last:" in plan.describe()


# --------------------------------------------------------------------------
# Utskriftsinställningar
# --------------------------------------------------------------------------


def test_the_settings_say_why():
    case = load_core.LoadCase(mass_kg=5.0, support="cantilever", axis=0)

    settings = load_core.print_advice(case)

    assert settings
    assert all(s.why for s in settings), "en inställning utan skäl är inte användbar"
    assert any("Vägg" in s.name for s in settings)


def test_no_load_means_no_settings():
    assert load_core.print_advice(load_core.LoadCase()) == []
    assert "Ingen last" in load_core.describe_advice(load_core.LoadCase())


def test_the_advice_is_marked_as_rules_of_thumb():
    """Texten får inte kunna läsas som en beräkning - det är just den
    förväxlingen som gör ett lugnande tal farligt."""
    text = load_core.describe_advice(load_core.LoadCase(5.0, "cantilever"))

    assert "tumregler" in text
