"""Testgeometri genererad i koden - inga binära testfiler i repot."""

from __future__ import annotations

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
