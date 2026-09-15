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



@pytest.fixture
def ladder() -> trimesh.Trimesh:
    """Stege 240 mm längs Y: två sidostycken och sex jämnt fördelade pinnar.

    Pinnarna är runda, så tvärsnittet ändrar sig genom hela pinnen - de blir
    aldrig prismatiska partier och kan därför inte växa. Det som *kan* växa är
    de fem lika stora mellanrummen och de två kortare ändstyckena.
    """
    parts = []
    for x in (-50.0, 50.0):
        rail = trimesh.creation.box(extents=[10.0, 240.0, 20.0])
        rail.apply_translation([x, 0.0, 0.0])
        parts.append(rail)
    for index in range(6):
        y = -120.0 + 20.0 + index * 40.0
        rung = trimesh.creation.cylinder(radius=5.0, height=100.0, sections=48)
        rung.apply_transform(
            trimesh.transformations.rotation_matrix(np.pi / 2.0, [0.0, 1.0, 0.0])
        )
        rung.apply_translation([0.0, y, 0.0])
        parts.append(rung)
    return trimesh.boolean.union(parts, engine="manifold")


@pytest.fixture
def lopsided_ladder() -> trimesh.Trimesh:
    """Samma stege, men med pinnarna medvetet osymmetriskt placerade.

    Mellanrummen är olika långa och modellen är inte spegelsymmetrisk längs Y.
    Den ska inte tvingas till symmetri - men fördelningen ska ändå vara
    proportionell mot mellanrummens längd.
    """
    parts = []
    for x in (-50.0, 50.0):
        rail = trimesh.creation.box(extents=[10.0, 240.0, 20.0])
        rail.apply_translation([x, 0.0, 0.0])
        parts.append(rail)
    for y in (-95.0, -35.0, 20.0, 70.0):
        rung = trimesh.creation.cylinder(radius=5.0, height=100.0, sections=48)
        rung.apply_transform(
            trimesh.transformations.rotation_matrix(np.pi / 2.0, [0.0, 1.0, 0.0])
        )
        rung.apply_translation([0.0, y, 0.0])
        parts.append(rung)
    return trimesh.boolean.union(parts, engine="manifold")


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


def _is_thick(mesh: trimesh.Trimesh, axis: int, position: float, thin_area: float) -> bool:
    polygon = R.section_polygon(mesh, axis, float(position))
    return polygon is not None and polygon.area > thin_area


def _edge(mesh, axis, low, high, thin_area, want_thick_at_high: bool) -> float:
    """Halvera fram övergången mellan tunt och tjockt till 0,005 mm."""
    for _ in range(12):
        middle = (low + high) / 2.0
        if _is_thick(mesh, axis, middle, thin_area) == want_thick_at_high:
            high = middle
        else:
            low = middle
    return (low + high) / 2.0


def rung_positions(
    mesh: trimesh.Trimesh, axis: int = 1, thin_area: float = 450.0, step: float = 0.5
) -> list[tuple[float, float]]:
    """(mitt, tjocklek) för varje pinne, mätt på tvärsnittsarean.

    Bara sidostyckena ger 400 mm²; där en pinne finns är arean större. Måtten
    läses alltså ur geometrin, inte ur det programmet påstår sig ha gjort.
    Grovsökning var halv millimeter, sedan halvering fram till kanten - annars
    tar mätningen längre tid än måttändringen själv.
    """
    low, high = float(mesh.bounds[0][axis]), float(mesh.bounds[1][axis])
    positions = np.arange(low + step / 2.0, high, step)
    thick = [_is_thick(mesh, axis, float(p), thin_area) for p in positions]

    found: list[tuple[float, float]] = []
    start_index = None
    for index, is_thick in enumerate(thick):
        if is_thick and start_index is None:
            start_index = index
        elif start_index is not None and not is_thick:
            begins = _edge(
                mesh, axis, positions[start_index - 1], positions[start_index],
                thin_area, True,
            )
            ends = _edge(
                mesh, axis, positions[index - 1], positions[index], thin_area, False
            )
            found.append(((begins + ends) / 2.0, ends - begins))
            start_index = None
    return found


def gaps_between(rungs: list[tuple[float, float]]) -> list[float]:
    """Mellanrummen mellan pinnarna, kant till kant."""
    return [
        (rungs[index + 1][0] - rungs[index + 1][1] / 2.0)
        - (rungs[index][0] + rungs[index][1] / 2.0)
        for index in range(len(rungs) - 1)
    ]


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
    assert axis["span_selection"] == "auto"
    assert axis["resolved_selection"] == "symmetric-centered"
    assert axis["mirror_symmetric_before"] is True
    assert axis["mirror_symmetric_after"] is True
    assert axis["spans_found"]
    assert axis["spans_used"][0]["applied_delta_mm"] == pytest.approx(150.0, abs=0.5)
    assert axis["spans_used"][0]["cut_at_mm"] == pytest.approx(0.0, abs=0.1)
    assert axis["placement"].startswith("Y: 250,0 → 400,0 mm.")
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


