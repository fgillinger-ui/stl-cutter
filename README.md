# STL Cutter

Delar upp STL- och 3MF-modeller som är för stora för 3D-skrivarens byggplatta,
så att varje del får plats. Verktyget körs lokalt på Linux (utvecklat på Kubuntu).

Programmet analyserar varje snittyta, **väljer en lämplig fogtyp** (laxstjärt,
styrpinnar, pusselprofil, skruv eller plan limfog) med motivering på svenska,
och **bygger fogen i geometrin** — så att delarna går att passa ihop och limma
eller skruva.

Det kan också **ändra ett mått** på modellen — göra lådan 550 mm djup i stället
för 250 — utan att godstjocklek, hörnradier eller hål förändras. Se
[docs/RESIZE.md](docs/RESIZE.md).

Det finns både ett **grafiskt gränssnitt** och ett kommandoradsverktyg.

## Installation (Kubuntu)

```bash
git clone https://github.com/fgillinger-ui/stl-cutter.git
cd stl-cutter
./install.sh
```

Det är allt. Skriptet gör följande och skriver ut vad det gjorde:

* installerar systembibliotek som Qt behöver (frågar efter ditt lösenord)
* skapar en egen miljö i `~/.local/share/stl-cutter/venv` — den rör inte
  systemets Python
* lägger programmet i menyn under **STL Cutter** med ikon
* gör kommandona `stl-cutter` och `stl-cutter-gui` tillgängliga
* kapar en testmodell för att kontrollera att allt verkligen fungerar

Kör skriptet igen när du hämtat en ny version — det är gjort för att köras om.
Ligger inte `~/.local/bin` i din `PATH` säger skriptet till hur du fixar det.

### Avinstallera

```bash
./install.sh --uninstall
```

Dina inställningar och egna skrivarprofiler i `~/.config/stl-cutter` lämnas
kvar. Ta bort den mappen själv om du vill bli av med dem också.

### Installera för hand

Föredrar du att göra det själv:

