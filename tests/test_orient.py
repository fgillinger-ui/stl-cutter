"""Att vända en kapad del platt inför utskrift.

Testerna mäter det som faktiskt spelar roll vid skrivaren: bygghöjden, att
geometrin är orörd, och att delen står stadigt. Att en transform "ser rätt ut"
säger ingenting - måtten gör det.
"""

from __future__ import annotations

import numpy as np
import pytest
import trimesh

from stl_cutter.core import orient


def flat_slab(width=120.0, depth=80.0, height=10.0) -> trimesh.Trimesh:
    """En platta - uppenbart plattast liggande."""
    return trimesh.creation.box(extents=(width, depth, height))


def comb() -> trimesh.Trimesh:
    """En ram med ribbor, som en hyllsektion.

    Formen som avslöjade det första försöket: ribbornas undersidor är vågräta
    tak, vilket ett överhängsmått felaktigt straffade så hårt att det platta
    läget valdes bort.
    """
    rail = trimesh.creation.box(extents=(120.0, 10.0, 12.0))
    solids = [rail]
    for y in (-30.0, 0.0, 30.0):
        slat = trimesh.creation.box(extents=(120.0, 8.0, 8.0))
        slat.apply_translation([0.0, y, 2.0])
        solids.append(slat)
    out = trimesh.boolean.union(solids, engine="manifold")
    out.merge_vertices()
    return out


def test_a_part_on_edge_is_laid_down():
    """Kärnan: en platta på högkant ska läggas ner."""
    upright = flat_slab()
    upright.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
    assert upright.extents[2] == pytest.approx(80.0, abs=0.01)

    laid, height = orient.lay_flat(upright)

    assert height == pytest.approx(10.0, abs=0.01)
    assert laid.extents[2] == pytest.approx(10.0, abs=0.01)


def test_a_part_that_already_lies_flat_is_left_alone():
    """Ligger delen redan rätt ska den inte vridas i onödan."""
    slab = flat_slab()

    laid, height = orient.lay_flat(slab)

    assert height == pytest.approx(10.0, abs=0.01)
    assert laid.extents == pytest.approx(slab.extents, abs=0.01)


def test_the_geometry_is_untouched():
    """Vändningen får flytta delen, aldrig ändra den."""
    upright = comb()
    upright.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [0, 1, 0]))

    laid, _ = orient.lay_flat(upright)

    assert laid.is_watertight
    assert laid.volume == pytest.approx(upright.volume, rel=1e-9)
    assert sorted(np.round(laid.extents, 4)) == sorted(np.round(upright.extents, 4))


def test_a_ribbed_part_is_laid_flat_not_stood_on_end():
    """Regressionsskydd för det första felaktiga måltalet.

    Ett överhängsmått räknade ribbornas vågräta undersidor som stödbehov och
    ställde delen på högkant för att slippa dem - trots att en skrivare
    bryggar dem utan stöd. Delen ska ligga ner.
    """
    ribbed = comb()
    ribbed.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))

    laid, height = orient.lay_flat(ribbed)

    assert height == pytest.approx(min(ribbed.extents), abs=0.01)


def test_the_part_rests_on_the_plate_and_in_the_first_quadrant():
    """Slicern vill ha delen på z = 0 och med positiva koordinater."""
    slab = flat_slab()
    slab.apply_translation([-500.0, 300.0, 120.0])

    laid, _ = orient.lay_flat(slab)

    assert laid.bounds[0] == pytest.approx([0.0, 0.0, 0.0], abs=0.001)


def test_the_flattest_pose_wins_even_with_a_small_footprint():
    """En kam vilar bara på sina ribbor, men det är ändå rätt läge.

    Den första versionen krävde att anliggningen var en stor andel av den
    bästa möjliga, och gallrade därmed bort just det platta läget.
    """
    ribbed = comb()

    laid, height = orient.lay_flat(ribbed)

    assert height == pytest.approx(min(ribbed.extents), abs=0.01)


def test_an_empty_mesh_is_handled():
    empty = trimesh.Trimesh(vertices=np.zeros((0, 3)), faces=np.zeros((0, 3), dtype=int))

    transform, height = orient.flat_transform(empty)

    assert height == 0.0
    assert transform == pytest.approx(np.eye(4))


def test_the_description_says_what_changed():
    upright = flat_slab()
    upright.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
    laid, _ = orient.lay_flat(upright)

    assert "80.0" in orient.describe_orientation(upright, laid)
    assert "låg redan platt" in orient.describe_orientation(laid, laid)
