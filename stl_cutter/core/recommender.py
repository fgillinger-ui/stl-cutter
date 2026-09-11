"""Val av fogtyp för ett snitt.

Regelbaserad motor: varje fogtyp poängsätts mot snittets mått
(`analysis.SectionAnalysis`) och användarens monteringsavsikt. Bäst poäng
vinner, och de tre bästa returneras så att GUI:t kan erbjuda alternativ.

Ingen geometri byggs här - det görs i fas 3 (`core/joints/`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .analysis import SectionAnalysis
from .printers import DEFAULT_CLEARANCE_MM, PrinterProfile

AssemblyIntent = Literal["glue", "demountable"]

JOINT_TYPES = ("none", "puzzle", "dovetail", "pins", "screw")

#: Tröskelvärden i mm, enligt reglerna för fogval.
MIN_WALL_FOR_JOINT = 4.0
PUZZLE_MAX_WALL = 8.0
DOVETAIL_MIN_WALL = 8.0
PINS_MIN_WALL = 6.0
SCREW_MIN_WALL = 12.0

#: Över den här arean kompletteras vald fog med två extra styrpinnar.
LARGE_AREA_MM2 = 5000.0

#: Pinndiameter är en andel av minsta väggtjocklek, med ett tak.
PIN_DIAMETER_RATIO = 0.2
PIN_DIAMETER_MAX_MM = 8.0

INTENT_LABELS = {"glue": "limmas permanent", "demountable": "ska kunna tas isär"}


@dataclass
class JointRecommendation:
    """En föreslagen fogtyp med parametrar och motivering på svenska."""

    joint_type: str
    params: dict = field(default_factory=dict)
    motivation: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "joint_type": self.joint_type,
            "params": self.params,
            "motivation": self.motivation,
            "confidence": round(self.confidence, 3),
        }

    def describe(self) -> str:
        return f"{self.joint_type} ({self.confidence * 100:.0f} % säkerhet): {self.motivation}"


def _pin_params(analysis: SectionAnalysis, clearance: float, count: int | None = None) -> dict:
    diameter = min(PIN_DIAMETER_RATIO * analysis.min_wall_mm, PIN_DIAMETER_MAX_MM)
    diameter = max(2.0, round(diameter * 2) / 2)  # avrunda till halv mm, minst 2 mm
    if count is None:
        if analysis.area_mm2 < 1000:
            count = 2
        elif analysis.area_mm2 < LARGE_AREA_MM2:
            count = 3
        else:
            count = 4
    return {
        "count": count,
        "diameter_mm": diameter,
        "length_mm": round(min(3.0 * diameter, 20.0), 1),
        "edge_margin_mm": 3.0,
        "clearance_mm": clearance,
    }


def _dovetail_params(analysis: SectionAnalysis, clearance: float) -> dict:
    long_side, short_side = max(analysis.bbox_mm), min(analysis.bbox_mm)
    if long_side < 60:
        count = 1
    elif long_side < 150:
        count = 2
    else:
        count = 3
    # Djupet är hur långt laxstjärten sticker in i den andra delen. Det behöver
    # inte vara stort - runt 1,5 gånger halsbredden räcker gott, och ett djup
    # som skalar med snittets längd gör bara delarna onödigt otympliga.
    width = round(min(0.5 * short_side, 20.0), 1)
    return {
        "count": count,
        "width_mm": width,
        "depth_mm": round(min(1.5 * width, 0.25 * long_side, 15.0), 1),
        "angle_deg": 8.0,
        "chamfer_mm": 0.4,
        "clearance_mm": clearance,
    }


def _puzzle_params(analysis: SectionAnalysis, clearance: float) -> dict:
    short_side = min(analysis.bbox_mm)
    period = min(max(analysis.perimeter_mm / 6.0, 10.0), 40.0)
    return {
        "profile": "sine",
        "period_mm": round(period, 1),
        "amplitude_mm": round(min(max(0.15 * short_side, 2.0), 8.0), 1),
        "thickness_mm": round(analysis.min_wall_mm, 2),
        "clearance_mm": clearance,
    }


def _screw_params(clearance: float) -> dict:
    return {
        "screw": "M3",
        "hole_diameter_mm": 3.4,
        "counterbore_diameter_mm": 6.0,
        "counterbore_depth_mm": 3.0,
        "nut_across_flats_mm": 5.5,
        "nut_depth_mm": 2.6,
        "count": 2,
        "guide_pins": 2,
        "clearance_mm": clearance,
    }


def _candidates(
    analysis: SectionAnalysis, intent: AssemblyIntent, clearance: float
) -> list[JointRecommendation]:
    """Poängsätt alla fogtyper. Poängen blir konfidenssiffran."""
    # Fogen byggs på varje ö för sig, och det är den största som bär den.
    # Att låta en tunn flik i kanten avgöra fogvalet för hela snittet vore fel.
    wall = analysis.main_wall_mm
    demountable = intent == "demountable"
    thinner = (
        f" (den tunnaste delen av snittet är {analysis.min_wall_mm:.1f} mm)"
        if analysis.has_thinner_islands
        else ""
    )
    out: list[JointRecommendation] = []

    if analysis.empty:
        return [
            JointRecommendation(
                "none",
                {},
                "Planet träffar ingen geometri - ingen fog behövs.",
                0.9,
            )
        ]

    # --- none: plan limfog -------------------------------------------------
    if wall < MIN_WALL_FOR_JOINT:
        out.append(
            JointRecommendation(
                "none",
                {"surface": "plan", "clearance_mm": 0.0},
                f"Snittet är bara {wall:.1f} mm tjockt - för tunt för en fog.{thinner} "
                "Limma ihop den plana ytan.",
                0.9,
            )
        )
    else:
        out.append(
            JointRecommendation(
                "none",
                {"surface": "plan", "clearance_mm": 0.0},
                "Plan limfog fungerar alltid, men ger ingen styrning vid montering.",
                0.2 if not demountable else 0.1,
            )
        )

    # --- puzzle ------------------------------------------------------------
    if wall >= MIN_WALL_FOR_JOINT and analysis.is_flat:
        if wall <= PUZZLE_MAX_WALL:
            score, why = 0.85, (
                f"Platt snitt ({analysis.roundness:.2f} rundhet) och {wall:.1f} mm tjockt - "
                "en pusselprofil genom hela tjockleken låser delarna i sidled."
            )
        else:
            score, why = 0.45, (
                f"Platt snitt men {wall:.1f} mm tjockt - pussel går, men en tapp- eller "
                "laxstjärtsfog utnyttjar tjockleken bättre."
            )
        if demountable:
            score -= 0.15  # pusselfogar limmas oftast
        out.append(JointRecommendation("puzzle", _puzzle_params(analysis, clearance), why, score))

    # --- dovetail ----------------------------------------------------------
    if wall >= DOVETAIL_MIN_WALL:
        if analysis.is_elongated:
            score, why = 0.9, (
                f"{wall:.1f} mm tjockt och avlångt snitt "
                f"(förhållande {analysis.aspect_ratio:.1f}:1) - laxstjärt längs långsidan "
                "ger en tydlig glidriktning och drar ihop delarna."
            )
        else:
            score, why = 0.45, (
                f"{wall:.1f} mm tjockt men snittet saknar tydlig glidriktning - "
                "laxstjärt går, men blir känsligare för passning."
            )
        out.append(
            JointRecommendation("dovetail", _dovetail_params(analysis, clearance), why, score)
        )

    # --- pins --------------------------------------------------------------
    if wall >= PINS_MIN_WALL:
        if analysis.is_round:
            score, why = 0.85, (
                f"Rundaktigt snitt (rundhet {analysis.roundness:.2f}) och {wall:.1f} mm "
                "tjockt - styrpinnar centrerar delarna oavsett vridning."
            )
        else:
            score, why = 0.5, (
                f"{wall:.1f} mm tjockt - styrpinnar ger enkel och robust styrning, "
                "men låser inte delarna mot dragkraft."
            )
        out.append(JointRecommendation("pins", _pin_params(analysis, clearance), why, score))

    # --- screw -------------------------------------------------------------
    if demountable and wall >= SCREW_MIN_WALL:
        out.append(
            JointRecommendation(
                "screw",
                _screw_params(clearance),
                f"Du har valt att delarna ska kunna tas isär och snittet är {wall:.1f} mm "
                "tjockt - M3-skruv med mutterficka plus två styrpinnar.",
                0.95,
            )
        )
    elif demountable and wall >= DOVETAIL_MIN_WALL:
        out.append(
            JointRecommendation(
                "screw",
                _screw_params(clearance),
                f"Demonterbart önskas men snittet är bara {wall:.1f} mm - "
                f"en M3-insats kräver helst {SCREW_MIN_WALL:.0f} mm. Går, men blir svagt.",
                0.35,
            )
        )

    return out


def recommend_joint(
    analysis: SectionAnalysis,
    intent: AssemblyIntent = "glue",
    printer: PrinterProfile | None = None,
    clearance_mm: float | None = None,
) -> tuple[JointRecommendation, list[JointRecommendation]]:
    """Välj fogtyp för ett snitt.

    Returnerar (bästa förslag, de tre bästa alternativen inklusive det valda).
    """
    if clearance_mm is None:
        clearance_mm = printer.clearance_mm if printer else DEFAULT_CLEARANCE_MM

    candidates = _candidates(analysis, intent, clearance_mm)
    candidates.sort(key=lambda c: c.confidence, reverse=True)
    best = candidates[0]

    # Stor snittyta: komplettera med två extra styrpinnar för styrning.
    if (
        analysis.area_mm2 > LARGE_AREA_MM2
        and best.joint_type not in ("pins", "screw")
        and not analysis.empty
    ):
        best.params = dict(best.params)
        best.params["guide_pins"] = 2
        best.params["guide_pin_diameter_mm"] = _pin_params(analysis, clearance_mm, count=2)[
            "diameter_mm"
        ]
        best.motivation += (
            f" Snittytan är stor ({analysis.area_mm2:.0f} mm²), så två extra "
            "styrpinnar läggs till för att delarna ska hamna rätt."
        )

    return best, candidates[:3]


def explain(analysis: SectionAnalysis, best: JointRecommendation, index: int) -> str:
    """Läsbar svensk text för `--explain`."""
    axis = "XYZ"[analysis.axis]
    lines = [
        f"Snitt {index}: {axis} = {analysis.position_mm:.1f} mm",
        f"  Snittyta: {analysis.describe()}",
        f"  Rekommendation: {best.joint_type}",
        f"  Motivering: {best.motivation}",
        f"  Säkerhet: {best.confidence * 100:.0f} %",
    ]
    if best.params:
        params = ", ".join(f"{k}={v}" for k, v in best.params.items())
        lines.append(f"  Parametrar: {params}")
    return "\n".join(lines)


def build_recommendation(
    joint_type: str,
    analysis: SectionAnalysis,
    intent: AssemblyIntent = "glue",
    printer: PrinterProfile | None = None,
    clearance_mm: float | None = None,
) -> JointRecommendation:
    """Rekommendation för en *vald* fogtyp, även om regeln inte hade valt den.

    Används när användaren byter fogtyp manuellt (GUI:t i fas 4) - parametrarna
    räknas ut på samma sätt som vanligt, men typen är given.
    """
    if joint_type not in JOINT_TYPES:
        raise ValueError(f"Okänd fogtyp {joint_type!r}. Kända: {', '.join(JOINT_TYPES)}")
    if clearance_mm is None:
        clearance_mm = printer.clearance_mm if printer else DEFAULT_CLEARANCE_MM

    for candidate in _candidates(analysis, intent, clearance_mm):
        if candidate.joint_type == joint_type:
            return candidate

    # Typen är inte lämplig här, men användaren har bett om den ändå.
    builders = {
        "none": lambda: {"surface": "plan", "clearance_mm": 0.0},
        "pins": lambda: _pin_params(analysis, clearance_mm),
        "dovetail": lambda: _dovetail_params(analysis, clearance_mm),
        "puzzle": lambda: _puzzle_params(analysis, clearance_mm),
        "screw": lambda: _screw_params(clearance_mm),
    }
    return JointRecommendation(
        joint_type,
        builders[joint_type](),
        "Vald manuellt. Snittet uppfyller inte villkoren för den här fogtypen, "
        "så passformen kan bli sämre än vanligt.",
        0.2,
    )