```bash
sudo apt install python3-venv python3-pip libegl1 libgl1 libxkbcommon-x11-0
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

Kontrollera att allt fungerar:

```bash
stl-cutter --list-printers
pytest
```

## Använda det grafiska gränssnittet

Sök efter **STL Cutter** i programmenyn, eller kör i en terminal:

```bash
stl-cutter-gui
```

Fönstret har en panel till vänster med **en flik per steg** — 1. Modell,
1c. Hål, 2. Skrivare, 3. Snitt, 4. Delar, 5. Exportera — och en 3D-vy till
höger. Varje
flik får plats i sin helhet, så ingenting hamnar utanför fönsterkanten.

1. **Modell** — klicka *Öppna fil…* eller dra en STL- eller 3MF-fil in i
   fönstret. Programmet visar mått, volym och om modellen är hel. Kryssrutan
   *Öppna den senaste modellen vid start* gör att programmet tar upp den fil du
   höll på med förra gången, i stället för att du får leta upp den igen. Är
   filen flyttad står det, och programmet startar tomt.
1b. **Ändra mått** — behöver modellen vara större eller mindre? Skriv in de
   önskade måtten. Klicka *Visa var modellen kan sträckas* för att se de gröna
   partierna där tvärsnittet är konstant — det är där materialet skjuts in.
   Godstjocklek, hörnradier och hål behåller sina mått. Hittas inget sådant
   parti får du en varning och kan välja skalning i stället, med vetskap om att
   hål då blir ovala. *Ångra* lägger tillbaka originalet.
1c. **Hål** — behöver modellen hål? Klicka *Placera hål i vyn* och peka på
   modellen: hålet borras vinkelrätt in i ytan du pekar på. Eller *Lägg till
   hål* och skriv in X, Y, Z, diameter och djup i tabellen. Välj skruv (M3–M6)
   så sätts diametern och försänkningen efter den, eller ange ett eget mått.
   Hålen borras **före** snitten, så att ett hål tvärs över ett snitt hamnar i
   båda delarna. *Ångra borrning* lägger tillbaka modellen. Se
   [docs/HOLES.md](docs/HOLES.md).
2. **Skrivare** — välj din skrivare i listan. Måtten fylls i automatiskt men går
   att ändra. Har du en skrivare som inte finns med: skriv in måtten och klicka
   *Spara som ny profil*.
3. **Montering** — ska delarna limmas ihop för gott, eller kunna tas isär igen?
   Valet styr vilka fogtyper som föreslås. Toleransen är spelet i fogen; större
   värde ger lösare passning.
3b. **Belastning** — ska delen bära något? Kryssa i rutan och ange vikten.
   Programmet gissar upphängningen ur formen, visar gissningen med sitt skäl
   så att du kan rätta den, och lägger sedan snitten där böjmomentet är minst.
   Knappen *Utskriftsinställningar för styrka…* ger inställningarna för
   slicern. Se [Delar som ska bära något](#delar-som-ska-bära-något).
4. **Förslag** — klicka *Analysera*. Du får en tabell med ett snitt per rad:
   var det ligger, vilken fogtyp programmet föreslår och varför. Markera en rad
   för att läsa hela motiveringen och se de näst bästa alternativen.

   **Du bestämmer själv.** Varje rad går att ändra:

   * **Axel** — vilket håll snittet går i (X, Y eller Z)
   * **Position** — var snittet ligger, i millimeter. Snittplanet i 3D-vyn
     följer med medan du ändrar värdet.
   * **Fogtyp** — välj fritt, oavsett vad programmet föreslog

   Under tabellen finns tre inställningar för det markerade snittet:

   * **Lutning** — vinkla snittet ett exakt antal grader kring en vald axel.
     0 betyder rakt. Samma sak går att göra på fri hand med Shift och dra i
     planet.
   * **Stoppkant** — bara för laxstjärt. Stänger botten på spåret så att delen
     glider in och tar emot mot material i stället för att bara hållas av
     friktion. Se [docs/JOINTS.md](docs/JOINTS.md) för en bild.
   * **Styrpinnar Ø** — pinnarnas tjocklek. Programmet föreslår en diameter ur
     godstjockleken; här kan du prova dig fram. Gäller både fogtypen
     styrpinnar och styrpinnar som lagts till som komplement till en annan
     fog. På kommandoraden: `--pin-diameter MM`.

   Laxstjärtar i samma snitt skjuts alltid ihop **åt samma håll** — riktningen
   räknas ut för hela snittet och skrivs i loggen ("delarna skjuts ihop längs
   Z"). I förhandsgranskningen målas fogytorna orange, så att du ser vad som
   ska möta vad.

   **Eller ta tag i snittet direkt i 3D-vyn:**

   | Gör så här | Vad som händer |
   |-----------|----------------|
   | Dra i ett snittplan | Flyttar snittet längs sin egen riktning |
   | **Shift** + dra i ett snittplan | Vinklar snittet — du kan kapa snett |
   | Dra vid sidan om planen | Vrider modellen som vanligt |

   Tabellen och 3D-vyn hålls i takt: drar du planet ändras siffran, skriver du
   en siffra flyttas planet.

   Knapparna under tabellen: *Lägg till snitt* placerar ett nytt snitt mitt på
   modellens längsta sida, *Ta bort snitt* tar bort det markerade, *Räta upp*
   tar bort lutningen på ett vinklat snitt, och *Räkna ut åt mig* kastar dina
   snitt och tar tillbaka programmets förslag.

   Du behöver inte börja med *Analysera* — klicka *Lägg till snitt* direkt så
   placerar du alla snitt själv från början.

   Raden under knapparna visar hela tiden hur många delar planen ger, hur stor
   den största blir, och varnar i rött om någon del inte får plats på
   byggplattan.
4b. **Delar** — efter en förhandsgranskning listas varje del med sitt mått.
   Skriv ett **namn** på raden så heter filen det vid export ("vänster gavel"
   i stället för `part_02`); tecken som inte går i ett filnamn byts mot `_`,
   och två delar med samma namn får ett löpnummer så att ingen fil skriver
   över en annan. Klicka på **färgrutan** för att välja delens färg i 3D-vyn,
   och *Fogens färg…* för färgen på ytorna som ska mötas. *Återställ färgerna*
   tar tillbaka programmets egna.
5. **Kapa och exportera** — klicka *Förhandsgranska* för att se resultatet först:
   modellen kapas i minnet, delarna visas isärdragna i 3D-vyn och du får de
   verkliga måtten — **inga filer skrivs**. Ser det rätt ut, välj målmapp och
   klicka *Kapa och exportera*. Har du redan förhandsgranskat skrivs samma
   resultat ut direkt, utan att räknas om.

I 3D-vyn ser du modellen, snittplanen som orange plan, och efter kapningen
delarna i olika färger. Dra i reglaget *Spräng isär* för att se fogarna, och
kryssa i *Visa byggplatta* för att se skrivarens plattstorlek som rutnät —
plattan läggs mitt under det som visas, oavsett var modellen har sina
koordinater.
Kryssrutan *Ljus bakgrund* växlar mellan ljus och mörk vy — valet sparas.

### Att se runt modellen

| Gör så här | Vad som händer |
|-----------|----------------|
| Dra i ett **snittplan** | Flyttar snittet. Shift+dra vinklar det. |
| Dra med **höger** eller **vänster** musknapp vid sidan om planen | Vrider modellen så att du kan se den från alla håll |
| **Mushjulet** | Zoomar in och ut |
| **Mittenknappen**, eller **Ctrl** + dra | Flyttar vyn i sidled |
| **Högerklick** utan att dra | Meny med färdiga vinklar: framifrån, ovanifrån, från sidan, samt *Anpassa till modellen* |

Analys och kapning kan ta någon minut på stora modeller. Det går alltid att
trycka *Avbryt* — fönstret slutar aldrig svara. Meddelanden visas i rutan
längst ner; den fullständiga loggen skrivs till
`~/.local/share/stl-cutter/log.txt`. Dina inställningar sparas i
`~/.config/stl-cutter/settings.json`.

## Använda kommandoraden

Kapa en modell:

```bash
stl-cutter cut modell.stl --printer "Bambu P1S" --out ./ut
```

Resultatet i `./ut` blir `part_01.stl`, `part_02.stl` … plus `split_report.json`
med planen, mått och volym per del.

Se förslagen på fogtyper med motivering:

```bash
stl-cutter cut modell.stl --printer "Bambu P1S" --out ./ut --explain
```

Ska delarna kunna skruvas isär igen i stället för att limmas:

```bash
stl-cutter cut modell.stl --printer "Bambu P1S" --out ./ut \
    --assembly demountable --explain
