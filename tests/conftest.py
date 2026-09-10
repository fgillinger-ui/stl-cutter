"""Testgeometri genererad i koden - inga binära testfiler i repot."""

from __future__ import annotations

import os

# Qt måste veta att det inte finns någon skärm innan det importeras.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import trimesh

from stl_cutter.core.printers import PrinterProfile


@pytest.fixture
def printer() -> PrinterProfile:
    """En Bambu-liknande profil: 256 mm kub, 5 mm marginal -> 246 mm användbart."""
    return PrinterProfile(name="Test 256", bed_x=256.0, bed_y=256.0, bed_z=256.0, margin_mm=5.0)


@pytest.fixture
def small_box() -> trimesh.Trimesh:
    """Får plats som den är."""
    return trimesh.creation.box(extents=[100.0, 80.0, 60.0])


@pytest.fixture
def big_box() -> trimesh.Trimesh:
    """För stor i en axel."""
    return trimesh.creation.box(extents=[600.0, 200.0, 100.0])


@pytest.fixture
def two_axis_box() -> trimesh.Trimesh:
    """För stor i två axlar."""
    return trimesh.creation.box(extents=[500.0, 400.0, 150.0])


@pytest.fixture
def big_cylinder() -> trimesh.Trimesh:
    return trimesh.creation.cylinder(radius=180.0, height=400.0, sections=64)


@pytest.fixture
def big_torus() -> trimesh.Trimesh:
    return trimesh.creation.torus(major_radius=200.0, minor_radius=50.0)


@pytest.fixture
def long_rod() -> trimesh.Trimesh:
    """Lång stav - snittet blir kompakt och tjockt (laxstjärt eller pinnar)."""
    return trimesh.creation.box(extents=[500.0, 60.0, 60.0])


@pytest.fixture
def thin_plate() -> trimesh.Trimesh:
    """Tunn platta - snittet blir bara 3 mm tjockt (pussel eller ingen fog)."""
    return trimesh.creation.box(extents=[600.0, 300.0, 3.0])


@pytest.fixture
def medium_plate() -> trimesh.Trimesh:
    """6 mm platt snitt - hamnar i pusselintervallet 4-8 mm."""
    return trimesh.creation.box(extents=[600.0, 300.0, 6.0])


@pytest.fixture
def big_sphere() -> trimesh.Trimesh:
    """Stort klot - runt snitt, ska ge pinnar."""
    return trimesh.creation.icosphere(subdivisions=4, radius=200.0)


@pytest.fixture
def necked_bar() -> trimesh.Trimesh:
    """Stav med ett 3 mm tunt midjeparti exakt där det jämnt fördelade snittet
    skulle hamna. Planeraren ska flytta snittet därifrån."""
    bar = trimesh.creation.box(extents=[500.0, 60.0, 60.0])
    neck_x = -500.0 / 3.0 + 500.0 / 6.0  # = nominell position för första snittet
    cutters = []
    for sign in (1.0, -1.0):
        block = trimesh.creation.box(extents=[20.0, 30.0, 80.0])
        block.apply_translation([neck_x, sign * 16.5, 0.0])
        cutters.append(block)
    return bar.difference(trimesh.util.concatenate(cutters))
