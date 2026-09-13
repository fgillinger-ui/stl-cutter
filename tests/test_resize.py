"""Måttändring (fas 3B).

All testgeometri byggs i koden - inga binära filer i repot. Varje fall
kontrollerar samma tre saker som `core.resize` självt kräver: meshen är hel,
bounding boxen stämmer med målet och volymen ändrades med tvärsnittsarea gånger
delta. Utöver det kontrolleras det som är hela poängen med fasen: att
godstjocklek, hål och hörnradier är exakt desamma efteråt.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
import shapely
import trimesh
from shapely.geometry import box as box2d

from stl_cutter.core import resize as R
from stl_cutter.core.resize import ResizeError, ValidationError


# --------------------------------------------------------------------------
# Testgeometri
# --------------------------------------------------------------------------


def _extrude_along_y(polygon, length: float, y0: float) -> trimesh.Trimesh:
    """Extrudera en (X, Z)-profil längs Y, med början i `y0`."""
    solid = trimesh.creation.extrude_polygon(polygon, height=length)
    # extrude_polygon bygger i XY och extruderar längs Z: vrid så att profilen
    # hamnar i (X, Z) och extruderingen längs Y.
    matrix = np.array(
        [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, y0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
    )
    solid.apply_transform(matrix)
    return solid


def rounded_rect(width: float, height: float, radius: float):
    """Rektangel med rundade hörn, centrerad i origo."""
    rect = box2d(-width / 2.0, -height / 2.0, width / 2.0, height / 2.0)
    return rect.buffer(-radius).buffer(radius, quad_segs=16)


@pytest.fixture
def hollow_box() -> trimesh.Trimesh:
    """Låda 250 x 250 x 200 mm: 4 mm väggar, 10 mm hörnradier, lock i båda ändar.

    Djupet ligger längs Y. Tvärsnittet är konstant mellan locken, så det finns
    ett prismatiskt parti på 242 mm att sträcka i.
    """
    outer = rounded_rect(250.0, 200.0, 10.0)
    inner = outer.buffer(-4.0)
    ring = outer.difference(inner)
    parts = [
        _extrude_along_y(ring, 242.0, -121.0),
        _extrude_along_y(outer, 4.0, -125.0),
        _extrude_along_y(outer, 4.0, 121.0),
    ]
    return trimesh.boolean.union(parts, engine="manifold")


@pytest.fixture
def drilled_box() -> trimesh.Trimesh:
    """Massiv låda 100 x 250 x 40 med Ø8-hål 15 mm från vardera gavel."""
    body = trimesh.creation.box(extents=[100.0, 250.0, 40.0])
    holes = []
    for y in (-110.0, 110.0):
        hole = trimesh.creation.cylinder(radius=4.0, height=60.0, sections=64)
        hole.apply_translation([0.0, y, 0.0])
        holes.append(hole)
    return trimesh.boolean.difference([body, *holes], engine="manifold")


@pytest.fixture
def pipe() -> trimesh.Trimesh:
    """Rör: ytterdiameter 60, innerdiameter 40, 200 mm långt längs Z."""
    return trimesh.creation.annulus(r_min=20.0, r_max=30.0, height=200.0, sections=96)


@pytest.fixture
def sphere() -> trimesh.Trimesh:
    """Klot - inget tvärsnitt är någonsin konstant."""
    return trimesh.creation.icosphere(subdivisions=3, radius=100.0)


@pytest.fixture
def shelf_unit() -> trimesh.Trimesh:
    """Hyllstomme med tre jämnt fördelade hyllplan längs Z."""
    shell = trimesh.boolean.difference(
        [
            trimesh.creation.box(extents=[200.0, 120.0, 300.0]),
            trimesh.creation.box(extents=[184.0, 104.0, 284.0]),
        ],
        engine="manifold",
    )
    shelves = []
    for z in (-70.0, 0.0, 70.0):
        shelf = trimesh.creation.box(extents=[184.0, 104.0, 8.0])
        shelf.apply_translation([0.0, 0.0, z])
        shelves.append(shelf)
    return trimesh.boolean.union([shell, *shelves], engine="manifold")


# --------------------------------------------------------------------------
# Gemensamma kontroller
# --------------------------------------------------------------------------


def check_result(result: R.ResizeResult, axis: int, target_mm: float) -> None:
    """De obligatoriska kontrollerna, en gång till utifrån."""
    mesh = result.mesh
    assert mesh.is_watertight, "resultatet ska vara slutet"
    assert mesh.is_winding_consistent, "normalriktningarna ska vara konsekventa"
    assert mesh.volume > 0
    assert abs(float(mesh.extents[axis]) - target_mm) <= R.BBOX_TOLERANCE_MM

    entry = next(entry for entry in result.axes if entry.axis == axis)
    if entry.mode == "preserve":
        tolerance = max(
            R.VOLUME_TOLERANCE * abs(entry.expected_volume_change_mm3),
            R.VOLUME_ABSOLUTE_FLOOR_MM3,
        )
        assert (
            abs(entry.actual_volume_change_mm3 - entry.expected_volume_change_mm3)
            <= tolerance
        )


def section_at(mesh: trimesh.Trimesh, axis: int, position: float):
    polygon = R.section_polygon(mesh, axis, position)
    assert polygon is not None, f"inget tvärsnitt vid {position} längs axel {axis}"
    return polygon


def same_section(a, b, tol_mm2: float = 1.0) -> bool:
    return a.symmetric_difference(b).area <= tol_mm2


def holes_of(polygon) -> list:
    """Alla inre konturer (hål) i ett tvärsnitt."""
    interiors = []
    for piece in R._polygons(polygon):
        interiors.extend(shapely.geometry.Polygon(ring) for ring in piece.interiors)
    return interiors


# --------------------------------------------------------------------------
# find_prismatic_spans
# --------------------------------------------------------------------------


def test_spans_of_a_plain_box_cover_it_all():
    mesh = trimesh.creation.box(extents=[250.0, 250.0, 200.0])
    spans = R.find_prismatic_spans(mesh, 1)
    assert len(spans) == 1
    assert spans[0].length == pytest.approx(249.0, abs=1.5)
    assert spans[0].section_area == pytest.approx(250.0 * 200.0, rel=1e-6)


def test_spans_are_sorted_longest_first(hollow_box):
    spans = R.find_prismatic_spans(hollow_box, 1)
    lengths = [span.length for span in spans]
    assert lengths == sorted(lengths, reverse=True)
    assert spans[0].length > 230.0  # det ihåliga partiet mellan locken


def test_short_spans_are_ignored(drilled_box):
    spans = R.find_prismatic_spans(drilled_box, 1, min_length_mm=50.0)
    assert spans
    assert all(span.length >= 50.0 for span in spans)


def test_a_hole_does_not_count_as_a_constant_section(drilled_box):
    """Ett hål smalnar av långsamt. Arean ändras knappt men konturen flyttar sig,
    och zonen får inte räknas som prismatisk - då hade hålet deformerats."""
    spans = R.find_prismatic_spans(drilled_box, 1)
    for span in spans:
        assert not (span.start < -110.0 < span.end)
        assert not (span.start < 110.0 < span.end)


def test_sphere_has_no_prismatic_span(sphere):
    assert R.find_prismatic_spans(sphere, 0) == []
    assert R.find_prismatic_spans(sphere, 1) == []
    assert R.find_prismatic_spans(sphere, 2) == []


def test_describe_spans_is_readable(pipe):
    text = R.describe_spans(pipe, 2, R.find_prismatic_spans(pipe, 2))
    assert "Axel Z" in text
    assert "mm" in text


# --------------------------------------------------------------------------
# Förlängning
# --------------------------------------------------------------------------


def test_box_depth_250_to_550_keeps_walls_and_corner_radii(hollow_box):
    before = hollow_box
    result = R.resize_axis(before, 1, 550.0)
    check_result(result, 1, 550.0)
    after = result.mesh

    # Locken och väggarna ligger kvar lika långt in från respektive gavel.
    for offset in (2.0, 10.0, 60.0):
        low_before = section_at(before, 1, float(before.bounds[0][1]) + offset)
        low_after = section_at(after, 1, float(after.bounds[0][1]) + offset)
        assert same_section(low_before, low_after)

        high_before = section_at(before, 1, float(before.bounds[1][1]) - offset)
        high_after = section_at(after, 1, float(after.bounds[1][1]) - offset)
        assert same_section(high_before, high_after)

    # Väggtjockleken: hålrummets mått mot ytterkontens.
    cavity_before = holes_of(section_at(before, 1, 0.0))[0]
    cavity_after = holes_of(section_at(after, 1, 0.0))[0]
    assert same_section(cavity_before, cavity_after)

    # Övriga mått orörda.
    assert float(after.extents[0]) == pytest.approx(float(before.extents[0]), abs=0.01)
    assert float(after.extents[2]) == pytest.approx(float(before.extents[2]), abs=0.01)


def test_holes_keep_diameter_and_distance_to_the_ends(drilled_box):
    before, target = drilled_box, 350.0
    result = R.resize_axis(before, 1, target)
    check_result(result, 1, target)
    after = result.mesh

    def hole_report(mesh):
        section = section_at(mesh, 2, 0.0)  # tvärsnitt vinkelrätt mot hålen
        found = []
        for hole in sorted(holes_of(section), key=lambda h: h.centroid.y):
            minx, miny, maxx, maxy = hole.bounds
            found.append(
                {
                    "diameter": (maxx - minx + maxy - miny) / 2.0,
                    "to_low": hole.centroid.y - float(mesh.bounds[0][1]),
                    "to_high": float(mesh.bounds[1][1]) - hole.centroid.y,
                }
            )
        return found

    holes_before, holes_after = hole_report(before), hole_report(after)
    assert len(holes_before) == 2
    assert len(holes_after) == 2
    # Hålen sitter 15 mm från var sin gavel. Det är avståndet till den egna
    # gaveln som ska vara oförändrat - avståndet dem emellan växer förstås.
    for key, old, new in (
        ("to_low", holes_before[0], holes_after[0]),
        ("to_high", holes_before[1], holes_after[1]),
    ):
        assert new["diameter"] == pytest.approx(old["diameter"], abs=0.05)
        assert new[key] == pytest.approx(old[key], abs=0.05)
        assert new[key] == pytest.approx(15.0, abs=0.1)


def test_pipe_keeps_its_inner_diameter(pipe):
    result = R.resize_axis(pipe, 2, 300.0)
    check_result(result, 2, 300.0)
    after = result.mesh

    def inner_diameter(mesh):
        section = section_at(mesh, 2, float(mesh.bounds[0][2]) + 5.0)
        hole = holes_of(section)[0]
        minx, miny, maxx, maxy = hole.bounds
        return (maxx - minx + maxy - miny) / 2.0

    assert inner_diameter(after) == pytest.approx(inner_diameter(pipe), abs=0.02)
    assert float(after.extents[0]) == pytest.approx(float(pipe.extents[0]), abs=0.02)


# --------------------------------------------------------------------------
# Avkortning
# --------------------------------------------------------------------------


def test_shortening_250_to_180(hollow_box):
    result = R.resize_axis(hollow_box, 1, 180.0)
    check_result(result, 1, 180.0)
    assert result.axes[0].delta_mm == pytest.approx(-70.0)
    assert result.axes[0].actual_volume_change_mm3 < 0

    after = result.mesh
    for offset in (2.0, 10.0):
        assert same_section(
            section_at(hollow_box, 1, float(hollow_box.bounds[0][1]) + offset),
            section_at(after, 1, float(after.bounds[0][1]) + offset),
        )


def test_shortening_more_than_the_zone_holds_is_refused(drilled_box):
    # Zonerna är tillsammans klart kortare än 240 mm att ta bort.
    with pytest.raises(ResizeError) as excinfo:
        R.resize_axis(drilled_box, 1, 10.0, span_selection="distribute")
    assert excinfo.value.code == "not_enough_material"
    assert "korta av" in excinfo.value.message


def test_shortening_falls_back_from_a_too_short_longest_zone(drilled_box):
    """Den längsta zonen räcker; men när den inte gör det ska nästa provas."""
    spans = R.find_prismatic_spans(drilled_box, 1)
    longest = spans[0]
    delta = -(longest.capacity_mm + 5.0)
    target = float(drilled_box.extents[1]) + delta
    result = R.resize_axis(drilled_box, 1, target, span_selection="longest")
    check_result(result, 1, target)
    assert len(result.axes[0].chosen) > 1  # föll tillbaka på att fördela


# --------------------------------------------------------------------------
# Zonval
# --------------------------------------------------------------------------


def test_distribute_spreads_proportionally(shelf_unit):
    result = R.resize_axis(shelf_unit, 2, 400.0, span_selection="distribute")
    check_result(result, 2, 400.0)
    chosen = result.axes[0].chosen
    assert len(chosen) > 1
    ratios = [delta / span.length for span, delta in chosen]
    assert max(ratios) - min(ratios) < 1e-6  # proportionellt mot längden
    assert sum(delta for _, delta in chosen) == pytest.approx(100.0, abs=0.01)


def test_distribute_can_skip_short_zones(shelf_unit):
    """Hyllplanen är egna korta zoner. `min_span_mm` håller dem utanför, så att
    hyllplanen behåller sin tjocklek och bara mellanrummen växer."""
    result = R.resize_axis(
        shelf_unit, 2, 400.0, span_selection="distribute", min_span_mm=30.0
    )
    check_result(result, 2, 400.0)
    assert all(span.length >= 30.0 for span, _ in result.axes[0].chosen)


def test_manual_zone_is_used(hollow_box):
    spans = R.find_prismatic_spans(hollow_box, 1)
    result = R.resize_axis(
        hollow_box, 1, 300.0, span_selection="manual", span_index=0
    )
    check_result(result, 1, 300.0)
    assert result.axes[0].chosen[0][0].start == pytest.approx(spans[0].start)


def test_manual_zone_out_of_range(hollow_box):
    with pytest.raises(ResizeError) as excinfo:
        R.resize_axis(hollow_box, 1, 300.0, span_selection="manual", span_index=99)
    assert excinfo.value.code == "bad_span"


# --------------------------------------------------------------------------
# Fel och reservutvägar
# --------------------------------------------------------------------------


def test_sphere_gives_a_structured_error_in_swedish(sphere):
    with pytest.raises(ResizeError) as excinfo:
        R.resize_axis(sphere, 1, 250.0)
    error = excinfo.value
    assert error.code == "no_prismatic_span"
    assert "konstant tvärsnitt" in error.message
    assert "djupet" in error.message
    assert "godstjocklek" in error.message
    assert "scale" in error.suggestion
    assert error.to_dict()["details"]["axis"] == "Y"


def test_scale_mode_works_but_warns(sphere):
    result = R.resize_axis(sphere, 1, 250.0, mode="scale")
    check_result(result, 1, 250.0)
    assert result.warnings
    assert "ovala" in result.warnings[0]


def test_unknown_mode_is_refused(hollow_box):
    with pytest.raises(ResizeError) as excinfo:
        R.resize_axis(hollow_box, 1, 300.0, mode="stretch")
    assert excinfo.value.code == "bad_mode"


def test_zero_target_is_refused(hollow_box):
    with pytest.raises(ResizeError) as excinfo:
        R.resize_axis(hollow_box, 1, 0.0)
    assert excinfo.value.code == "bad_target"


def test_no_change_returns_the_same_size(hollow_box):
    result = R.resize_axis(hollow_box, 1, float(hollow_box.extents[1]))
    assert result.mesh.extents == pytest.approx(hollow_box.extents)
    assert not result.changed


def test_validation_catches_a_broken_result(monkeypatch, hollow_box):
    """Går något fel i booleanerna ska ingenting levereras."""

    def broken(mesh, axis, span, delta, cut_at):
        out = mesh.copy()
        out.apply_scale([1.0, 2.0, 1.0])
        return out

    monkeypatch.setattr(R, "_stretch_once", broken)
    with pytest.raises(ValidationError):
        R.resize_axis(hollow_box, 1, 300.0)


# --------------------------------------------------------------------------
# Flera axlar och rapport
# --------------------------------------------------------------------------


def test_two_axes_in_sequence(hollow_box):
    result = R.resize(hollow_box, [300.0, 400.0, None])
    assert len(result.axes) == 2
    assert float(result.mesh.extents[0]) == pytest.approx(300.0, abs=R.BBOX_TOLERANCE_MM)
    assert float(result.mesh.extents[1]) == pytest.approx(400.0, abs=R.BBOX_TOLERANCE_MM)
    assert float(result.mesh.extents[2]) == pytest.approx(
        float(hollow_box.extents[2]), abs=R.BBOX_TOLERANCE_MM
    )
    assert result.mesh.is_watertight


def test_resize_needs_three_targets(hollow_box):
    with pytest.raises(ValueError):
        R.resize(hollow_box, [300.0, 400.0])


def test_report_contains_everything_the_user_needs(tmp_path, hollow_box):
    result = R.resize_axis(hollow_box, 1, 400.0)
    path = R.write_resize_report(result, tmp_path, source="modell.stl")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["source"] == "modell.stl"
    assert payload["original_extents_mm"][1] == pytest.approx(250.0, abs=0.1)
    assert payload["target_extents_mm"][1] == pytest.approx(400.0)
    assert payload["result_extents_mm"][1] == pytest.approx(400.0, abs=0.1)

    axis = payload["axes"][0]
    assert axis["axis"] == "Y"
    assert axis["span_selection"] == "longest"
    assert axis["spans_found"]
    assert axis["spans_used"][0]["applied_delta_mm"] == pytest.approx(150.0, abs=0.5)
    assert axis["actual_volume_change_mm3"] != 0.0
    assert "warnings" in axis


# --------------------------------------------------------------------------
# Kommandoraden
# --------------------------------------------------------------------------


def _write(mesh: trimesh.Trimesh, path) -> str:
    mesh.export(path)
    return str(path)


def test_cli_analyze_spans(tmp_path, capsys, hollow_box):
    from stl_cutter.cli import main

    model = _write(hollow_box, tmp_path / "modell.stl")
    assert main(["analyze-spans", model, "--axis", "y"]) == 0
    out = capsys.readouterr().out
    assert "Axel Y" in out
    assert "parti" in out


def test_cli_resize_writes_model_and_report(tmp_path, hollow_box):
    from stl_cutter.cli import main

    model = _write(hollow_box, tmp_path / "modell.stl")
    out = tmp_path / "modell_550.stl"
    assert main(["resize", model, "--y", "550", "--out", str(out)]) == 0
    assert out.exists()

    report = tmp_path / R.REPORT_NAME
    assert report.exists()
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["result_extents_mm"][1] == pytest.approx(550.0, abs=0.1)

    written = trimesh.load(out, process=False)
    assert float(written.extents[1]) == pytest.approx(550.0, abs=0.1)


def test_cli_resize_without_measurements_is_an_error(tmp_path, capsys, hollow_box):
    from stl_cutter.cli import main

    model = _write(hollow_box, tmp_path / "modell.stl")
    assert main(["resize", model]) == 2
    assert "Ange minst ett mått" in capsys.readouterr().err


def test_cli_resize_reports_the_structured_error(tmp_path, capsys, sphere):
    from stl_cutter.cli import main

    model = _write(sphere, tmp_path / "klot.stl")
    assert main(["resize", model, "--y", "250"]) == 3
    err = capsys.readouterr().err
    assert "konstant tvärsnitt" in err
    assert "scale" in err


def test_cli_resize_distribute(tmp_path, shelf_unit):
    from stl_cutter.cli import main

    model = _write(shelf_unit, tmp_path / "hylla.stl")
    out = tmp_path / "hylla_400.stl"
    assert main(["resize", model, "--z", "400", "--distribute", "--out", str(out)]) == 0
    written = trimesh.load(out, process=False)
    assert float(written.extents[2]) == pytest.approx(400.0, abs=0.1)


def test_cli_cut_resizes_before_planning(tmp_path, hollow_box):
    from stl_cutter.cli import main

    model = _write(hollow_box, tmp_path / "modell.stl")
    out = tmp_path / "ut"
    code = main(["cut", model, "--resize-y", "550", "--out", str(out), "--no-joints"])
    assert code == 0
    assert (out / R.REPORT_NAME).exists()

    payload = json.loads((out / "split_report.json").read_text(encoding="utf-8"))
    bounds = payload["plan"]["bounds_mm"]
    depth = max(
        abs(bounds[1][index] - bounds[0][index]) for index in range(3)
    )
    assert depth == pytest.approx(550.0, abs=1.0)