```

Ändra ett mått utan att deformera modellen:

```bash
stl-cutter analyze-spans modell.stl --axis y
stl-cutter resize modell.stl --y 550 --out modell_550.stl
```

Innehåller filen flera objekt som hör ihop — en hylla och dess bakplatta, en
låda och dess lock — pekar `--part` ut vilket måttet gäller. Övriga objekt får
samma **tillskott** i millimeter, inte samma mått, så att spår och laxstjärtar
fortsätter sitta mitt för varandra:

```bash
stl-cutter resize nas.3mf --x 270 --part 2 --out ut/nas.stl
```

I gränssnittet dyker då en rullgardin **Objekt** upp i rutan *1b. Ändra mått*.
Se [docs/RESIZE.md](docs/RESIZE.md#flera-objekt-i-samma-fil).

Borra hål — skruvhål med försänkning, eller egna mått:

```bash
stl-cutter drill platta.stl --hole 10,0,6 --hole=-10,0,6 --screw M4
stl-cutter drill hylla.stl --hole 0,-30,20 --direction +y --diameter 6 --depth 12
```

Punkten är där hålet börjar, på ytan. Negativa tal skrivs med likhetstecken
(`--hole=-10,0,6`). Se [docs/HOLES.md](docs/HOLES.md).

Delarna skrivs **vända platt** och med **lösa kroppar i egna filer**. Det
första tar bort stödbehovet och lägger utskriftens lager längs delen i stället
för tvärs — en list som står upp knäcks i lagerfogen, samma list liggande
håller flera gånger mer. Det andra gör att en del som föll i flera klumpar blir
en fil per klump, så att varje utskrift går att vända och placera för sig. Båda
går att stänga av: kryssrutorna i rutan *5. Kapa och exportera*, eller
`--no-lay-flat` och `--no-split-bodies` på kommandoraden.

### Delar som ska bära något

Ska delen bära last — en hylla, en konsol — spelar det stor roll *var* snittet
hamnar. En fog är alltid svagare än helt gods, och den ska inte hamna där
böjningen är värst. Kryssa i **Delen ska bära last** i rutan *3b. Belastning*,
ange vikten, och snitten flyttar sig dit modellen är minst belastad:

```bash
stl-cutter cut hylla.stl --printer "Bambu P1S" --out ./ut --load-kg 5
```

Programmet gissar upphängningen ur formen (längsta vågräta axeln är
spännaxeln; den ände som har mest material i tvärsnittet tas för infästningen)
och **skriver ut gissningen med sitt skäl**. Stämmer den inte rättar du den med
`--support cantilever|both_ends`, `--load-axis x|y|z` och `--load-end low|high`,
eller med rullgardinerna i gränssnittet. Fel upphängning vänder momentkurvan
helt, så den gissningen får aldrig gå igenom osedd.

Med en last angiven skrivs också utskriftsinställningar för hållfasthet ut —
väggar, fyllnad, lagerhöjd, temperatur och fläkt, var och en med skälet till
sig. I gränssnittet ligger de bakom knappen **Utskriftsinställningar för
styrka…**.

Inställningarna följer inte med i STL-filen — en STL innehåller bara trianglar.
Men de går att få som en profil slicern kan importera (OrcaSlicer, FlashPrint,
Bambu Studio, Qidi Studio):

```bash
stl-cutter profile --load-kg 5 --base-profile "0.20mm Standard @FF C5" --out ./ut
```

eller knappen **Spara slicerprofil…** i rutan *3b. Belastning*. Namnet på
basprofilen är det som står i slicerns rullgardin — profilen sätter bara det
som rör hållfasthet och ärver resten därifrån. Se
[docs/LOAD.md](docs/LOAD.md#profil-till-slicern).

> **Vad programmet inte gör:** det räknar *inte* ut hur mycket hyllan bär.
> Det kräver en strukturmodell av en godtycklig mesh, och FDM varierar
> dessutom ±50 % med skrivare, material och kylning. Ett tal som ser ut att
> vara beräknat men inte är det är farligare än inget tal alls — det är det
> man hänger upp sin NAS på. Det programmet gör är att *rangordna* snittlägen,
> och den rangordningen beror bara på upphängning och spännvidd. Ska lasten
> vara stor: provbelasta, med marginal.

### Spara arbetet och fortsätta senare

Ett riktigt jobb blir sällan klart på en gång: måtten ändras, snitten flyttas,
fogtypen byts. **Arkiv → Spara projekt…** lägger allt i en fil — modellen,
skrivaren, monteringsvalet, lasten, varje snitt med sin fogtyp och sina
inställningar. **Arkiv → Öppna projekt…** tar upp det precis som det var.

Modellen ligger **med i filen**. Det är avsiktligt: en måttändrad modell finns
inte någon annanstans, och originalet kan flyttas eller ändras i CAD. Filen
(`.stlcut`) blir därmed ungefär lika stor som modellen — den är en vanlig zip
med `project.json` och `model.stl` i, så den går att titta i för hand om något
ser konstigt ut.

Ett sparat projekt går också att köra om från kommandoraden, utan att klicka:

```bash
stl-cutter project hylla.stlcut            # kapar och exporterar som sparat
stl-cutter project hylla.stlcut --info     # visar bara vad projektet innehåller
```

Snitten läggs exakt där de sparades — ingen omplanering. Samma projekt ger
samma delar.

Behöver modellen inte delas — den får plats som den är, eller ska tillbaka in
i CAD — skriver `export` ut den utan att kapa:

```bash
stl-cutter export modell.3mf --out ut/modell.stl
```

Flera objekt i filen blir en fil var (`modell_01.stl`, `modell_02.stl`);
`--merge` lägger allt i samma fil. I gränssnittet heter det **Exportera utan
att dela** i rutan *1b. Ändra mått*, och finns även under **Arkiv**.

Också den exporten **lägger objekten platt**, precis som delarna efter en
kapning — en hyllplatta som står upp i CAD-filen skrivs annars på högkant med
stöd överallt. Ska filen tillbaka in i CAD vill man ha den orörd: kryssa ur
*Vänd delarna platt* eller kör `--no-lay-flat`.

Ändra måttet och kapa i ett svep:

```bash
stl-cutter cut modell.stl --resize-y 550 --printer "Bambu P1S" --out ./ut
```

Se bara planen utan att kapa:

```bash
stl-cutter cut modell.stl --printer "Prusa MK4" --out ./ut --dry-run
```

Lista skrivarprofiler:

```bash
stl-cutter --list-printers
```

Kommandona går också att köra som `python -m stl_cutter.cli` respektive
`python -m stl_cutter.gui` om du hellre vill det.

### Flaggor

| Flagga | Betydelse |
|--------|-----------|
| `--printer NAMN` | Skrivarprofil. Delvis namn räcker: `"Bambu P1S"` hittar `Bambu Lab P1S`. |
| `--out MAPP` | Målmapp för delarna (skapas om den saknas). |
| `--dry-run` | Skriv bara `split_report.json`, kapa inte. |
| `--no-orient` | Rotera inte modellen automatiskt för bästa passform. |
| `--margin MM` | Överstyr profilens marginal. |
| `--assembly glue\|demountable` | Ska delarna limmas permanent eller kunna tas isär? Påverkar vilken fog som föreslås. |
| `--explain` | Skriv analys och motivering för varje snitt på svenska. |
| `--joint TYP` | Tvinga en fogtyp: `none`, `pins`, `dovetail`, `puzzle`, `screw`. Standard är `auto`, som följer rekommendationen per snitt. |
| `--no-joints` | Bygg ingen foggeometri — bara plana snitt. |
| `--no-analysis` | Hoppa över analysen. Snabbare, men snitten läggs jämnt fördelade utan hänsyn till snittytan. |
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
stl-cutter printers --add "Min skrivare" --bed 300 300 400 --margin 8
```

