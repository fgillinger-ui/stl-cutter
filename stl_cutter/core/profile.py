"""Skriva slicerprofiler för en belastad del.

`core.load` ger inställningarna i klartext med skäl, och med de nycklar och
värden de motsvarar i en slicerprofil. Den här modulen skriver de värdena som
filer slicern kan läsa - den har inga egna siffror, så råden och profilen kan
inte säga olika saker.

**Formatet.** OrcaSlicer och de slicers som bygger på den - Orca-Flashforge
(Flashforges "Flash Studio" för Creator 5), Bambu Studio, Qidi Studio - läser
profiler som JSON där varje värde är en sträng och `inherits` pekar på en
profil som redan finns. I filamentprofiler är varje värde en *lista* med
strängar, en per extrudervariant.

**Vad importen kräver.** Avläst ur Orca-Flashforges källkod, grenen
``release_0817`` (commit 84b7074, den första gren där Creator 5 Pro finns -
``main`` har den inte):

* ``PresetBundle::import_json_presets`` -
  https://github.com/FlashForge/Orca-Flashforge/blob/84b7074b252af144a75fec04a0db3c21e6e65536/src/libslic3r/PresetBundle.cpp#L1635-L1760

  - ``version`` måste gå att tolka som semver, annars avbryts inläsningen
    utan meddelande (rad 1653: ``if (!version) return false;``).
  - Typen avgörs av id-fältet, inte av ``type``: ``print_settings_id`` gör
    den till process, ``filament_settings_id`` till filament (rad 1657-1671).
  - ``inherits`` slås upp med ``find_preset2`` (rad 1696). Finns namnet inte
    hoppas profilen över - bara en rad i slicerns logg (rad 1706). **Det här
    är nästan alltid felet** när importen säger "0 configs imported".
  - En profil med samma namn som en systemprofil vägras (rad 1678).

* ``ConfigBase::load_from_json`` -
  https://github.com/FlashForge/Orca-Flashforge/blob/84b7074b252af144a75fec04a0db3c21e6e65536/src/libslic3r/Config.cpp#L820-L1010

  Varje värde måste vara en sträng eller en lista med strängar av samma typ.
  Ett tal eller en boolean loggas som "invalid json type" och ignoreras.

* ``PresetCollection::load_presets`` (användarmappen) -
  https://github.com/FlashForge/Orca-Flashforge/blob/84b7074b252af144a75fec04a0db3c21e6e65536/src/libslic3r/Preset.cpp#L1590-L1700

  Profilnamnet tas från **filnamnet** (rad 1604), så en fil som läggs direkt
  i mappen måste heta ``<namn>.json``. Utan tolkbar version hoppas den över
  (rad 1645).

* Mapparna. Datamappen är ``$XDG_CONFIG_HOME/<appnamn>`` (GUI_App.cpp rad
  2696), och appnamnet är ``Orca-Flashforge`` (version.inc rad 5; med
  ``-beta``/``-alpha`` för förhandsversioner, GUI_App.cpp rad 2651-2658).
  Användarprofilerna ligger i ``user/<preset_folder>/process`` och
  ``.../filament``, där ``preset_folder`` står i ``<appnamn>.conf`` under
  ``app`` och är ``default`` när ingen är inloggad (PresetBundle.cpp rad
  1498-1500, PresetBundle.hpp rad 17). Systemprofilerna ligger i ``system/``
  - dit skriver programmet aldrig.

**Namnen för Creator 5 Pro med 0,4 mm munstycke** (``resources/profiles/
Flashforge.json`` i samma commit): skrivaren heter ``Flashforge Creator 5 Pro
0.4 nozzle``, processprofilerna ``0.08/0.10/0.12/0.20/0.24mm Standard @FF
C5``, och filamentprofilerna slutar på ``@FF C5P`` - till exempel
``Flashforge HS PETG @FF C5P`` och ``Flashforge PLA Basic @FF C5P``. Den
tidigare platshållaren ``Flashforge HS PETG @FF C5`` finns inte, och en
filamentprofil som ärvde från den vägrades tyst.

**Varför `inherits` är obligatoriskt.** En processprofil har hundratals
inställningar: hastigheter, accelerationer, stöd, primtorn. Vi kan bara de
som har med hållfasthet att göra. Resten måste komma från en profil som redan
fungerar för skrivaren. Därför läser programmet slicerns egna systemprofiler
och låter användaren välja ur en lista, i stället för att namnet ska skrivas
för hand tecken för tecken.

**Varför temperaturen inte sätts utan att du anger den.** Vad som är normalt
beror på filamentet: PLA 210, PETG 235, ASA 270. En absolut siffra kräver att
någon säger vad utgångsläget är. Utan den skrivs ingen filamentprofil, och det
står varför.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import load as load_core
from .load import FAN_MAX_PERCENT, FAN_MIN_PERCENT, TEMPERATURE_BOOST_C, LoadCase

log = logging.getLogger(__name__)

__all__ = [
    "ProfileError",
    "ProfileBundle",
    "SlicerInstall",
    "process_profile",
    "filament_profile",
    "settings_text",
    "write_profiles",
    "validate_profile",
    "find_slicers",
    "system_profiles",
    "user_profile_path",
    "install_profiles",
    "resolve_base",
    "user_profile_names",
    "inherited_value",
    "extruder_variants",
    "TEMPERATURE_BOOST_C",
    "FAN_MAX_PERCENT",
    "FAN_MIN_PERCENT",
]

#: Versionsnumret i profilen. Tre siffror är giltigt både för den strikta
#: semver-tolken och den utökade som slicern använder. Värdet jämförs inte mot
#: något - det måste bara gå att tolka, annars förkastas hela filen.
PROFILE_VERSION = "1.0.0"

# TEMPERATURE_BOOST_C, FAN_MAX_PERCENT och FAN_MIN_PERCENT bor i `core.load`
# och importeras ovan, så att de fortfarande går att nå härifrån.


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


def process_profile(
    load: LoadCase,
    base_profile: str,
    name: str = "Bärande delar",
    nozzle_mm: float = 0.4,
    overrides: dict | None = None,
) -> dict:
    """Processprofilen: väggar, skal, fyllnad och lagerhöjd.

    Värdena hämtas ur `load_core.print_advice` - samma rader som visas i
    förhandsvisningen. Allt annat ärvs från `base_profile`, som måste vara
    namnet på en systemprofil i slicern.

    `overrides` är inställningar från en egen profil som ska följa med (se
    `resolve_base`). Hållfasthetsvärdena skrivs efter dem och vinner.
    """
    if not load.active:
        raise ProfileError(
            "Ingen last angiven, så det finns inga hållfasthetsinställningar "
            "att skriva. Kryssa i Belastning och ange vikten först."
        )
    if not base_profile.strip():
        raise ProfileError(
            "Välj vilken processprofil den ska bygga på - namnet som står i "
            "slicerns rullgardin, till exempel '0.20mm Standard @FF C5'. Utan "
            "den vet profilen ingenting om din skrivare."
        )

    safe = _safe_name(name)
    advice = load_core.print_advice(load, nozzle_mm=nozzle_mm)
    return {
        # Ordningen är den slicern själv skriver: rubrikerna först.
        "version": PROFILE_VERSION,
        "name": safe,
        "from": "User",
        "inherits": base_profile.strip(),
        # Det här fältet, inte "type", gör den till en processprofil.
        "print_settings_id": safe,
        **_clean_overrides(overrides),
        **load_core.profile_values(advice, load_core.PROCESS),
    }


def filament_profile(
    load: LoadCase,
    base_profile: str,
    normal_temp_c: float,
    name: str = "Bärande delar",
    extruder_variants: list[str] | None = None,
    overrides: dict | None = None,
) -> dict:
    """Filamentprofilen: varmare plast och lugnare fläkt.

    `normal_temp_c` är den temperatur du brukar köra filamentet i. Den kan
    inte gissas - se modulens docstring.

    `extruder_variants` är basprofilens ``filament_extruder_variant``, när den
    är känd. Slicern har ett värde per variant i varje lista; med fler än en
    variant skrivs värdet en gång per variant så att längderna stämmer.
    """
    if not load.active:
        raise ProfileError("Ingen last angiven.")
    if not base_profile.strip():
        raise ProfileError(
            "Välj vilken filamentprofil den ska bygga på, till exempel "
            "'Flashforge HS PETG @FF C5P'."
        )
    if normal_temp_c <= 0:
        raise ProfileError("Ange filamentets normala temperatur i °C.")

    safe = _safe_name(name)
    advice = load_core.print_advice(load, normal_temp_c=normal_temp_c)
    count = max(1, len(extruder_variants or []))
    out: dict = {
        "version": PROFILE_VERSION,
        "name": safe,
        "from": "User",
        "inherits": base_profile.strip(),
        # Motsvarigheten för filament - och en lista, som alla filamentvärden.
        "filament_settings_id": [safe],
        **_clean_overrides(overrides),
    }
    if extruder_variants and count > 1:
        out["filament_extruder_variant"] = list(extruder_variants)
    for key, value in load_core.profile_values(advice, load_core.FILAMENT).items():
        out[key] = [value] * count
    return out


#: Vad inställningarna heter i slicerns eget gränssnitt. Nyckeln i profilen
#: står i parentes, så att den går att söka på om språket är ett annat.
UI_LABELS = {
    "wall_loops": "Väggar / Wall loops",
    "top_shell_layers": "Topplager / Top shell layers",
    "bottom_shell_layers": "Bottenlager / Bottom shell layers",
    "sparse_infill_density": "Fyllnadsgrad / Sparse infill density",
    "sparse_infill_pattern": "Fyllnadsmönster / Sparse infill pattern",
    "layer_height": "Lagerhöjd / Layer height",
    "nozzle_temperature": "Munstyckstemperatur / Nozzle temperature",
    "nozzle_temperature_initial_layer": "Munstycke, första lagret",
    "fan_max_speed": "Fläkt max / Fan max speed",
    "fan_min_speed": "Fläkt min / Fan min speed",
}

#: Fälten som är profilens rubrik, inte en inställning att skriva in.
HEADER_KEYS = ("version", "name", "from", "inherits", "print_settings_id", "filament_settings_id")


#: Fält som hör till profilens identitet eller till slicerns synkning och
#: därför aldrig följer med från en egen profil till den nya.
_NOT_COPIED = set(HEADER_KEYS) | {
    "printer_settings_id",
    "setting_id",
    "base_id",
    "user_id",
    "updated_time",
    "type",
    "instantiation",
    "is_custom_defined",
}


def _clean_overrides(overrides: dict | None) -> dict:
    return {k: v for k, v in (overrides or {}).items() if k not in _NOT_COPIED}


def settings_text(process: dict, filament: dict | None = None) -> str:
    """Inställningarna som en lista att knappa in för hand.

    Importen kan misslyckas av skäl programmet inte kan se: profilnamnet i
    `inherits` måste finnas i just den här slicern, och en avknoppning av
    OrcaSlicer kan ha ändrat vad den godtar. Listan fungerar alltid - det är
    sju värden, och efter det spelar det ingen roll om filen gick in eller ej.
    """
    lines = [
        "Inställningar för bärande delar",
        "=" * 34,
        "",
        "Så här importerar du filerna i slicern:",
        "  Arkiv → Importera → Importera konfiguration (File → Import → Import configs)",
        "",
        f"Profilen bygger på: {process.get('inherits', '')!r}",
        "Det namnet måste stå exakt så i slicerns rullgardin, annars vägrar",
        "importen utan att säga varför. Jämför tecken för tecken - mellanslag",
        "och @-suffix räknas.",
        "",
        "Går importen ändå inte: ställ in värdena själv, det är de här och",
        "inga andra. Allt annat lämnas som du har det.",
        "",
        "Processinställningar:",
    ]
    for key, value in process.items():
        if key not in UI_LABELS:
            continue
        lines.append(f"  {UI_LABELS.get(key, key)}: {value}   ({key})")

    if filament:
        lines.append("")
        lines.append("Filamentinställningar:")
        for key, value in filament.items():
            if key not in UI_LABELS:
                continue
            shown = value[0] if isinstance(value, list) and value else value
            lines.append(f"  {UI_LABELS.get(key, key)}: {shown}   ({key})")
    else:
        lines.append("")
        lines.append(
            "Ingen filamentprofil skrevs - temperatur och fläkt beror på vilket\n"
            "filament du kör. Ange filamentprofilens namn och dess vanliga\n"
            "temperatur i programmet, så skrivs den också."
        )
    return "\n".join(lines) + "\n"


def write_profiles(
    load: LoadCase,
    out_dir: str | Path,
    base_profile: str,
    name: str = "Bärande delar",
    nozzle_mm: float = 0.4,
    filament_base: str = "",
    normal_temp_c: float = 0.0,
    extruder_variants: list[str] | None = None,
    process_overrides: dict | None = None,
    filament_overrides: dict | None = None,
) -> ProfileBundle:
    """Skriv profilerna till `out_dir` och berätta vad som inte gick att göra."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(name)

    files: list[Path] = []
    notes: list[str] = []

    process = process_profile(load, base_profile, name, nozzle_mm, overrides=process_overrides)
    process_path = out_dir / f"{stem} - process.json"
    process_path.write_text(
        json.dumps(process, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    files.append(process_path)

    filament = None
    if filament_base.strip() and normal_temp_c > 0:
        filament = filament_profile(
            load,
            filament_base,
            normal_temp_c,
            name,
            extruder_variants=extruder_variants,
            overrides=filament_overrides,
        )
        filament_path = out_dir / f"{stem} - filament.json"
        filament_path.write_text(
            json.dumps(filament, indent=4, ensure_ascii=False) + "\n",
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

    # Listan att knappa in för hand. Den fungerar även när importen inte gör
    # det, och kostar ingenting att skriva.
    text_path = out_dir / f"{stem} - inställningar.txt"
    text_path.write_text(settings_text(process, filament), encoding="utf-8")
    files.append(text_path)
    notes.append(
        f"Importen bygger på profilen {process['inherits']!r} - det namnet måste "
        "finnas i slicern, exakt så. Går importen inte igenom står alla värden i "
        f"{text_path.name} att skriva in för hand."
    )
    return ProfileBundle(files=files, notes=notes)


# --------------------------------------------------------------------------
# Kontroll: samma krav som slicerns import
# --------------------------------------------------------------------------

_SEMVER = re.compile(r"^\d+\.\d+\.\d+(\.\d+)?([-+][0-9A-Za-z.-]+)?$")
_ID_FIELDS = ("printer_settings_id", "print_settings_id", "filament_settings_id")
#: Hör till leverantörsprofiler; slicern plockar bort dem ur användarprofiler.
_VENDOR_ONLY = ("type", "instantiation", "setting_id")


def validate_profile(data: dict, known_profiles: set[str] | None = None) -> list[str]:
    """Det som skulle få slicerns import att vägra, som en lista med skäl.

    Efterliknar kontrollerna i `import_json_presets` och `load_from_json`
    (se modulens docstring). En tom lista betyder att filen går igenom.
    `known_profiles` är namnen som finns i slicern; anges de kontrolleras att
    `inherits` pekar på något som finns - det vanligaste felet.
    """
    problems: list[str] = []

    version = data.get("version")
    if not isinstance(version, str) or not _SEMVER.match(version):
        problems.append(f"'version' saknas eller går inte att tolka: {version!r}")

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append("'name' saknas")
    elif any(ch in name for ch in '/\\:*?"<>|'):
        problems.append(f"'name' innehåller sökvägstecken: {name!r}")

    ids = [key for key in _ID_FIELDS if key in data]
    if not ids:
        problems.append("Inget id-fält - slicern säger 'Preset type is unknown'")
    elif len(ids) > 1:
        problems.append(f"Flera id-fält: {', '.join(ids)}")

    for key in _VENDOR_ONLY:
        if key in data:
            problems.append(f"'{key}' hör till leverantörsprofiler")

    for key, value in data.items():
        if isinstance(value, str):
            continue
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            continue
        problems.append(f"'{key}' är varken en sträng eller en lista med strängar")

    inherits = data.get("inherits", "")
    if not isinstance(inherits, str) or not inherits.strip():
        problems.append("'inherits' saknas - resten av inställningarna skulle bli påhittade")
    elif known_profiles is not None and inherits not in known_profiles:
        problems.append(f"Basprofilen {inherits!r} finns inte i slicern")
    return problems


# --------------------------------------------------------------------------
# Installerade slicers
# --------------------------------------------------------------------------

#: Datamappens namn under ~/.config, och namnet att visa. Orca-Flashforge får
#: -beta/-alpha på förhandsversioner (GUI_App.cpp rad 2651-2658).
SLICER_APPS = (
    ("Orca-Flashforge", "Orca-Flashforge"),
    ("Orca-Flashforge-beta", "Orca-Flashforge beta"),
    ("Orca-Flashforge-alpha", "Orca-Flashforge alpha"),
    ("OrcaSlicer", "OrcaSlicer"),
    ("BambuStudio", "Bambu Studio"),
)

DEFAULT_USER_FOLDER = "default"


@dataclass
class SlicerInstall:
    """En slicer som har körts på den här datorn."""

    app_key: str
    label: str
    data_dir: Path
    flatpak: bool = False
    _profiles: dict[str, dict[str, dict]] = field(default_factory=dict, repr=False)

    @property
    def display_name(self) -> str:
        return f"{self.label} (Flatpak)" if self.flatpak else self.label

    def _conf(self) -> dict:
        path = self.data_dir / f"{self.app_key}.conf"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @property
    def preset_folder(self) -> str:
        """Undermappen under user/ som slicern läser just nu."""
        app = self._conf().get("app", {})
        folder = str(app.get("preset_folder", "") or "") if isinstance(app, dict) else ""
        return folder or DEFAULT_USER_FOLDER

    @property
    def user_dir(self) -> Path:
        return self.data_dir / "user" / self.preset_folder

    @property
    def selected_printer(self) -> str:
        """Skrivarprofilen som var vald när slicern stängdes, eller tomt."""
        presets = self._conf().get("presets", {})
        name = str(presets.get("machine", "") or "") if isinstance(presets, dict) else ""
        if not name:
            return ""
        # En egen skrivarprofil ärver från systemets - det är den som står i
        # profilernas compatible_printers.
        machines = self.profiles("machine")
        if name in machines:
            return name
        user_file = self.user_dir / "machine" / f"{name}.json"
        try:
            parent = json.loads(user_file.read_text(encoding="utf-8")).get("inherits", "")
        except (OSError, ValueError, AttributeError):
            parent = ""
        return parent or name

    def profiles(self, kind: str) -> dict[str, dict]:
        """Systemprofilerna av en sort ("process", "filament", "machine")."""
        if kind not in self._profiles:
            self._profiles[kind] = _read_system_profiles(self.data_dir / "system", kind)
        return self._profiles[kind]

    def user_profiles(self, kind: str) -> dict[str, dict]:
        """Egna profiler av en sort, ur användarmappen.

        Slicern tar namnet från filnamnet (Preset.cpp rad 1604), så det gör
        vi också. Filer som inte går att läsa hoppas över, som i slicern.
        """
        key = f"user:{kind}"
        if key not in self._profiles:
            found: dict[str, dict] = {}
            folder = self.user_dir / kind
            if folder.is_dir():
                for path in sorted(folder.glob("*.json")):
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if isinstance(data, dict):
                        found[path.stem] = data
            self._profiles[key] = found
        return self._profiles[key]

    def all_profiles(self, kind: str) -> dict[str, dict]:
        """System- och egna profiler, för att följa arvskedjor genom båda."""
        return {**self.profiles(kind), **self.user_profiles(kind)}


def _config_home(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("XDG_CONFIG_HOME") or (Path(env.get("HOME") or Path.home()) / ".config"))


def find_slicers(home: Path | None = None, env: dict | None = None) -> list[SlicerInstall]:
    """Slicers vars datamapp har en system-mapp, alltså som har körts.

    Letar i ``$XDG_CONFIG_HOME`` (eller ``~/.config``) och i Flatpaks
    ``~/.var/app/*/config``.
    """
    home = Path(home) if home is not None else Path.home()
    if env is None and home != Path.home():
        env = {"HOME": str(home)}
    roots: list[tuple[Path, bool]] = [(_config_home(env), False)]
    flatpak_apps = home / ".var" / "app"
    if flatpak_apps.is_dir():
        roots += [(p / "config", True) for p in sorted(flatpak_apps.iterdir()) if p.is_dir()]

    found: list[SlicerInstall] = []
    for root, flatpak in roots:
        for app_key, label in SLICER_APPS:
            data_dir = root / app_key
            if (data_dir / "system").is_dir():
                found.append(SlicerInstall(app_key, label, data_dir, flatpak))
    return found


def _read_system_profiles(system_dir: Path, kind: str) -> dict[str, dict]:
    """Alla profiler av en sort ur leverantörsindexen i system/.

    Varje leverantör har en ``<Leverantör>.json`` med listor över profilerna
    och var de ligger (``process_list`` med ``sub_path``).
    """
    list_key = {"process": "process_list", "filament": "filament_list", "machine": "machine_list"}[
        kind
    ]
    out: dict[str, dict] = {}
    if not system_dir.is_dir():
        return out
    for index in sorted(system_dir.glob("*.json")):
        try:
            entries = json.loads(index.read_text(encoding="utf-8")).get(list_key, [])
        except (OSError, ValueError, AttributeError):
            continue
        vendor_dir = system_dir / index.stem
        for entry in entries:
            if not isinstance(entry, dict) or "sub_path" not in entry:
                continue
            try:
                data = json.loads((vendor_dir / entry["sub_path"]).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                out[str(data.get("name") or entry.get("name", ""))] = data
    return out


def inherited_value(profiles: dict[str, dict], name: str, key: str):
    """Värdet på `key`, uppslaget längs arvskedjan."""
    seen: set[str] = set()
    while name in profiles and name not in seen:
        seen.add(name)
        data = profiles[name]
        if data.get(key):
            return data[key]
        name = data.get("inherits", "")
    return None


def user_profile_names(install: SlicerInstall, kind: str, printer: str = "") -> list[str]:
    """Egna profiler som bygger på något i slicern, filtrerade på skrivaren.

    En egen profil saknar ofta ``compatible_printers`` och ärver den från
    systemprofilen, så den slås upp genom hela kedjan.
    """
    everything = install.all_profiles(kind)
    names = sorted(install.user_profiles(kind))
    if not printer:
        return names
    out = []
    for name in names:
        compatible = inherited_value(everything, name, "compatible_printers")
        if not compatible or printer in compatible:
            out.append(name)
    return out


def resolve_base(install: SlicerInstall | None, kind: str, name: str) -> tuple[str, dict]:
    """Systemprofilen att ärva från, och de egna inställningar som ska med.

    Väljer man en egen profil ("Synology hylla") som bas går det inte att
    låta den nya profilen ärva från den: slicern läser användarmappen i
    godtycklig ordning och hoppar tyst över en profil vars förälder inte är
    inläst än (Preset.cpp rad 1676-1680). I stället följs kedjan upp till
    systemprofilen, och den egna profilens inställningar kopieras in - så
    blir den nya profilen den egna plus hållfastheten.
    """
    name = name.strip()
    if install is None or name not in install.user_profiles(kind):
        return name, {}
    system = install.profiles(kind)
    users = install.user_profiles(kind)
    chain: list[dict] = []
    current = name
    seen: set[str] = set()
    while current in users and current not in seen:
        seen.add(current)
        chain.append(users[current])
        current = str(users[current].get("inherits", "") or "")
    if not current:
        raise ProfileError(
            f"Den egna profilen {name!r} bygger inte på någon av slicerns "
            "profiler, så det finns inget att ärva resten från. Välj en av "
            "slicerns egna profiler i stället."
        )
    if current not in system:
        raise ProfileError(
            f"Den egna profilen {name!r} bygger på {current!r}, som inte finns "
            "bland slicerns systemprofiler."
        )
    overrides: dict = {}
    for data in reversed(chain):  # närmast systemet först, den valda sist
        overrides.update(_clean_overrides(data))
    return current, overrides


def system_profiles(install: SlicerInstall, kind: str, printer: str = "") -> list[str]:
    """Namnen i slicerns rullgardin, filtrerade på skrivaren när det går.

    Bara profiler med ``instantiation: true`` syns i slicern. Profiler som
    uttryckligen gäller `printer` kommer först; finns det sådana visas bara
    de, annars alla.
    """
    profiles = install.profiles(kind)
    visible = sorted(n for n, d in profiles.items() if str(d.get("instantiation")) == "true")
    if not printer:
        return visible
    matching = [
        n for n in visible if printer in (inherited_value(profiles, n, "compatible_printers") or [])
    ]
    return matching or visible


def extruder_variants(install: SlicerInstall, filament: str) -> list[str]:
    """Basprofilens ``filament_extruder_variant``, eller tom lista."""
    value = inherited_value(install.all_profiles("filament"), filament, "filament_extruder_variant")
    return [str(v) for v in value] if isinstance(value, list) else []


def user_profile_path(install: SlicerInstall, kind: str, name: str) -> Path:
    """Var slicern själv sparar en användarprofil med det här namnet.

    Namnet tas från filnamnet när slicern läser mappen, så filen måste heta
    exakt som profilen.
    """
    if kind not in ("process", "filament"):
        raise ValueError(kind)
    path = install.user_dir / kind / f"{_safe_name(name)}.json"
    system = (install.data_dir / "system").resolve()
    if system in path.resolve().parents:  # pragma: no cover - skydd
        raise ProfileError("Vägrar skriva i slicerns system-mapp.")
    return path


def install_profiles(install: SlicerInstall, profiles: list[dict]) -> list[Path]:
    """Skriv profilerna rakt in i slicerns användarmapp.

    Anroparen frågar om lov innan en befintlig fil skrivs över - den här
    funktionen skriver det den får.
    """
    written: list[Path] = []
    for data in profiles:
        kind = "process" if "print_settings_id" in data else "filament"
        path = user_profile_path(install, kind, data["name"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(path)
    return written
