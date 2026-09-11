"""Bilderna på fogtyperna.

Bilderna genereras av tools/render_joints.py från den riktiga foggeometrin.
Testerna kontrollerar att de finns, är giltiga och används där de ska.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from stl_cutter.core.recommender import JOINT_TYPES  # noqa: E402

ASSET_DIR = Path(__file__).resolve().parents[1] / "assets" / "joints"


@pytest.mark.parametrize("joint_type", JOINT_TYPES)
def test_every_joint_type_has_both_variants(joint_type):
    """En bild med rubrik för dokumentationen, en utan för gränssnittet."""
    assert (ASSET_DIR / f"{joint_type}.svg").exists()
    assert (ASSET_DIR / f"{joint_type}-plain.svg").exists()


@pytest.mark.parametrize("joint_type", JOINT_TYPES)
def test_images_are_valid_svg_with_real_geometry(joint_type):
    content = (ASSET_DIR / f"{joint_type}.svg").read_text(encoding="utf-8")

    assert content.startswith("<svg")
    assert content.rstrip().endswith("</svg>")
    # Trianglarna ritas som polygoner - en tom bild vore värdelös.
    # En plan limfog är bara två lådor, så kravet får vara lågt.
    assert content.count("<polygon") >= 8


@pytest.mark.parametrize("joint_type", JOINT_TYPES)
def test_captioned_version_names_the_joint(joint_type):
    content = (ASSET_DIR / f"{joint_type}.svg").read_text(encoding="utf-8")
    plain = (ASSET_DIR / f"{joint_type}-plain.svg").read_text(encoding="utf-8")

    assert "<text" in content, "dokumentationsbilden ska ha rubrik"
    assert "<text" not in plain, "gränssnittsbilden ska vara utan text"


def test_both_parts_are_drawn():
    """Hane och hona ska ha olika färg, annars går fogen inte att förstå."""
    content = (ASSET_DIR / "dovetail.svg").read_text(encoding="utf-8")
    colors = set(re.findall(r'fill="(#[0-9a-f]{6})"', content))

    # Blå toner för del A, orange för del B.
    blue = [c for c in colors if int(c[5:7], 16) > int(c[1:3], 16)]
    orange = [c for c in colors if int(c[1:3], 16) > int(c[5:7], 16)]
    assert blue and orange


def test_the_documentation_shows_every_image():
    text = (Path(__file__).resolve().parents[1] / "docs" / "JOINTS.md").read_text(
        encoding="utf-8"
    )
    for joint_type in JOINT_TYPES:
        assert f"assets/joints/{joint_type}.svg" in text


# --------------------------------------------------------------------------
# Användning i gränssnittet
# --------------------------------------------------------------------------

pytest.importorskip("PySide6", reason="PySide6 krävs för GUI-testerna")

from PySide6.QtWidgets import QApplication  # noqa: E402

from stl_cutter.gui import joint_images  # noqa: E402
from stl_cutter.gui.joint_help import JointHelpDialog  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("joint_type", JOINT_TYPES)
def test_gui_finds_an_image_for_every_joint(app, joint_type):
    picture = joint_images.pixmap(joint_type, width=200)

    assert picture is not None
    assert picture.width() == 200
    assert picture.height() > 0


def test_gui_prefers_the_version_without_text(app):
    assert joint_images.image_path("dovetail").name == "dovetail-plain.svg"
    assert joint_images.image_path("dovetail", plain=False).name == "dovetail.svg"


def test_every_joint_has_a_swedish_description():
    for joint_type in JOINT_TYPES:
        text = joint_images.DESCRIPTIONS[joint_type]
        assert len(text) > 60, f"{joint_type} behöver en riktig förklaring"
        assert text.rstrip().endswith("."), f"{joint_type} saknar avslutande punkt"


def test_missing_image_is_not_an_error(app, monkeypatch):
    """Saknas bilderna ska gränssnittet fungera ändå, bara utan bild."""
    monkeypatch.setattr(joint_images, "_CANDIDATES", (Path("/finns/inte"),))

    assert joint_images.image_path("dovetail") is None
    assert joint_images.pixmap("dovetail") is None


def test_help_dialog_shows_all_joint_types(app):
    labels = {t: t.title() for t in JOINT_TYPES}

    dialog = JointHelpDialog(labels)

    assert dialog.shown == list(JOINT_TYPES)
    assert dialog.windowTitle()
