# STL Cutter

Delar upp STL- och 3MF-modeller som är för stora för 3D-skrivarens byggplatta,
så att varje del får plats. Verktyget körs lokalt på Linux (utvecklat på Kubuntu).

Just nu (fas 1) görs **raka, plana snitt** utan fogar. Analys av snittytor,
rekommenderade fogtyper, foggeometri och ett grafiskt gränssnitt kommer i
kommande faser.

## Installation (Kubuntu)

```bash
sudo apt install python3-venv python3-pip
git clone https://github.com/fgillinger-ui/stl-cutter.git
cd stl-cutter
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

Kontrollera att allt fungerar:

```bash
python -m stl_cutter.cli --list-printers
pytest
```

## Användning

Kapa en modell:

```bash
python -m stl_cutter.cli cut modell.stl --printer "Bambu P1S" --out ./ut
```

Resultatet i `./ut` blir `part_01.stl`, `part_02.stl` … plus `split_report.json`
med planen, mått och volym per del.

Se bara planen utan att kapa:

```bash
python -m stl_cutter.cli cut modell.stl --printer "Prusa MK4" --out ./ut --dry-run
```

Lista skrivarprofiler:

```bash
python -m stl_cutter.cli --list-printers
```

Efter `pip install -e .` finns även kommandot `stl-cutter` direkt i skalet.

### Flaggor

| Flagga | Betydelse |
|--------|-----------|
| `--printer NAMN` | Skrivarprofil. Delvis namn räcker: `"Bambu P1S"` hittar `Bambu Lab P1S`. |
| `--out MAPP` | Målmapp för delarna (skapas om den saknas). |
| `--dry-run` | Skriv bara `split_report.json`, kapa inte. |
| `--no-orient` | Rotera inte modellen automatiskt för bästa passform. |
| `--margin MM` | Överstyr profilens marginal. |
| `--format stl\|3mf` | Filformat för delarna (3MF faller tillbaka till STL om stöd saknas). |
| `-v` | Utförlig loggning. |

## Skrivarprofiler

Inbyggda profiler ligger i `data/printers.json`: Bambu Lab P1S, Bambu Lab X1C,
Prusa MK4, Ender 3 och Custom. Varje profil har byggmått (`bed_x/y/z`), en
`margin_mm` (standard 5 mm) som hålls fri runt varje del, och `clearance_mm`
(standard 0,15 mm) som används för fogtoleranser i senare faser.

Lägg till en egen profil — den sparas i `~/.config/stl-cutter/printers.json` och
skriver inte över de inbyggda:

```bash
python -m stl_cutter.cli printers --add "Min skrivare" --bed 300 300 400 --margin 8
```

## Hur delningen fungerar

1. Modellen läses in, dubblerade vertices slås ihop, hål fylls om möjligt och
   det rapporteras om meshen är hel (watertight).
2. Modellen roteras till den orientering som ger minst antal delar (rotationer
   i 15°-steg runt X/Y/Z samt en PCA-baserad orientering).
3. Antal delar per axel räknas ut som `ceil(storlek / (byggmått − 2·marginal))`.
4. Snitten utförs med lock på snittytan, och volymen jämförs mot originalet
   (avvikelsen ska vara under 0,5 %).

Tekniska detaljer finns i [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Felsökning

* **"Meshen är inte watertight"** — modellen har hål. Delarna går ofta ändå att
  kapa, men kontrollera resultatet. Reparera gärna i t.ex. Blender först.
* **Delar får fortfarande inte plats** — sänk `--margin` eller kontrollera att
  rätt skrivarprofil används.
* **Färre delar än planen anger** — modellen har hålrum, så vissa celler i
  rutnätet blir tomma. Det är förväntat.
