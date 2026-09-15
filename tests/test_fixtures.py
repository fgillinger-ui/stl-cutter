"""Regressionsfall byggda på verkliga modeller i `tests/fixtures/`.

Filerna ligger inte i repot. Finns de inte hoppas testerna över - se
`tests/fixtures/README.md`. Poängen är att en modell som en gång gått sönder
ska gå att lägga in och därefter aldrig gå sönder igen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stl_cutter.core import mesh_io
from stl_cutter.core import resize as R

FIXTURES = Path(__file__).parent / "fixtures"

#: Fredriks modell: måttändringen längs Y lade hela tillskottet på ett ställe.
DS_NAS = "ds nas_ds nas_Body1.3mf"


def load_fixture(name: str):
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"{name} saknas i tests/fixtures/ - se README där.")
    return mesh_io.load_mesh(path)


def test_ds_nas_keeps_its_symmetry_and_its_other_measurements():
    """Y 240 → 250 mm får varken bli osymmetriskt eller ändra X och Z.

    Båda felen fanns i samma körning: tillskottet hamnade i ett enda parti, och
    måttsektionen visade ett X-mått som inte stämde med panelen. `resize`
    kontrollerar numera båda sakerna själv och vägrar leverera om något av dem
    slår fel - det här testet ser till att den kontrollen faktiskt körs på den
    modell som avslöjade problemet.
    """
    info = load_fixture(DS_NAS)
    before = tuple(float(value) for value in info.mesh.extents)
    symmetric = R.detect_mirror_symmetry(info.mesh, 1)

    result = R.resize_axis(info.mesh, 1, before[1] + 10.0)
    after = tuple(float(value) for value in result.mesh.extents)

    assert after[1] == pytest.approx(before[1] + 10.0, abs=R.BBOX_TOLERANCE_MM)
    assert after[0] == pytest.approx(before[0], abs=R.BBOX_TOLERANCE_MM)
    assert after[2] == pytest.approx(before[2], abs=R.BBOX_TOLERANCE_MM)
    if symmetric:
        assert R.detect_mirror_symmetry(result.mesh, 1)
    assert result.mesh.is_watertight
