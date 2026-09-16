"""Flera objekt i samma fil som ändrar mått tillsammans.

Geometrin här är byggd för att likna det verkliga fallet som drev fram
funktionen: en hylla med två stolpar ytterst och en bakplatta med två spår som
stolparna glider ner i. Passar spåren inte mot stolparna efteråt är delarna
oanvändbara, och det är precis det testerna mäter.
"""

from __future__ import annotations

import numpy as np
import pytest
import trimesh

from stl_cutter.core import assembly


def plate_with_grooves(
    width: float = 250.0,
    height: float = 180.0,
    thickness: float = 10.0,
    groove_inset: float = 16.0,
    groove_width: float = 12.0,
) -> trimesh.Trimesh:
    """En platta med ett spår innanför var kant, som två stolpar passar i."""
    plate = trimesh.creation.box(extents=(width, thickness, height))
    cuts = []
    for sign in (-1.0, 1.0):
        groove = trimesh.creation.box(extents=(groove_width, thickness / 2.0, height * 2))
        groove.apply_translation(
            [sign * (width / 2.0 - groove_inset - groove_width / 2.0), thickness / 4.0, 0.0]
        )
        cuts.append(groove)
    out = trimesh.boolean.difference([plate] + cuts, engine="manifold")
    out.merge_vertices()
    return out


def frame_with_posts(
    width: float = 230.0, depth: float = 120.0, height: float = 80.0, post: float = 12.0
) -> trimesh.Trimesh:
    """En bottenplatta med en stolpe ytterst i var ände."""
    base = trimesh.creation.box(extents=(width, depth, 10.0))
    base.apply_translation([0.0, 0.0, 5.0])
    solids = [base]
    for sign in (-1.0, 1.0):
        column = trimesh.creation.box(extents=(post, depth, height))
        column.apply_translation([sign * (width - post) / 2.0, 0.0, height / 2.0])
        solids.append(column)
    out = trimesh.boolean.union(solids, engine="manifold")
    out.merge_vertices()
    return out


def two_objects(gap: float = 60.0) -> trimesh.Trimesh:
    """Hyllan och plattan i samma mesh, men utan att röra varandra."""
    frame = frame_with_posts()
    plate = plate_with_grooves()
    plate.apply_translation([0.0, gap + 100.0, 0.0])
    return trimesh.util.concatenate([frame, plate])


# --------------------------------------------------------------------------
# Att hålla isär objekten
# --------------------------------------------------------------------------


def test_separate_bodies_stay_separate():
    """Två kroppar som ligger isär är två objekt, inte en hopslagen solid."""
    parts = assembly.split_parts(two_objects())

    assert len(parts) == 2
    widths = sorted(round(float(p.extents_mm[0])) for p in parts)
    assert widths == [230, 250]


def test_touching_bodies_are_merged():
    """Kroppar som möts slås ihop - annars blir kanterna non-manifold.

    Det är samma reparation som tidigare orsakade rapporten "141 non-manifold
    edges" i slicern, och den får inte gå förlorad nu när objekt hålls isär.
    """
    a = trimesh.creation.box(extents=(20.0, 20.0, 20.0))
    b = trimesh.creation.box(extents=(20.0, 20.0, 20.0))
    b.apply_translation([10.0, 0.0, 0.0])  # halvvägs in i den första

    parts = assembly.split_parts(trimesh.util.concatenate([a, b]))

    assert len(parts) == 1
    assert parts[0].mesh.is_watertight
    assert assembly.mesh_io.bad_edges(parts[0].mesh) == (0, 0)


def test_bodies_meeting_face_to_face_are_one_object():
    """Noll överlapp men ingen glipa - det är ett föremål, inte två.

    Det är precis det fallet som ger fyra trianglar per kant om kropparna inte
    slås ihop, alltså det slicern rapporterar som non-manifold edges.
    """
    a = trimesh.creation.box(extents=(20.0, 20.0, 20.0))
    b = trimesh.creation.box(extents=(20.0, 20.0, 20.0))
    b.apply_translation([20.0, 0.0, 0.0])  # yta mot yta

    parts = assembly.split_parts(trimesh.util.concatenate([a, b]))

    assert len(parts) == 1
    assert parts[0].mesh.is_watertight
    assert assembly.mesh_io.bad_edges(parts[0].mesh) == (0, 0)