## Hur delningen fungerar

1. Modellen läses in, dubblerade vertices slås ihop, hål fylls om möjligt och
   det rapporteras om meshen är hel (watertight).
2. Modellen roteras till den orientering som ger minst antal delar (rotationer
   i 15°-steg runt X/Y/Z samt en PCA-baserad orientering). Lägen där modellen
   bara balanserar på en smal kant räknas inte: anliggningen mot plattan måste
   vara minst en fjärdedel av den största modellen kan få. En platta som bara
   "får plats" genom att ställas på högkant kapas hellre och skrivs liggande,
   och då står det i planen att valet fanns.
3. Antal delar per axel räknas ut som `ceil(storlek / (byggmått − 2·marginal))`.
4. Runt varje snittläge provas alternativa positioner (±15 % av modellens längd,
   i steg om 2 mm). Varje kandidat mäts — snittarea, antal öar, minsta
   väggtjocklek, rundhet — och poängsätts. Snitt genom tunna väggar, genom många
   separata öar eller som lämnar en nästan tom del undviks. Har du angett en
   last straffas dessutom lägen där böjmomentet är stort, och sökningen får
   leta i ±30 % i stället. Antalet delar ökar aldrig av den här optimeringen.
5. Varje valt snitt får en rekommenderad fogtyp med motivering och de två näst
   bästa alternativen, som hamnar i `split_report.json`.
