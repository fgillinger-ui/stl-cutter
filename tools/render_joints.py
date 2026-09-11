#!/usr/bin/env python3
"""Rita exempelbilder på fogtyperna.

Bilderna byggs av samma kod som bygger de riktiga fogarna, så de kan aldrig
visa något annat än vad programmet faktiskt gör. Resultatet blir SVG, som
fungerar både i dokumentationen på GitHub och i det grafiska gränssnittet.

Kör: python tools/render_joints.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import trimesh

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stl_cutter.core.joints import JointParams, build_joint  # noqa: E402
from stl_cutter.core.planner import Plane  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[1] / "assets" / "joints"

WIDTH, HEIGHT = 560, 320
BACKGROUND = "#f4f5f7"

#: Del A (hanen) och del B (honan) får varsin färg.
COLOR_A = np.array([0.30, 0.55, 0.80])
COLOR_B = np.array([0.90, 0.60, 0.25])

#: Riktning ljuset kommer ifrån.
LIGHT = np.array([0.4, -0.6, 0.7])
LIGHT = LIGHT / np.linalg.norm(LIGHT)

#: Hur långt delarna dras isär i bilden.
EXPLODE_MM = 46.0

PLANE = Plane(origin=(0.0, 0.0, 0.0), normal=(1.0, 0.0, 0.0), axis=0)

#: Per fogtyp: mått på testkroppen, fogparametrar, rubrik, och hur bilden ska
#: byggas. `flip` vänder honan ett halvt varv så att urtaget syns; `section`
#: skär bort främre halvan så att hål inuti syns.
CASES = {
    "none": {
        "extents": [150.0, 110.0, 12.0],
        "params": {},
        "title": "Plan limfog",
        "subtitle": "Plana ytor som limmas mot varandra - ingen fog",
    },
    "puzzle": {
        "extents": [170.0, 120.0, 10.0],
        "params": {"period_mm": 36.0, "amplitude_mm": 11.0},
        "title": "Pusselprofil",
        "subtitle": "Vågig skarv genom hela tjockleken som låser i sidled",
        "flip": True,
    },
    "dovetail": {
        "extents": [150.0, 120.0, 40.0],
        "params": {"count": 2, "width_mm": 26.0, "depth_mm": 20.0, "angle_deg": 8.0},
        "title": "Laxstjärt",
        "subtitle": "Bredare längst ut - delarna skjuts ihop i sidled och kan inte dras isär",
        "flip": True,
    },
    "pins": {
        "extents": [150.0, 120.0, 50.0],
        "params": {"count": 3, "diameter_mm": 12.0, "length_mm": 22.0},
        "title": "Styrpinnar",
        "subtitle": "Tappar på ena delen, hål i den andra - tryck ihop och limma",
        "flip": True,
    },
    "dovetail-stop": {
        "joint": "dovetail",
        "extents": [150.0, 120.0, 44.0],
        "params": {
            "count": 2,
            "width_mm": 26.0,
            "depth_mm": 20.0,
            "angle_deg": 8.0,
            "stop_mm": 12.0,
        },
        "title": "Laxstjärt med stoppkant",
        "subtitle": "Spåret är stängt i botten - delen glider in och tar emot mot material",
        "flip": True,
    },
    "screw": {
        # Liten kropp: en M3-skruv i en 150 mm-låda blir bara en prick. Delarna
        # visas nästan hopsatta, så att hela skruvens väg syns i ett svep:
        # försänkning, genomgående hål, mutterficka.
        "extents": [56.0, 46.0, 22.0],
        "params": {"count": 1, "guide_pins": 0},
        "title": "Skruv med mutter",
        "subtitle": "Genomskärning: försänkning, genomgående hål och sexkantsficka för muttern",
        "section": True,
        "explode": 7.0,
    },
}


def split_box(extents) -> tuple[trimesh.Trimesh, trimesh.Trimesh]:
    box = trimesh.creation.box(extents=extents)
    below = trimesh.intersections.slice_mesh_plane(
        box, [-1, 0, 0], [0, 0, 0], cap=True, engine="manifold"
    )
    above = trimesh.intersections.slice_mesh_plane(
        box, [1, 0, 0], [0, 0, 0], cap=True, engine="manifold"
    )
    return below, above


def view_matrix(direction=(1.0, -0.85, 0.55)) -> np.ndarray:
    """Kamera som tittar från `direction` mot origo.

    Standardriktningen ligger på snittets +X-sida, så att båda delarnas
    snittytor vetter mot betraktaren när honan är vänd ett halvt varv.
    """
    forward = np.asarray(direction, dtype=float)
    forward /= np.linalg.norm(forward)
    world_up = np.array([0.0, 0.0, 1.0])
    right = np.cross(world_up, forward)
    right /= np.linalg.norm(right)
    up = np.cross(forward, right)

    matrix = np.eye(4)
    matrix[:3, 0] = right       # skärmens x
    matrix[:3, 1] = up          # skärmens y
    matrix[:3, 2] = forward     # djup: större värde = närmare
    return matrix.T


def shade(normal: np.ndarray, base: np.ndarray) -> str:
    """Enkel flat shading - en färg per triangel."""
    intensity = 0.35 + 0.65 * max(float(np.dot(normal, LIGHT)), 0.0)
    rgb = np.clip(base * intensity, 0.0, 1.0) * 255
    return "#%02x%02x%02x" % tuple(int(v) for v in rgb)


def triangles_of(mesh: trimesh.Trimesh, color: np.ndarray, view: np.ndarray) -> list[tuple]:
    """Projicera trianglarna till 2D och sortera dem bakifrån och fram."""
    vertices = trimesh.transform_points(np.asarray(mesh.vertices, dtype=float), view)
    normals = np.asarray(mesh.face_normals, dtype=float) @ view[:3, :3].T
    faces = np.asarray(mesh.faces)

    out = []
    for face, normal in zip(faces, normals):
        if normal[2] <= 0:  # baksidor syns ändå inte (normalen pekar bort)
            continue
        corners = vertices[face]
        depth = float(corners[:, 2].mean())
        out.append((depth, corners[:, :2], shade(normal, color)))
    return out


def render(
    meshes_and_colors, title: str, subtitle: str, path: Path, captions: bool = True
) -> None:
    """Rita bilden. Utan `captions` blir det bara geometrin, för användning där
    rubriken redan står bredvid (gränssnittet)."""
    view = view_matrix()
    triangles: list[tuple] = []
    for mesh, color in meshes_and_colors:
        triangles.extend(triangles_of(mesh, color, view))
    triangles.sort(key=lambda item: item[0])  # målarens algoritm

    points = np.vstack([t[1] for t in triangles])
    low, high = points.min(axis=0), points.max(axis=0)
    span = np.maximum(high - low, 1e-6)
    margin = 26
    reserved = 44 if captions else 0
    scale = min((WIDTH - 2 * margin) / span[0], (HEIGHT - 2 * margin - reserved) / span[1])
    offset = np.array([WIDTH / 2, (HEIGHT + reserved) / 2]) - (low + high) / 2 * scale * [
        1,
        -1,
    ]

    def project(xy: np.ndarray) -> np.ndarray:
        return xy * scale * [1, -1] + offset

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="{WIDTH}" height="{HEIGHT}" role="img" aria-label="{title}">',
        f"<title>{title}</title>",
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{BACKGROUND}"/>',
    ]
    if captions:
        parts.append(
            f'<text x="{WIDTH / 2}" y="24" text-anchor="middle" font-family="sans-serif" '
            f'font-size="15" font-weight="600" fill="#22262c">{title}</text>'
        )
    for _, corners, color in triangles:
        screen = project(corners)
        points_attr = " ".join(f"{x:.1f},{y:.1f}" for x, y in screen)
        parts.append(
            f'<polygon points="{points_attr}" fill="{color}" '
            f'stroke="{color}" stroke-width="0.6"/>'
        )
    if captions:
        parts.append(
            f'<text x="{WIDTH / 2}" y="{HEIGHT - 10}" text-anchor="middle" '
            f'font-family="sans-serif" font-size="12" fill="#5a6068">{subtitle}</text>'
        )
    parts.append("</svg>")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")


def flip(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Vänd en del ett halvt varv kring sin egen mitt, så urtaget syns."""
    centre = mesh.bounds.mean(axis=0)
    turned = mesh.copy()
    turned.apply_translation(-centre)
    turned.apply_transform(trimesh.transformations.rotation_matrix(math.pi, [0, 0, 1]))
    turned.apply_translation(centre)
    return turned