# --------------------------------------------------------------------------
# Spegelsymmetri
# --------------------------------------------------------------------------


def test_a_box_is_mirror_symmetric_along_every_axis():
    box = trimesh.creation.box(extents=[100.0, 240.0, 60.0])
    for axis in (0, 1, 2):
        assert R.detect_mirror_symmetry(box, axis)


def test_the_ladder_is_mirror_symmetric_along_its_length(ladder):
    assert R.detect_mirror_symmetry(ladder, 1)


def test_a_lopsided_model_is_not_called_symmetric(lopsided_ladder):
    assert not R.detect_mirror_symmetry(lopsided_ladder, 1)


def test_a_drilled_box_with_one_hole_is_not_symmetric():
    body = trimesh.creation.box(extents=[100.0, 240.0, 40.0])
    hole = trimesh.creation.cylinder(radius=6.0, height=60.0, sections=48)
    hole.apply_translation([0.0, 90.0, 0.0])
    mesh = trimesh.boolean.difference([body, hole], engine="manifold")
    assert not R.detect_mirror_symmetry(mesh, 1)
    assert R.detect_mirror_symmetry(mesh, 0)


def test_a_slight_asymmetry_is_caught():
    """En halv millimeter räcker - gränsen ligger under vad en skrivare ser."""
    left = trimesh.creation.box(extents=[100.0, 100.0, 40.0])
    left.apply_translation([0.0, -50.0, 0.0])
    right = trimesh.creation.box(extents=[100.0, 100.0, 40.0])
    right.apply_translation([0.0, 50.0, 0.0])
    bump = trimesh.creation.box(extents=[20.0, 20.0, 41.0])
    bump.apply_translation([0.0, 40.0, 0.0])
    mesh = trimesh.boolean.union([left, right, bump], engine="manifold")
    assert not R.detect_mirror_symmetry(mesh, 1)


# --------------------------------------------------------------------------
# auto: var materialet hamnar
# --------------------------------------------------------------------------


def test_auto_is_the_default_selection(hollow_box):
    result = R.resize_axis(hollow_box, 1, 300.0)
    assert result.axes[0].span_selection == "auto"


def test_a_symmetric_box_grows_centred_on_the_mid_plane(hollow_box):
    """Mittplanet ligger i ett prismatiskt parti: hela tillskottet hamnar där."""
    mid = R.mirror_plane(hollow_box, 1)
    result = R.resize_axis(hollow_box, 1, 400.0)
    check_result(result, 1, 400.0)

    entry = result.axes[0]
    assert entry.resolved_selection == "symmetric-centered"
    assert len(entry.insertions) == 1
    assert entry.insertions[0].cut_at == pytest.approx(mid, abs=0.1)
    assert entry.insertions[0].delta == pytest.approx(150.0, abs=0.1)
    assert entry.symmetric_before and entry.symmetric_after


def test_the_ladder_keeps_every_gap_equal_when_lengthened(ladder):
    """240 → 250 mm: tillskottet delas lika på de fem mellanrummen."""
    before = rung_positions(ladder)
    assert len(before) == 6

    result = R.resize_axis(ladder, 1, 250.0)
    check_result(result, 1, 250.0)

    after = rung_positions(result.mesh)
    assert len(after) == 6

    gaps = gaps_between(after)
    assert max(gaps) - min(gaps) <= 0.1, gaps

    for was, now in zip(before, after):
        assert now[1] == pytest.approx(was[1], abs=0.1), "pinnen ska inte bli tjockare"

    assert R.detect_mirror_symmetry(result.mesh, 1)
    assert result.axes[0].symmetric_after


def test_the_ladder_keeps_every_gap_equal_when_shortened(ladder):
    """240 → 210 mm: samma sak baklänges."""
    before = rung_positions(ladder)
    result = R.resize_axis(ladder, 1, 210.0)
    check_result(result, 1, 210.0)

    after = rung_positions(result.mesh)
    assert len(after) == 6

    gaps = gaps_between(after)
    assert max(gaps) - min(gaps) <= 0.1, gaps

    for was, now in zip(before, after):
        assert now[1] == pytest.approx(was[1], abs=0.1)

    assert R.detect_mirror_symmetry(result.mesh, 1)


def test_the_ladder_puts_the_material_where_the_log_says(ladder):
    result = R.resize_axis(ladder, 1, 250.0)
    entry = result.axes[0]
    assert len(entry.insertions) == 5
    assert all(item.delta == pytest.approx(2.0, abs=0.01) for item in entry.insertions)
    # Insättningarna utförs uppifrån och ner, så att de partier som ännu inte
    # behandlats behåller sina originalkoordinater.
    positions = [item.cut_at for item in entry.insertions]
    assert positions == sorted(positions, reverse=True)
    # ... och de ligger symmetriskt kring mittplanet.
    mirrored = sorted(-value for value in positions)
    assert mirrored == pytest.approx(sorted(positions), abs=0.1)