def test_a_single_object_gives_one_part():
    parts = assembly.split_parts(trimesh.creation.box(extents=(10.0, 10.0, 10.0)))

    assert len(parts) == 1
    assert parts[0].name == "Objekt 1"


def test_parts_are_ordered_left_to_right():
    """Namnen ska följa hur objekten ligger, annars blir listan obegriplig."""
    left = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
    right = trimesh.creation.box(extents=(20.0, 10.0, 10.0))
    right.apply_translation([100.0, 0.0, 0.0])

    parts = assembly.split_parts(trimesh.util.concatenate([right, left]))

    assert [round(float(p.extents_mm[0])) for p in parts] == [10, 20]


def test_load_parts_reads_a_file(tmp_path):
    path = tmp_path / "tva.stl"
    two_objects().export(path)

    parts = assembly.load_parts(path)

    assert len(parts) == 2
    assert all(part.mesh.is_watertight for part in parts)


# --------------------------------------------------------------------------
# Att hitta styrningarna
# --------------------------------------------------------------------------


def test_grooves_are_found():
    """Spåren ska hittas som svackor i tvärsnittsarean."""
    plate = plate_with_grooves(width=250.0, groove_inset=16.0, groove_width=12.0)

    spacing = assembly.feature_spacing(plate, 0)

    # Spårens mitt ligger 16 + 6 = 22 mm innanför var kant.
    assert spacing == pytest.approx(250.0 - 2 * 22.0, abs=1.5)


def test_a_part_without_grooves_reports_none():
    """Hyllan möter plattan med sina ändytor och har inga spår att mäta."""
    assert assembly.feature_spacing(frame_with_posts(), 0) is None


# --------------------------------------------------------------------------
# Måttändring i grupp
# --------------------------------------------------------------------------


def test_the_follower_shares_the_delta_not_the_target():
    """Kärnan i hela funktionen.

    Hyllan är 230 och plattan 250. Ska hyllan bli 270 måste plattan bli 290 -
    får plattan också 270 flyttar spåren in och möter inte stolparna.
    """
    parts = assembly.split_parts(two_objects())
    leader = next(i for i, p in enumerate(parts) if round(float(p.extents_mm[0])) == 230)

    result = assembly.resize_together(parts, axis=0, target_mm=270.0, leader=leader)

    widths = sorted(round(float(p.extents_mm[0])) for p in result.parts)
    assert widths == [270, 290]
    assert result.delta_mm == pytest.approx(40.0)


def test_the_grooves_follow_the_posts():
    """Det som faktiskt avgör om delarna passar: flyttade spåren lika mycket
    som stolparna?"""
    parts = assembly.split_parts(two_objects())
    leader = next(i for i, p in enumerate(parts) if round(float(p.extents_mm[0])) == 230)
    follower = 1 - leader

    before = assembly.feature_spacing(parts[follower].mesh, 0)
    result = assembly.resize_together(parts, axis=0, target_mm=270.0, leader=leader)
    after = assembly.feature_spacing(result.parts[follower].mesh, 0)

    assert after - before == pytest.approx(40.0, abs=assembly.ALIGNMENT_TOLERANCE_MM)


def test_every_part_stays_watertight():
    parts = assembly.split_parts(two_objects())

    result = assembly.resize_together(parts, axis=0, target_mm=270.0, leader=0)

    for part in result.parts:
        assert part.mesh.is_watertight, f"{part.name} är inte hel"


def test_a_part_can_be_left_alone():
    """Kryssar man ur ett objekt ska det stå orört kvar."""
    parts = assembly.split_parts(two_objects())
    before = [round(float(p.extents_mm[0])) for p in parts]

    result = assembly.resize_together(
        parts, axis=0, target_mm=parts[0].extents_mm[0] + 20.0, leader=0,
        follow=[True, False],
    )

    assert round(float(result.parts[1].extents_mm[0])) == before[1]
    assert result.entries[1].delta_mm == pytest.approx(0.0)