def half(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Skär bort främre halvan så att hål och fickor inuti blir synliga."""
    middle = float(mesh.bounds.mean(axis=0)[1])
    cut = trimesh.intersections.slice_mesh_plane(
        mesh, [0, 1, 0], [0, middle, 0], cap=True, engine="manifold"
    )
    return cut if cut is not None and len(cut.faces) else mesh


def main() -> int:
    for name, case in CASES.items():
        joint_type = case.get("joint", name)
        below, above = split_box(case["extents"])
        result = build_joint(
            below, above, PLANE, JointParams(joint_type=joint_type, **case["params"])
        )
        if joint_type != "none" and not result.applied:
            print(f"VARNING: {name} gick inte att bygga: {result.attempts}")

        mesh_a, mesh_b = result.mesh_a.copy(), result.mesh_b.copy()
        if case.get("section"):
            mesh_a, mesh_b = half(mesh_a), half(mesh_b)
        if case.get("flip"):
            mesh_b = flip(mesh_b)

        gap = float(case.get("explode", EXPLODE_MM))
        mesh_a.apply_translation([-gap / 2, 0, 0])
        mesh_b.apply_translation([gap / 2, 0, 0])

        drawing = [(mesh_a, COLOR_A), (mesh_b, COLOR_B)]
        root = Path(__file__).resolve().parents[1]
        for suffix, captions in (("", True), ("-plain", False)):
            path = OUT_DIR / f"{name}{suffix}.svg"
            render(drawing, case["title"], case["subtitle"], path, captions=captions)
            print(f"skrev {path.relative_to(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