def test_a_lopsided_model_is_not_forced_into_symmetry(lopsided_ladder):
    """Fördelningen ska vara proportionell, men symmetri ska inte uppfinnas."""
    result = R.resize_axis(lopsided_ladder, 1, 260.0)
    check_result(result, 1, 260.0)

    entry = result.axes[0]
    assert entry.symmetric_before is False
    assert entry.symmetric_after is False
    assert entry.resolved_selection == "distribute"
    assert len(entry.insertions) > 1

    # Proportionellt: andel av delta = andel av partiernas sammanlagda längd.
    total = sum(item.span.length for item in entry.insertions)
    for item in entry.insertions:
        assert item.delta == pytest.approx(20.0 * item.span.length / total, abs=0.05)

    # Modellen var osymmetrisk och ska förbli det - ingen symmetri uppfanns.
    assert not R.detect_mirror_symmetry(result.mesh, 1)


def test_longest_is_still_available_as_a_deliberate_choice(ladder):
    """`longest` finns kvar i menyn - men bara som ett medvetet val."""
    result = R.resize_axis(ladder, 1, 250.0, span_selection="longest", validate=False)
    entry = result.axes[0]
    assert entry.resolved_selection == "longest"
    assert len(entry.insertions) == 1
    assert entry.insertions[0].delta == pytest.approx(10.0, abs=0.01)


def test_symmetry_is_checked_even_for_longest(ladder):
    """Ett osymmetriskt resultat på en symmetrisk modell levereras inte."""
    with pytest.raises(ValidationError) as error:
        R.resize_axis(ladder, 1, 250.0, span_selection="longest")
    assert error.value.code == "symmetry_lost"
    assert "spegelsymmetrisk" in error.value.message


def test_a_side_effect_on_another_axis_is_refused(monkeypatch, hollow_box):
    """Ett mellanstycke som skjuter ut i sidled ska fällas, inte levereras."""
    original = R._filler

    def fat_filler(polygon, axis, low, high):
        solid = original(polygon, axis, low, high)
        return solid.union(trimesh.creation.box(extents=[400.0, high - low, 10.0]))

    monkeypatch.setattr(R, "_filler", fat_filler)
    with pytest.raises(ValidationError) as error:
        R.resize_axis(hollow_box, 1, 300.0)
    assert error.value.code in {"side_effect", "volume_mismatch"}


# --------------------------------------------------------------------------
# Snittplanet
# --------------------------------------------------------------------------


def test_the_cut_plane_keeps_its_distance_to_the_ends_of_the_span():
    span = R.PrismaticSpan(axis=1, start=0.0, end=10.0, section_area=100.0)
    for candidate in R._cut_candidates(span, delta=5.0, preferred=9.9):
        assert R.SPAN_END_MARGIN_MM - 1e-9 <= candidate <= 10.0 - R.SPAN_END_MARGIN_MM


def test_the_cut_plane_leaves_room_for_the_removed_piece():
    span = R.PrismaticSpan(axis=1, start=0.0, end=20.0, section_area=100.0)
    for candidate in R._cut_candidates(span, delta=-10.0, preferred=0.0):
        assert candidate - 5.0 >= R.SPAN_END_MARGIN_MM - 1e-9
        assert candidate + 5.0 <= 20.0 - R.SPAN_END_MARGIN_MM + 1e-9


def test_the_section_is_taken_at_the_cut_plane_not_at_the_span_start(drilled_box):
    """Mellanstycket extruderas från tvärsnittet vid snittet, inget annat."""
    insertions, _, _ = R.plan_insertions(drilled_box, 1, 350.0)
    for item in insertions:
        at_cut = R.section_polygon(drilled_box, 1, item.cut_at)
        assert item.polygon.symmetric_difference(at_cut).area < 1e-6


def test_an_unstable_cut_plane_is_moved(pipe):
    """Ett läge där tvärsnittet ändrar sig ±0,5 mm ska inte väljas."""
    spans = R.find_prismatic_spans(pipe, 2)
    insertions, _, _ = R.plan_insertions(pipe, 2, 260.0, spans=spans)
    for item in insertions:
        assert R._section_is_stable(pipe, 2, item.cut_at, tol=R.DEFAULT_TOL)


# --------------------------------------------------------------------------
# Loggtexten
# --------------------------------------------------------------------------


def test_the_log_says_exactly_where_the_material_went(ladder):
    result = R.resize_axis(ladder, 1, 250.0)
    text = result.axes[0].placement
    assert text.startswith("Y: 240,0 → 250,0 mm.")
    assert "10,0 mm fördelat på 5 partier" in text
    assert text.count("+2,0 mm vid y=") == 5
    assert " och " in text
    assert text.endswith(".")


def test_the_log_names_a_single_insertion_point(hollow_box):
    text = R.resize_axis(hollow_box, 1, 400.0).axes[0].placement
    assert text == "Y: 250,0 → 400,0 mm. 150,0 mm tillagt vid y=0,0."


def test_the_log_says_when_material_was_removed(hollow_box):
    text = R.resize_axis(hollow_box, 1, 180.0).axes[0].placement
    assert text.startswith("Y: 250,0 → 180,0 mm. 70,0 mm borttaget vid y=")
