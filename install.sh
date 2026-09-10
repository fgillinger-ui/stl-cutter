#!/usr/bin/env bash
#
# Installerar STL Cutter på Kubuntu/Ubuntu.
#
# Skriptet är idempotent: kör det igen för att uppdatera en befintlig
# installation. Ingenting utanför de mappar som skrivs ut nedan rörs.
#
#   ./install.sh              installera eller uppdatera
#   ./install.sh --uninstall  ta bort installationen
#
set -euo pipefail

APP_NAME="stl-cutter"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/$APP_NAME"
VENV_DIR="$DATA_DIR/venv"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILE="$DESKTOP_DIR/$APP_NAME.desktop"
BIN_DIR="$HOME/.local/bin"

# Systempaket som Qt och venv behöver. Vi letar efter det som faktiskt saknas
# i stället för att fråga dpkg om paketnamn - namnen skiljer sig mellan
# utgåvor, och paketen kan vara installerade under ett annat namn.
QT_LIBRARIES=(libEGL.so.1 libGL.so.1 libxkbcommon-x11.so.0)
QT_PACKAGES=(libegl1 libgl1 libxkbcommon-x11-0)

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mFel:\033[0m %s\n' "$*" >&2; exit 1; }

uninstall() {
    say "Tar bort STL Cutter"
    rm -rf "$VENV_DIR"
    rm -f "$DESKTOP_FILE" "$ICON_DIR/$APP_NAME.svg" "$BIN_DIR/stl-cutter" "$BIN_DIR/stl-cutter-gui"
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
    fi
    echo
    echo "Borttaget:"
    echo "  $VENV_DIR"
    echo "  $DESKTOP_FILE"
    echo "  $ICON_DIR/$APP_NAME.svg"
    echo "  genvägarna i $BIN_DIR"
    echo
    echo "Kvar (raderas inte automatiskt, de innehåller dina egna data):"
    echo "  ${XDG_CONFIG_HOME:-$HOME/.config}/$APP_NAME   inställningar och egna skrivarprofiler"
    echo "  $DATA_DIR/log.txt                             loggfil"
    echo "  $SOURCE_DIR                                   källkoden"
    exit 0
}

[[ "${1:-}" == "--uninstall" ]] && uninstall
[[ "${1:-}" == "--help" || "${1:-}" == "-h" ]] && { sed -n '3,9p' "$0" | sed 's/^# \?//'; exit 0; }

[[ -f "$SOURCE_DIR/pyproject.toml" ]] || die "Kör skriptet från stl-cutter-mappen."

# --- 1. Systempaket -------------------------------------------------------
# Vilka bibliotek saknas? Vi provar dem i stället för att lita på paketnamn.
missing_packages() {
    local i
    for i in "${!QT_LIBRARIES[@]}"; do
        if ! ldconfig -p 2>/dev/null | grep -q "${QT_LIBRARIES[$i]}"; then
            printf '%s\n' "${QT_PACKAGES[$i]}"
        fi
    done
    python3 -m venv --help >/dev/null 2>&1 || printf '%s\n' python3-venv
}

missing=()
mapfile -t missing < <(missing_packages)

if (( ${#missing[@]} )); then
    say "Saknar systempaket: ${missing[*]}"
    if command -v sudo >/dev/null 2>&1 || [[ $EUID -eq 0 ]]; then
        # Ett trasigt paketarkiv får inte stoppa hela installationen - resten
        # av programmet fungerar ändå, bara utan grafiskt gränssnitt.
        apt_cmd=(apt-get)
        [[ $EUID -ne 0 ]] && apt_cmd=(sudo apt-get)
        "${apt_cmd[@]}" update -qq || warn "apt-get update misslyckades - fortsätter ändå."
        "${apt_cmd[@]}" install -y "${missing[@]}" || warn "Kunde inte installera ${missing[*]}."
    else
        warn "Hittar inte sudo. Installera själv: sudo apt install ${missing[*]}"
    fi

    still_missing=()
    mapfile -t still_missing < <(missing_packages)
    if (( ${#still_missing[@]} )); then
        warn "Följande saknas fortfarande: ${still_missing[*]}"
        warn "Kommandoraden fungerar, men det grafiska gränssnittet startar inte."
        warn "Installera dem med: sudo apt install ${still_missing[*]}"
        GUI_LIBS_MISSING=1
    fi
else
    say "Systembiblioteken finns redan"
fi
GUI_LIBS_MISSING="${GUI_LIBS_MISSING:-0}"

# --- 2. Virtuell miljö ----------------------------------------------------
if [[ -x "$VENV_DIR/bin/python" ]]; then
    say "Använder befintlig miljö i $VENV_DIR"
else
    say "Skapar virtuell miljö i $VENV_DIR"
    mkdir -p "$DATA_DIR"
    python3 -m venv "$VENV_DIR"
fi

say "Installerar programmet och dess beroenden"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet --upgrade -r "$SOURCE_DIR/requirements.txt"
"$VENV_DIR/bin/pip" install --quiet --upgrade -e "$SOURCE_DIR"

# --- 3. Ikon --------------------------------------------------------------
say "Lägger ikonen i $ICON_DIR"
mkdir -p "$ICON_DIR"
cp "$SOURCE_DIR/assets/$APP_NAME.svg" "$ICON_DIR/$APP_NAME.svg"

# --- 4. Menypost ----------------------------------------------------------
say "Skapar menypost i $DESKTOP_FILE"
mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_FILE" <<DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=STL Cutter
GenericName=Modelldelare för 3D-utskrift
Comment=Dela upp STL- och 3MF-modeller så att delarna får plats på byggplattan
Exec=$VENV_DIR/bin/stl-cutter-gui
Icon=$APP_NAME
Terminal=false
Categories=Graphics;3DGraphics;Utility;
Keywords=STL;3MF;3D;utskrift;kapa;dela;
StartupNotify=true
DESKTOP
chmod 644 "$DESKTOP_FILE"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
fi

# --- 5. Genvägar i PATH ---------------------------------------------------
say "Länkar kommandona i $BIN_DIR"
mkdir -p "$BIN_DIR"
ln -sf "$VENV_DIR/bin/stl-cutter" "$BIN_DIR/stl-cutter"
ln -sf "$VENV_DIR/bin/stl-cutter-gui" "$BIN_DIR/stl-cutter-gui"

# --- 6. Kontroll ----------------------------------------------------------
say "Kontrollerar installationen"
"$VENV_DIR/bin/stl-cutter" --list-printers >/dev/null || die "Programmet startar inte. Se felet ovan."

# Kapa en liten testkropp på riktigt. Ett saknat beroende märks först här -
# att programmet startar betyder inte att det kan göra sitt jobb.
smoke_dir="$(mktemp -d)"
trap 'rm -rf "$smoke_dir"' EXIT
if ! "$VENV_DIR/bin/python" - "$smoke_dir" >"$smoke_dir/log" 2>&1 <<'SMOKE'
import sys
import trimesh
from stl_cutter.cli import main

out = sys.argv[1]
trimesh.creation.box(extents=[400, 120, 60]).export(f"{out}/test.stl")
raise SystemExit(main(["cut", f"{out}/test.stl", "--printer", "Ender 3", "--out", f"{out}/parts"]))
SMOKE
then
    cat "$smoke_dir/log" >&2
    die "Programmet kunde inte kapa en testmodell. Se felet ovan."
fi
say "Testkapning fungerar"
if [[ "$GUI_LIBS_MISSING" == "0" ]]; then
    if ! QT_QPA_PLATFORM=offscreen "$VENV_DIR/bin/python" -c 'import stl_cutter.gui' >/dev/null 2>&1; then
        warn "Det grafiska gränssnittet gick inte att importera. Kommandoraden fungerar."
        GUI_LIBS_MISSING=1
    fi
fi

# --- 7. Sammanfattning ----------------------------------------------------
version="$("$VENV_DIR/bin/python" -c 'import stl_cutter; print(stl_cutter.__version__)' 2>/dev/null || echo okänd)"

echo
say "Klart. STL Cutter $version är installerat."
echo
echo "Detta gjordes:"
echo "  Virtuell miljö   $VENV_DIR"
echo "  Menypost         $DESKTOP_FILE"
echo "  Ikon             $ICON_DIR/$APP_NAME.svg"
echo "  Kommandon        $BIN_DIR/stl-cutter, $BIN_DIR/stl-cutter-gui"
echo
echo "Så här startar du:"
if [[ "$GUI_LIBS_MISSING" == "0" ]]; then
    echo "  Sök efter \"STL Cutter\" i programmenyn, eller kör"
    echo "    stl-cutter-gui        grafiskt gränssnitt"
else
    echo "  (Det grafiska gränssnittet saknar bibliotek - se varningarna ovan.)"
fi
echo "    stl-cutter --help     kommandoraden"
echo
if ! printf '%s' ":$PATH:" | grep -q ":$BIN_DIR:"; then
    warn "$BIN_DIR ligger inte i din PATH."
    warn "Lägg till raden nedan sist i ~/.bashrc och starta om terminalen:"
    warn "    export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
echo "Inställningar sparas i ${XDG_CONFIG_HOME:-$HOME/.config}/$APP_NAME"
echo "Logg skrivs till $DATA_DIR/log.txt"
echo "Avinstallera med: ./install.sh --uninstall"