6. Snitten utförs med lock på snittytan, och volymen jämförs mot originalet
   (avvikelsen ska vara under 0,5 %).
7. Fogarna byggs in i delarna: den ena delen får hanen, den andra honan med
   tolerans. Varje del kontrolleras efter varje steg — går en fog inte att bygga
   provas ett enklare alternativ, och du får en varning i stället för ett
   kraschat program.

## Mer att läsa

* [docs/JOINTS.md](docs/JOINTS.md) — fogtyperna, när de passar och hur du monterar dem
* [docs/HOLES.md](docs/HOLES.md) — borra hål, skruvmåtten och vad som blir trängre i utskrift
* [docs/RESIZE.md](docs/RESIZE.md) — ändra mått utan att deformera godset, och vad som inte fungerar
* [docs/LOAD.md](docs/LOAD.md) — delar som ska bära last, och varför programmet inte räknar ut bärighet
* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — hur programmet är byggt

## Fogtyper som kan föreslås

| Typ | Passar när | Vad det betyder |
|-----|-----------|-----------------|
| `none` | snittet är tunnare än 4 mm | Plan yta som limmas. Ingen fog får plats. |
| `puzzle` | 4–8 mm och platt snitt | Pusselprofil genom hela tjockleken, låser i sidled. |
| `dovetail` | minst 8 mm och avlångt snitt | Laxstjärt som skjuts ihop längs snittet och drar ihop delarna. |
| `pins` | minst 6 mm och rundaktigt snitt | Styrpinnar (dowels) som centrerar delarna. |
| `screw` | demonterbart och minst 12 mm | M3-skruv med mutterficka plus två styrpinnar. |