def test_no_change_touches_nothing():
    parts = assembly.split_parts(two_objects())

    result = assembly.resize_together(
        parts, axis=0, target_mm=float(parts[0].extents_mm[0]), leader=0
    )

    assert result.delta_mm == pytest.approx(0.0)
    for before, after in zip(parts, result.parts):
        assert float(after.extents_mm[0]) == pytest.approx(float(before.extents_mm[0]))


def test_a_shrink_that_eats_a_part_is_refused():
    """Ett tillskott som är större än följaren ska stoppas med ett mått i
    meddelandet, inte sluta i en trasig mesh."""
    parts = assembly.split_parts(two_objects())
    small = min(range(len(parts)), key=lambda i: parts[i].extents_mm[0])

    with pytest.raises(assembly.AssemblyError) as excinfo:
        assembly.resize_together(parts, axis=0, target_mm=5.0, leader=1 - small)

    assert "mm" in str(excinfo.value)


def test_the_report_says_what_happened():
    parts = assembly.split_parts(two_objects())

    result = assembly.resize_together(parts, axis=0, target_mm=270.0, leader=0)
    text = assembly.describe_assembly(result)

    assert "bredden" in text
    assert "ledare" in text and "följer med" in text


def test_a_missing_cross_check_is_said_out_loud():
    """Hyllan har inga spår, så passningen kan inte verifieras automatiskt.
    Det ska stå i klartext i stället för att se ut som ett godkänt svar."""
    parts = assembly.split_parts(two_objects())

    result = assembly.resize_together(parts, axis=0, target_mm=270.0, leader=0)

    assert result.notes, "ingen upplysning om att kontrollen inte gick att köra"
    assert "kunde inte kontrolleras" in result.notes[0]


def test_an_excluded_object_is_not_called_a_follower():
    """Ett urkryssat objekt står stilla och ska inte stå som "följer med".

    Raden läses som ett kvitto på att passningen är omhändertagen. Sa den
    "följer med" om en del som inte rörde sig var det tvärtom mot vad som
    hände.
    """
    parts = assembly.split_parts(two_objects())
    leader = next(i for i, p in enumerate(parts) if round(float(p.extents_mm[0])) == 230)
    follow = [False] * len(parts)

    result = assembly.resize_together(
        parts, axis=0, target_mm=270.0, leader=leader, follow=follow
    )
    text = assembly.describe_assembly(result)

    assert "orörd" in text
    assert "följer med" not in text


def test_a_bad_axis_is_refused():
    parts = assembly.split_parts(two_objects())

    with pytest.raises(ValueError):
        assembly.resize_together(parts, axis=7, target_mm=100.0, leader=0)


def test_a_missing_leader_is_refused():
    parts = assembly.split_parts(two_objects())

    with pytest.raises(IndexError):
        assembly.resize_together(parts, axis=0, target_mm=100.0, leader=9)


def test_the_exported_files_are_whole(tmp_path):
    """Regressionsskydd för ett fel som bara syntes i den skrivna filen.

    En måttändrad platta var hel i minnet men kom tillbaka från STL med
    non-manifold-kanter: måttändringen lämnar hörn på varandra, och STL:s
    32-bitars precision drar ihop dem så att ytor svetsas fel. Slicern
    rapporterade det som non-manifold edges. Kontrollen måste därför ske på
    filen, inte på meshen i minnet.
    """
    from stl_cutter.core import mesh_io

    parts = assembly.split_parts(two_objects())
    leader = next(i for i, p in enumerate(parts) if round(float(p.extents_mm[0])) == 230)
    result = assembly.resize_together(parts, axis=0, target_mm=270.0, leader=leader)

    for index, part in enumerate(result.parts, start=1):
        path = mesh_io.save_stl(part.mesh, tmp_path / f"del_{index:02d}.stl")
        back = trimesh.load(path)
        assert back.is_watertight, f"{part.name} är inte hel i filen"
        assert mesh_io.bad_edges(back) == (0, 0), f"{part.name} har trasiga kanter"
        assert back.volume == pytest.approx(part.mesh.volume, rel=1e-4)
