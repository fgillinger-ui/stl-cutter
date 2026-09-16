"""Skriva slicerprofiler för en belastad del.

`core.load` ger inställningarna i klartext med skäl. Den här modulen skriver
samma inställningar som en fil slicern kan läsa, så att de inte behöver knappas
in för hand varje gång.

**Formatet.** OrcaSlicer och de slicers som bygger på den - FlashPrint för
Flashforge Creator 5, Bambu Studio, Qidi Studio - läser profiler som JSON där
varje värde är en sträng och `inherits` pekar på en profil som redan finns.
Nycklarna är avlästa ur OrcaSlicers egna profiler, inte gissade: `wall_loops`,
`sparse_infill_density`, `top_shell_layers`, `bottom_shell_layers`,
`sparse_infill_pattern`, `layer_height` i processfilen, och
`nozzle_temperature`, `fan_max_speed`, `fan_min_speed` i filamentfilen - där
varje värde är en *lista* med en sträng, en per extruder.

**Vad som krävs för att importen ska lyckas.** Första versionen av den här
modulen skrev en fil som såg riktig ut och som slicern förkastade med *"There
are 0 configs imported"* - utan att säga varför. Två krav saknades, båda
avlästa ur `PresetBundle::import_json_presets` i OrcaSlicers källkod:

* ``version`` måste finnas och gå att tolka som ett versionsnummer. Saknas det
  avbryts inläsningen på rad tre, före allt annat: ``if (!version) return
  false;``
* Profiltypen avgörs **inte** av fältet ``type`` utan av vilket id-fält som
  finns: ``print_settings_id`` gör den till en processprofil,
  ``filament_settings_id`` till en filamentprofil. Utan något av dem blir det
  *"Preset type is unknown, not loading"*.

Filen skrivs därför exakt som slicern själv skriver sina användarprofiler:
``version``, ``name``, ``from``, ``inherits``, id-fältet och inställningarna.
``type`` och ``instantiation`` hör till leverantörsprofiler och tas bort igen
av slicern, så de skrivs inte.

**Varför `inherits` är obligatoriskt.** En processprofil har hundratals
inställningar: hastigheter, accelerationer, stöd, primtorn. Vi kan bara de sju
som har med hållfasthet att göra. Resten måste komma från en profil som redan
fungerar för skrivaren, och den kan bara användaren namnge - det är den som
står i slicerns rullgardin. Att skriva en fristående profil hade betytt att
hitta på de övriga hundra värdena, och en profil som ser komplett ut men har
gissade hastigheter är värre än ingen profil alls.

**Varför temperaturen inte sätts utan att du anger den.** Rådet är "+5 till
+10 °C över det normala", och vad som är normalt beror på filamentet: PLA
215, PETG 235, ASA 255. En absolut siffra kräver alltså att någon säger vad
utgångsläget är. Utan den skrivs ingen filamentprofil, och det står varför.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .load import LoadCase

log = logging.getLogger(__name__)

__all__ = [
    "ProfileError",
    "ProfileBundle",
    "process_profile",
    "filament_profile",
    "write_profiles",
]

#: Versionsnumret i profilen. Tre siffror är giltigt både för den strikta
#: semver-tolken och den utökade som slicern använder. Värdet jämförs inte mot
#: något - det måste bara gå att tolka, annars förkastas hela filen.
PROFILE_VERSION = "1.0.0"

#: Hur mycket temperaturen höjs för bättre lagerhäftning, i °C. Mitten av
#: intervallet 5-10 som `core.load` rekommenderar.
TEMPERATURE_BOOST_C = 8

#: Fläkten hålls i det här intervallet. Snabb kylning ger fina detaljer men
#: svagare lagerfogar, och en bärande del behöver det omvända. Är skrivarens
#: profil redan tystare än så lämnas den i fred - taket sänker, det höjer inte.
FAN_MAX_PERCENT = 50
FAN_MIN_PERCENT = 30


class ProfileError(ValueError):
    """Profilen gick inte att skriva, med ett skäl som går att åtgärda."""


@dataclass
class ProfileBundle:
    """Filerna som skrevs, och det som medvetet inte skrevs."""

    files: list[Path]
    notes: list[str]


def _safe_name(name: str) -> str:
    """Ett namn som duger både som filnamn och som profilnamn.

    Slicern använder profilnamnet som filnamn när den sparar, och vägrar ett
    namn som innehåller sökvägstecken (`is_path_within_root` i
    `import_json_presets`). Därför städas namnet innan det skrivs, inte bara
    filnamnet.
    """
    cleaned = re.sub(r"[^\w\s.+-]", "_", name, flags=re.UNICODE).strip()
    return re.sub(r"\s+", " ", cleaned) or "profil"


def _layer_height(nozzle_mm: float) -> float:
    """Ungefär 65 % av munstycket, avrundat till något slicern trivs med."""
    return round(0.65 * nozzle_mm / 0.02) * 0.02


def process_profile(
    load: LoadCase,
    base_profile: str,
    name: str = "Bärande delar",
    nozzle_mm: float = 0.4,
) -> dict:
    """Processprofilen: väggar, skal, fyllnad och lagerhöjd.

    Bara de inställningar som `core.load` motiverar sätts. Allt annat ärvs
    från `base_profile`, som måste vara namnet på en profil som redan finns i
    slicern - det som står i rullgardinen.
    """
    if not load.active:
        raise ProfileError(
            "Ingen last angiven, så det finns inga hållfasthetsinställningar "
            "att skriva. Kryssa i Belastning och ange vikten först."
        )
    if not base_profile.strip():
        raise ProfileError(
            "Ange vilken profil den ska bygga på - namnet som står i slicerns "
            "rullgardin för processinställningar, till exempel "
            "'0.20mm Standard @FF C5'. Utan den vet profilen ingenting om din "
            "skrivare."
        )

    walls = 5 if load.mass_kg >= 3.0 else 4
    safe = _safe_name(name)
    return {
        # Ordningen är den slicern själv skriver: rubrikerna först.
        "version": PROFILE_VERSION,
        "name": safe,
        "from": "User",
        "inherits": base_profile.strip(),
        # Det här fältet, inte "type", gör den till en processprofil.
        "print_settings_id": safe,
        # Väggarna bär böjningen - de ligger längst från neutrallagret.
        "wall_loops": str(walls),
        # Yttersta materialet bär, och delen ligger platt.
        "top_shell_layers": "5",
        "bottom_shell_layers": "5",
        # Över ~30 % ger varje procent lite styrka och mycket tid.
        "sparse_infill_density": "25%",
        # Gyroid håller lika bra åt alla håll.
        "sparse_infill_pattern": "gyroid",
        # Tjockare lager: snabbare, och färre lagerfogar att spricka i.
        "layer_height": f"{_layer_height(nozzle_mm):.2f}",
    }


def filament_profile(
    load: LoadCase,
    base_profile: str,
    normal_temp_c: float,
    name: str = "Bärande delar",
) -> dict:
    """Filamentprofilen: varmare plast och lugnare fläkt.

    `normal_temp_c` är den temperatur du brukar köra filamentet i. Den kan
    inte gissas - se modulens docstring.
    """
    if not load.active:
        raise ProfileError("Ingen last angiven.")
    if not base_profile.strip():
        raise ProfileError(
            "Ange vilken filamentprofil den ska bygga på, till exempel "
            "'Flashforge HS PETG @FF C5'."
        )
    if normal_temp_c <= 0:
        raise ProfileError("Ange filamentets normala temperatur i °C.")

    hot = str(int(round(normal_temp_c + TEMPERATURE_BOOST_C)))
    safe = _safe_name(name)
    return {
        "version": PROFILE_VERSION,
        "name": safe,
        "from": "User",
        "inherits": base_profile.strip(),
        # Motsvarigheten för filament - och en lista, som alla filamentvärden.
        "filament_settings_id": [safe],
        # Lagerhäftningen är den svaga riktningen och blir bättre av varmare
        # plast. Första lagret får samma värde - det ska sitta.
        "nozzle_temperature": [hot],
        "nozzle_temperature_initial_layer": [hot],
        # Tak, inte golv: har skrivarens profil redan en lugnare fläkt är det
        # bara bra, och då är de här värdena ingen försämring.
        "fan_max_speed": [str(FAN_MAX_PERCENT)],
        "fan_min_speed": [str(FAN_MIN_PERCENT)],
    }


def write_profiles(
    load: LoadCase,
    out_dir: str | Path,
    base_profile: str,
    name: str = "Bärande delar",
    nozzle_mm: float = 0.4,
    filament_base: str = "",
    normal_temp_c: float = 0.0,
) -> ProfileBundle:
    """Skriv profilerna till `out_dir` och berätta vad som inte gick att göra."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(name)

    files: list[Path] = []
    notes: list[str] = []

    process_path = out_dir / f"{stem} - process.json"
    process_path.write_text(
        json.dumps(process_profile(load, base_profile, name, nozzle_mm), indent=4, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    files.append(process_path)

    if filament_base.strip() and normal_temp_c > 0:
        filament_path = out_dir / f"{stem} - filament.json"
        filament_path.write_text(
            json.dumps(
                filament_profile(load, filament_base, normal_temp_c, name),
                indent=4,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        files.append(filament_path)
    else:
        notes.append(
            "Ingen filamentprofil skrevs. Temperatur och fläkt hör till "
            "filamentet, och rådet är +5 till +10 °C över det normala - vad "
            "som är normalt beror på om du kör PLA, PETG eller ASA. Ange "
            "filamentprofilens namn och dess vanliga temperatur, så skrivs "
            "den också."
        )
    return ProfileBundle(files=files, notes=notes)
