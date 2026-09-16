"""Var en belastad modell helst inte ska kapas.

Ska en hylla bära något spelar det stor roll *var* snittet hamnar. Ett snitt
där böjmomentet är som störst lägger fogen på den punkt som är hårdast
belastad; ett snitt där momentet är nära noll kostar nästan ingenting.

Programmet ville kapa en 250 mm bred hyllram vid x=138 - rakt genom alla sju
slatsen, mitt på deras spann, alltså exakt där böjningen är värst. Med lasten
känd får det läget ett straff och snittet flyttar dit modellen är hel.

**Vad modulen gör och inte gör.** Den räknar ut momentets *form* längs modellen
och använder den för att rangordna snittlägen. Den räknar inte ut hur mycket
hyllan håller, och ska inte utvidgas till det.

Ett tidigt försök gjorde just det: yttröghetsmomentet ur tvärsnittet, σ = Mc/I,
en spänning i MPa. Formeln behandlade hela tvärsnittet som en sammanhängande
balk 182 mm hög, medan lasten i verkligheten vilar på slatsplanet och stolparna
bara finns längst bak. Svaret blev 0,03 MPa - hundratals gånger för lågt, och
lugnande. En strukturmodell av en godtycklig mesh är ett eget problem, och FDM
varierar dessutom ±50 % med skrivare, material och kylning. Ett tal som ser ut
att vara beräknat men inte är det är farligare än inget tal, för det är det man
hänger upp en NAS på.

Rangordningen däremot är robust: momentkurvans form beror på upphängningen och
spännvidden, inte på tvärsnittets detaljer. Den säger inte hur mycket som
håller, men den säger var man ska undvika att kapa - och det är frågan
planeraren ställer.

Ordlista:

``upphängning``
    Hur hyllan bärs upp. Avgör momentkurvan helt: en utkragare har sitt
    maximum vid väggen, en hylla uppburen i båda ändar har det mitt emellan.
``spännaxel``
    Axeln lasten verkar längs, alltså hyllans djup för en utkragare.
``relativt moment``
    Böjmomentet i en punkt delat med det största längs modellen, 0 till 1.
    Det är allt poängsättningen behöver.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import trimesh

log = logging.getLogger(__name__)

__all__ = [
    "SUPPORTS",
    "SUPPORT_LABELS",
    "LoadCase",
    "Setting",
    "guess_load_case",
    "relative_moment",
    "moment_profile",
    "transformed_load",
    "print_advice",
    "describe_advice",
    "describe_load_case",
]

#: Upphängningar modulen känner till.
SUPPORTS = ("cantilever", "both_ends", "free")

#: Namn att visa för användaren.
SUPPORT_LABELS = {
    "cantilever": "Utkragad från vägg",
    "both_ends": "Uppburen i båda ändar",
    "free": "Ingen last att ta hänsyn till",
}

#: Hur tydligt tvärsnittet måste skilja sig mellan ändarna för att gissningen
#: på infästningsände ska räknas som säker. Under det säger vi att vi gissat.
END_AREA_RATIO = 1.3

#: Antal punkter momentkurvan mäts i. Fler ger inget - kurvan är slät.
PROFILE_STEPS = 64

#: Hur stor del av modellen som räknas som "änden" när gissningen letar efter
#: infästningen, och hur många tvärsnitt som mäts där. Ett enda tvärsnitt
#: räcker inte: en gavel är ofta bara ett par centimeter tjock, och ett prov
#: mitt i den yttersta tiondelen kan lika gärna hamna bredvid den.
END_FRACTION = 0.15
END_SAMPLES = 8


@dataclass(frozen=True)
class LoadCase:
    """Lasten en modell ska bära, och hur den bärs upp.

    `axis` är spännaxeln (0=X, 1=Y, 2=Z) och `fixed_at_low` säger i vilken
    ände väggen sitter för en utkragare.
    """

    mass_kg: float = 0.0
    support: str = "free"
    axis: int = 1
    fixed_at_low: bool = True
    #: Hur gissningen gjordes, för att kunna visa den och låta användaren
    #: rätta den. Tomt när användaren själv valt.
    guessed_from: str = ""

    @property
    def active(self) -> bool:
        return self.support in ("cantilever", "both_ends") and self.mass_kg > 0

    def to_dict(self) -> dict:
        return {
            "mass_kg": round(float(self.mass_kg), 3),
            "support": self.support,
            "axis": "XYZ"[self.axis],
            "fixed_at_low": bool(self.fixed_at_low),
            "guessed_from": self.guessed_from,
        }


def _section_area(mesh: trimesh.Trimesh, axis: int, position: float) -> float:
    from .resize import section_polygon

    geometry = section_polygon(mesh, axis, float(position))
    return 0.0 if geometry is None else float(geometry.area)


def _end_area(mesh: trimesh.Trimesh, axis: int, low: float, high: float, at_low: bool) -> float:
    """Genomsnittlig tvärsnittsarea i den yttersta delen av modellen."""
    span = high - low
    reach = END_FRACTION * span
    if at_low:
        samples = np.linspace(low + 0.02 * span, low + reach, END_SAMPLES)
    else:
        samples = np.linspace(high - reach, high - 0.02 * span, END_SAMPLES)
    return float(np.mean([_section_area(mesh, axis, x) for x in samples]))


def guess_load_case(mesh: trimesh.Trimesh, mass_kg: float) -> LoadCase:
    """Gissa spännaxel och infästningsände ur formen.

    Två enkla regler, valda för att gå att förklara i en mening var:

    * **Spännaxeln** är den längsta vågräta axeln. En hylla sticker ut från
      väggen eller spänner mellan två gavlar; åt det hållet är den längst.
    * **Infästningen** sitter i den ände som har mest material i tvärsnittet.
      Där sitter gavlarna, stolparna eller bakstycket som bär upp resten.

    Gissningen är med flit trubbig, och den visas för användaren med sitt skäl
    så att den går att rätta. Fel upphängning vänder momentkurvan helt, och det
    är inget programmet ska avgöra i tysthet.
    """
    extents = np.asarray(mesh.extents, dtype=float)
    axis = int(np.argmax(extents[:2])) if extents[0] != extents[1] else 0

    low = float(mesh.bounds[0][axis])
    high = float(mesh.bounds[1][axis])
    span = high - low
    near = _end_area(mesh, axis, low, high, at_low=True)
    far = _end_area(mesh, axis, low, high, at_low=False)

    fixed_at_low = near >= far
    thick, thin = (near, far) if fixed_at_low else (far, near)
    end = "början" if fixed_at_low else "slutet"
    if thin > 0 and thick / thin >= END_AREA_RATIO:
        why = (
            f"{'XYZ'[axis]} är längsta vågräta axeln, och tvärsnittet är "
            f"{thick / thin:.1f} gånger större vid {end} - där sitter rimligen "
            "infästningen."
        )
    else:
        why = (
            f"{'XYZ'[axis]} är längsta vågräta axeln. Ändarna är ungefär lika "
            "kraftiga, så vilken som sitter mot väggen är en ren gissning."
        )

    return LoadCase(
        mass_kg=float(mass_kg),
        support="cantilever",
        axis=axis,
        fixed_at_low=fixed_at_low,
        guessed_from=why,
    )


def relative_moment(load: LoadCase, position: float, low: float, high: float) -> float:
    """Böjmomentet i `position`, som andel av det största längs modellen.

    Lasten antas jämnt fördelad - en NAS står på fötter, men över en hylla som
    är lika lång som lasten är bred är skillnaden liten jämfört med osäkerheten
    i allt annat.

    Utkragare: M(x) = w(L-x)²/2, störst vid infästningen och noll ytterst.
    Uppburen i båda ändar: M(x) = w·x(L-x)/2, störst mitt emellan och noll i
    ändarna.
    """
    span = high - low
    if not load.active or span <= 0:
        return 0.0

    t = float(np.clip((position - low) / span, 0.0, 1.0))
    if load.support == "cantilever":
        # t mätt från infästningen.
        if not load.fixed_at_low:
            t = 1.0 - t
        return float((1.0 - t) ** 2)
    if load.support == "both_ends":
        return float(4.0 * t * (1.0 - t))  # normaliserad till 1 i mitten
    return 0.0


def moment_profile(load: LoadCase, low: float, high: float) -> np.ndarray:
    """Momentkurvan som (position, relativt moment), för visning."""
    xs = np.linspace(low, high, PROFILE_STEPS)
    return np.column_stack([xs, [relative_moment(load, x, low, high) for x in xs]])


def describe_load_case(load: LoadCase) -> str:
    """Lastfallet i klartext."""
    if not load.active:
        return "Ingen last angiven - snitten placeras utan hänsyn till bärighet."
    where = (
        "störst vid infästningen"
        if load.support == "cantilever"
        else "störst mitt emellan upplagen"
    )
    return (
        f"{SUPPORT_LABELS[load.support]}, {load.mass_kg:.1f} kg längs "
        f"{'XYZ'[load.axis]}. Böjmomentet är {where}, och snitt undviks där."
    )


def transformed_load(load: LoadCase, transform) -> LoadCase:
    """Samma lastfall uttryckt i ett roterat koordinatsystem.

    Planeringen roterar modellen för bästa passform, och spännaxeln följer med.
    Gör man inte om lastfallet hamnar straffet på fel axel och snittet flyttar
    åt fel håll - tyst, och åt det håll som ser rimligt ut.
    """
    matrix = np.asarray(transform, dtype=float)
    direction = np.zeros(3)
    direction[load.axis] = 1.0
    rotated = matrix[:3, :3] @ direction

    axis = int(np.argmax(np.abs(rotated)))
    flipped = bool(rotated[axis] < 0)
    return LoadCase(
        mass_kg=load.mass_kg,
        support=load.support,
        axis=axis,
        # Vänder axeln riktning vänder också vilken ände som sitter mot väggen.
        fixed_at_low=load.fixed_at_low if not flipped else not load.fixed_at_low,
        guessed_from=load.guessed_from,
    )


@dataclass(frozen=True)
class Setting:
    """En inställning i slicern, med skälet till den."""

    name: str
    value: str
    why: str

    def to_dict(self) -> dict:
        return {"name": self.name, "value": self.value, "why": self.why}


def print_advice(load: LoadCase, nozzle_mm: float = 0.4) -> list[Setting]:
    """Inställningar för en del som ska bära last.

    Det här är **tumregler**, inte beräkningar. De följer av hur FDM går sönder
    i böjning: sprickan går mellan lagren, och materialet som bär sitter i
    skalet längst från neutrallagret. Ingen av dem är ett löfte om en siffra -
    se modulens docstring om varför programmet inte räknar ut bärighet.

    Ordningen är avsiktlig: det som ger mest hållfasthet per minut först.
    """
    if not load.active:
        return []

    walls = 5 if load.mass_kg >= 3.0 else 4
    layer = round(0.65 * nozzle_mm, 2)
    return [
        Setting(
            "Väggar (perimeters)",
            f"{walls} st",
            "Väggarna bär böjningen - de ligger längst från neutrallagret. Att "
            "gå från 2 till 5 väggar ger mycket mer än att höja fyllnaden lika "
            "mycket, och kostar mindre tid.",
        ),
        Setting(
            "Fyllnad",
            "25 %, gyroid",
            "Över ungefär 30 % ger varje procent lite styrka och mycket tid. "
            "Gyroid håller lika bra åt alla håll, vilket spelar roll när "
            "lasten inte är helt förutsägbar.",
        ),
        Setting(
            "Topp- och bottenlager",
            "5 st",
            "Samma skäl som väggarna: yttersta materialet är det som bär, och "
            "delen ligger platt så böjningen drar i topp och botten.",
        ),
        Setting(
            "Lagerhöjd",
            f"{layer:.2f} mm",
            f"Ungefär 65 % av munstyckets {nozzle_mm:g} mm. Tjockare lager är "
            "både snabbare och något starkare mellan lagren - det blir färre "
            "fogar att spricka i.",
        ),
        Setting(
            "Temperatur",
            "+5 till +10 °C över normalt",
            "Lagerhäftningen är den svaga riktningen, och den blir bättre av "
            "varmare plast. Det är den enda inställningen här som är gratis i tid.",
        ),
        Setting(
            "Fläkt",
            "Sänk till 30-50 %",
            "Snabb kylning ger fina detaljer men svagare lagerfogar. En hylla "
            "behöver det omvända.",
        ),
        Setting(
            "Orientering",
            "Delen liggande (kryssrutan Vänd delarna platt)",
            "Lagren ska ligga längs delen, inte tvärs. En list som skrivs "
            "stående böjs isär lager från lager - FDM:s svagaste riktning.",
        ),
    ]


def describe_advice(load: LoadCase, nozzle_mm: float = 0.4) -> str:
    """Inställningarna i klartext, med tidsavvägningen sist."""
    settings = print_advice(load, nozzle_mm=nozzle_mm)
    if not settings:
        return "Ingen last angiven - inga särskilda utskriftsinställningar behövs."

    lines = ["Utskriftsinställningar för en belastad del (tumregler, inte beräkningar):"]
    for setting in settings:
        lines.append(f"  {setting.name}: {setting.value}")
        lines.append(f"      {setting.why}")
    lines.append(
        "  Vill du korta tiden: sänk fyllnaden och höj lagerhöjden. Spara inte "
        "in på väggarna eller temperaturen - det är de som bär."
    )
    return "\n".join(lines)