Är snittytan större än 5 000 mm² kompletteras den valda fogen med två extra
styrpinnar. Toleransen (`clearance_mm`) tas från skrivarprofilen — 0,15 mm för de
flesta skrivare, 0,2 mm för Ender 3.

[docs/JOINTS.md](docs/JOINTS.md) förklarar varje fogtyp närmare **med bilder**:
när den passar, hur delarna monteras och vilken tolerans som brukar fungera.
Samma bilder finns i programmet under knappen *Fogtyper – vad är vad?* i steg 4,
och en miniatyr visas bredvid motiveringen för det snitt du markerat.

### Så monteras de

* **`pins`** — pinnarna sitter fast på ena delen och passar i hål i den andra.
  Tryck ihop, limma om det ska sitta permanent.
* **`dovetail`** — skjuts ihop i sidled, inte rakt på. Laxstjärten är bredare
  längst ut, så delarna kan inte dras isär vinkelrätt mot skarven.
* **`puzzle`** — vågig skarv som låser i sidled. Limmas.
* **`screw`** — lägg M3-muttern i sexkantsfickan innan du sätter ihop delarna,
  skruva sedan från utsidan. Den enda fogen som är gjord för att tas isär igen.

Sitter en fog för hårt eller för löst: justera `clearance_mm` i skrivarprofilen.
Större värde ger lösare passning.

## Felsökning

* **"Modellen har N trasiga kanter"** — modellen är inte en giltig solid.
  Programmet försöker laga den automatiskt: flera kroppar slås ihop till en med
  en boolean union, identiska hörn slås ihop, sprickor smalare än 0,1 mm
  svetsas ihop, och små hål fylls. Står varningen kvar gick
  skadan inte att laga, och delarna ärver hålen. Din slicer kan då rapportera
  *non-manifold edges*.

  Så här lagar du modellen på Linux (slicerns egen "Fix Model" är ofta
  Windows-bara):

  * **Blender** — importera modellen, aktivera tillägget *3D-Print Toolbox*
    under Inställningar → Add-ons, och kör *Make Manifold* i sidopanelen.
    Exportera som STL och kapa den filen i stället.
  * **admesh** — `sudo apt install admesh`, sedan
    `admesh --write-binary-stl=lagad.stl trasig.stl`. Snabbt, men klarar bara STL.
  * **Meshlab** — `sudo apt install meshlab`, filtren *Remove Duplicate Vertices*
    och *Close Holes*.

  Testa alltid delarna i din slicer först — små hål stör ofta inte utskriften.
* **Delar får fortfarande inte plats** — sänk `--margin` eller kontrollera att
  rätt skrivarprofil används.
* **Färre delar än planen anger** — modellen har hålrum, så vissa celler i
  rutnätet blir tomma. Det är förväntat.
* **"Fogen gick inte att bygga - provar ..."** — fogen fick inte plats i
  materialet, så programmet valde ett enklare alternativ. Delarna är fortfarande
  hela och användbara. Vill du styra valet själv, använd `--joint`.
* **Analysen tar tid på stora modeller** — varje kandidatläge kräver ett
  tvärsnitt. Kör med `--no-analysis` för ett snabbt svar, eller `--dry-run`
  för att bara se planen. I GUI:t kan du avbryta när som helst.
* **GUI:t startar inte** — kör `./install.sh` igen; det installerar
  systembiblioteken Qt behöver och säger till om något saknas. Manuellt:
  `sudo apt install libegl1 libgl1 libxkbcommon-x11-0`.
* **Programmet syns inte i menyn** — logga ut och in igen, eller kör
  `update-desktop-database ~/.local/share/applications`.
* **3D-vyn är svart** — datorn saknar fungerande OpenGL-drivrutin. Resten av
  programmet fungerar ändå, och kommandoraden påverkas inte.
