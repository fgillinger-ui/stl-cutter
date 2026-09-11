# Arkitektur

Detta dokument beskriver modulerna och dataklasserna i `stl_cutter`.
**Kommande faser ska läsa och uppdatera den här filen.**

Status: alla fem faser är klara — grundstruktur och plana snitt (1), analys och
rekommendation (2), foggeometri (3), grafiskt gränssnitt (4) samt paketering,
installation och dokumentation (5).

## Teknikval (fastställt)

Python 3.12, `trimesh` (mesh-IO och snitt), `manifold3d` (boolean-motor),
`shapely` (2D-geometri), `numpy`, `scipy` (avståndstransform), `networkx`.
GUI i `PySide6` + `pyqtgraph.opengl` (fas 4). Allt körs lokalt, inga nätverksanrop.
Alla mått är i millimeter internt.

## Modulöversikt

```
stl_cutter/
  cli.py              # kommandorad: cut / printers / --list-printers / --dry-run / --explain
  __main__.py         # python -m stl_cutter
  core/
    mesh_io.py        # ladda och spara STL/3MF, reparera, rapportera watertight
    printers.py       # skrivarprofiler (byggvolym, marginal, tolerans)
    analysis.py       # mät snittytan: area, öar, väggtjocklek, rundhet     [fas 2]
    recommender.py    # välj fogtyp utifrån snittytan och monteringsavsikt  [fas 2]
    planner.py        # orientering, kandidatplan, poängsättning
    cutter.py         # utför plansnitten, parar ihop grannar, bygger fogar
    exporter.py       # skriver part_NN.stl + split_report.json
    joints/           # foggeometri                                       [fas 3]
      base.py         #   plan-frame, kontaktyta, booleaner, JointBuilder
      dovetail.py     #   laxstjärt (trapetsprisma med undersnitt)
      pins.py         #   styrpinnar (dowels)
      puzzle.py       #   pusselprofil (sinus eller nyckelhål)
      screw.py        #   M3-skruv med mutterficka + styrpinnar
      __init__.py     #   build_joint(): val, kontroll och fallback-kedja
    progress.py       # framsteg och avbrott för långa operationer          [fas 4]
  gui/                # grafiskt gränssnitt (PySide6)                       [fas 4]
    app.py            #   MainWindow: arbetsflödet i fem steg
    view3d.py         #   ModelView (pyqtgraph.opengl) + rena geometrihjälpare
    workers.py        #   QThread-arbetare, avbrott och begripliga felmeddelanden
    settings.py       #   ~/.config/stl-cutter/settings.json
    logging_setup.py  #   ~/.local/share/stl-cutter/log.txt
    paths.py          #   XDG-sökvägar
data/printers.json    # inbyggda profiler
assets/stl-cutter.svg # programikon
assets/joints/*.svg   # bilder på fogtyperna, genererade från geometrin
tools/render_joints.py # genererar bilderna
install.sh            # installation på Kubuntu, idempotent                [fas 5]
.github/workflows/    # CI: pytest på Python 3.12 + shellcheck             [fas 5]
docs/JOINTS.md        # fogtyperna för användaren                          [fas 5]
```

Dataflöde:

```
fil -> mesh_io.load_mesh  -> MeshInfo
    -> planner.plan_splits(mesh, printer, assembly_intent)
         |-> analysis.analyse_section()   per kandidatplan
         |-> planner.score_candidate()    väljer bästa läge
         |-> recommender.recommend_joint() per valt snitt
       -> SplitPlan (lista av CutInfo)
    -> cutter.cut_mesh(mesh, plan, joints=True, printer=...)
         |-> cutter.find_pairs()          hittar delar som möts vid ett plan
         |-> joints.build_joint()         bygger fogen, med fallback-kedja
         |-> joints.validate_parts()      kontrollerar alla delar
       -> CutResult (lista av Part + JointRecord)
    -> exporter.export_parts(...)         -> STL-filer + split_report.json
```

## Dataklasser

| Klass | Modul | Innehåll |
|-------|-------|----------|
| `MeshInfo` | `mesh_io` | `path`, `mesh`, `watertight`, `winding_consistent`, `volume_mm3`, `extents_mm`, `repairs`, `open_edges` |
| `PrinterProfile` | `printers` | `name`, `bed_x/y/z`, `margin_mm` (5), `clearance_mm` (0.15); `usable` = bädd − 2·marginal, `fits(extents)` |
| `ContourInfo` | `analysis` | `area_mm2`, `perimeter_mm`, `thickness_mm` (största inskrivna cirkelns diameter), `bbox_mm` |
| `SectionAnalysis` | `analysis` | `position_mm`, `axis`, `area_mm2`, `perimeter_mm`, `contour_count`, `min_wall_mm`, `roundness`, `aspect_ratio`, `bbox_mm`, `contours`, `empty`; egenskaper `cuts_thin_detail`, `is_flat`, `is_round`, `is_elongated` |
| `JointRecommendation` | `recommender` | `joint_type`, `params`, `motivation` (svenska), `confidence` (0–1) |
| `Plane` | `planner` | `origin`, `normal`, `axis` (0=X, 1=Y, 2=Z), `position` |
| `CandidateScore` | `planner` | `position_mm`, `total` (straff, lägre är bättre), `penalties` per kriterium, `feasible` |
| `CutInfo` | `planner` | `index`, `plane`, `analysis`, `score`, `recommendation`, `alternatives` (topp 3), `nominal_position_mm` |
| `PartBox` | `planner` | `index`, `grid`, `size_mm` — förväntad låda per del, före snitt |
| `SplitPlan` | `planner` | `cuts`, `part_count`, `part_boxes`, `transform` (4×4), `orientation_name`, `divisions`, `bounds`, `printer_name`, `assembly_intent`; `planes` är en egenskap härledd ur `cuts` |
| `JointParams` | `joints.base` | Alla fogparametrar med defaults; `from_recommendation()` fyller den från fas 2 |
| `JointResult` | `joints.base` | `mesh_a`, `mesh_b`, `joint_type`, `requested_type`, `applied`, `warnings`, `attempts`; `fell_back` |
| `PlaneFrame` | `joints.base` | Lokalt system för ett snitt: `origin`, `u` (lång riktning), `v` (kort riktning, glidriktning), `n` (mot del B) |
| `JointRecord` | `cutter` | Loggpost per fog: `cut_index`, `part_a`, `part_b`, `joint_type`, `requested_type`, `applied`, `warnings` |
| `Part` | `cutter` | `index`, `mesh`; härlett: `volume_mm3`, `extents_mm`, `watertight` |
| `CutResult` | `cutter` | `parts`, `original_volume_mm3`, `plan`, `warnings`, `joints`, `source_open_edges`; härlett: `volume_error`, `all_watertight`, `inherited_damage` |
| `ExportResult` | `exporter` | `directory`, `part_files`, `report_file` |

## Nyckelalgoritmer

### Orientering (fas 1)

`planner.best_fit_orientation()` provar identitet, rotationer runt X/Y/Z i steg
om 15° (0–165°) samt en PCA-baserad orientering. Kandidaten som ger minst antal
delar vinner; vid lika antal vinner minst bounding box-volym. Transformen sparas
i `SplitPlan.transform` och appliceras innan snitten.

### Antal snitt (fas 1)

Per axel: `ceil(storlek / (byggmått − 2·marginal))`.

### Reparation vid inläsning

**Flera kroppar i samma fil** — en CAD-fil, särskilt 3MF, innehåller ofta flera
separata solider. `merge_bodies()` slår ihop dem med en **boolean union** i
stället för att bara lägga trianglarna i samma mesh. Skillnaden är avgörande:
`concatenate` ger en mesh där kanterna mellan två kroppar delas av fyra
trianglar i stället för två, och slicern rapporterar *non-manifold edges*.
Unionen tar bort de inre väggarna. Har filen redan tappat kroppsindelningen (en
STL) hittar reparationen tillbaka genom att dela upp meshen i sammanhängande
komponenter och unionera dem.

`mesh_io.repair_mesh()` lagar en mesh i fem steg, från försiktigt till mer
ingripande, och gör bara nästa steg om meshen fortfarande inte är sluten:

0. slå ihop flera kroppar till en solid, när meshen har kanter med fler än två
   trianglar eller består av flera slutna skal (`body_count > 1`). Volymen får
   minska här — överlappande kroppar räknas dubbelt innan unionen, så det är
   unionens volym som är den riktiga — men inte under
   `MIN_UNION_VOLUME_FRACTION` (50 %),
1. slå ihop identiska vertices, kasta dubblerade och platta trianglar,
2. **svetsa ihop närliggande vertices** — `weld_vertices()` grupperar punkter
   efter avstånd med ett KD-träd och union-find, och ersätter varje grupp med
   dess tyngdpunkt. `merge_vertices()` slår bara ihop punkter som är exakt lika
   (eller avrundas lika) och missar därför de hårfina springorna en CAD-export
   lämnar efter sig. Toleranserna i `WELD_TOLERANCES_MM` provas i ordning
   (0,0001–0,1 mm) tills meshen är sluten; den grövsta ligger under vad en
   3D-skrivare kan återge. En svetsning som ändrar volymen mer än
   `MAX_REPAIR_VOLUME_CHANGE` (1 %) förkastas, så tunna detaljer inte plattas ut,
3. fyll återstående hål,
4. rätta normalriktningar.

`bad_edges()` returnerar (kanter utan granne, kanter med fler än två grannar).
Det första är hål, det andra ytor som ligger på varandra. `open_edge_count()`
summerar dem — samma mått som slicers kallar *non-manifold edges*. Att bara
räkna hål räckte inte: en modell byggd av flera kroppar har noll hål men är
ändå inte en giltig solid. Det sparas i `MeshInfo.open_edges` och i
`CutResult.source_open_edges`, så att en del som inte är sluten kan förklaras
med att **originalet** var trasigt (`CutResult.inherited_damage`) i stället för
att skyllas på kapningen. Varje del körs genom samma reparation efter snittet.

### Analys av snittytan (fas 2)

`analysis.analyse_section()` tar fram tvärsnittet med `trimesh.section` →
`Path2D` och mäter per ö (`polygons_full`):

* **area** och **omkrets** — summerat över alla öar.
* **väggtjocklek** — `largest_inscribed_diameter()` rastrerar konturen och kör
  `scipy.ndimage.distance_transform_edt`; den största inskrivna cirkelns diameter
  är konturens lokala tjocklek. Upplösningen styrs av konturens **korta** sida
  (minst 64 pixlar), annars mäts tunna plattor för grovt. Halva pixeln dras av
  eftersom avståndet mäts mellan pixelcentrum. Uppmätt fel är under 2 % —
  en 3 mm platta mäts till ≈2,95 mm. `min_wall_mm` är minsta värdet över alla öar.
* **rundhet** — normaliserad, `4πA / P²`. Cirkel = 1,0, kvadrat ≈ 0,79,
  2:1-rektangel ≈ 0,70, 10:1-remsa ≈ 0,26. Gränsen `is_round` går vid 0,60,
  vilket ungefär motsvarar en 3:1-rektangel.
* **längd/bredd** — snittets bounding box; `is_elongated` vid ≥ 2:1.
* **tunn detalj** — `cuts_thin_detail` när `min_wall_mm < 3 mm`.

Ett plan som inte träffar geometri ger `empty=True`.

### Kandidatplan och poängsättning (fas 2)

För varje nödvändigt snitt provas lägen i ett intervall om ±15 % av modellens
längd i axeln, i steg om 2 mm (`SCORE_CONFIG`). Fönstret begränsas dessutom så
att **antalet delar aldrig ökar**: en kandidat måste lämna skivor som får plats i
byggvolymen både bakåt och framåt. Snitten längs en axel väljs sekventiellt.

Poängen är ett **straff** — lägre är bättre — och vikterna ligger i
`planner.SCORE_WEIGHTS` så att de går att justera:

| Vikt | Standard | Straffas när |
|------|----------|--------------|
| `part_count` | 1000 | kandidaten är omöjlig (tomt snitt) |
| `joint_room` | 15 | en skiva kommer närmare byggvolymens gräns än `joint_room_mm` (12 mm) — då finns ingen plats kvar för fogens nyckel att sticka ut |
| `thin_wall` | 25 | `min_wall_mm` under 4 mm, linjärt mot underskottet |
| `contours` | 8 | per ö utöver den första |
| `area` | 10 | arean under 200 mm² eller över 20 000 mm², logaritmiskt |
| `small_part` | 20 | angränsande skiva tunnare än 5 % av axelns längd |
| `offset` | 4 | avstånd från den jämnt fördelade positionen |

`plan_splits(..., weights=..., score_config=...)` tar egna dictar som slås ihop
med standardvärdena. Med `analyse=False` hoppas analysen över och snitten läggs
jämnt fördelade (fas 1-beteendet) — snabbt och användbart för förhandsgranskning.

### Val av fogtyp (fas 2)

`recommender.recommend_joint(analysis, intent, printer)` poängsätter varje fogtyp
och returnerar `(bästa, topp 3)`. `intent` är `"glue"` eller `"demountable"`.

| Villkor | Fogtyp | Konfidens |
|---------|--------|-----------|
| `min_wall < 4 mm` | `none` (plan limfog) | 0,90 |
| 4–8 mm och platt snitt | `puzzle` | 0,85 |
| ≥ 8 mm och avlångt snitt | `dovetail` | 0,90 |
| ≥ 6 mm och rundaktigt snitt | `pins` | 0,85 |
| demonterbart och ≥ 12 mm | `screw` (M3 + 2 styrpinnar) | 0,95 |

Utanför sitt idealfall får varje fogtyp en lägre poäng (0,35–0,50) i stället för
att uteslutas, vilket ger meningsfulla alternativ i topp 3-listan.
Är snittytan större än 5 000 mm² kompletteras den valda fogen med två extra
styrpinnar (gäller inte `pins`/`screw`, som redan har styrning).
Pinndiametern är 20 % av minsta väggtjocklek, avrundad till halv mm, med taket
8 mm. `clearance_mm` kommer från skrivarprofilen.

### Snittning (fas 1)

`cutter.cut_mesh()` kör planen sekventiellt. Varje plan delar varje befintlig bit
i två med `trimesh.intersections.slice_mesh_plane(cap=True)`, med `manifold3d`
som motor när det finns installerat (`cutter.preferred_engine()`). Bitar med
försumbar volym kastas, så en modell med hål ger färre delar än rutnätets
`part_count` — det är förväntat.

Efter snittet fylls hål och normaler rättas per del. Delar som inte är watertight
loggas som varning, och volymskillnaden mot originalet jämförs mot
`cutter.VOLUME_TOLERANCE` (0,5 %).

### Foggeometri (fas 3)

Gemensamt gränssnitt: `build(mesh_a, mesh_b, plane, params) -> (mesh_a_out, mesh_b_out)`.
Del A ligger under planet och får fogens **hane**, del B över och får **honan**.

**Generell metod** — `JointBuilder.build()`: `keys()` bygger nyckeln som solid i
det lokala systemet, den adderas till A, och samma nyckel uppförstorad med
`clearance_mm` per sida subtraheras från B. **Varje ö i kontaktytan får en egen
fog** (`islands()`): ett snitt genom en ribbad eller ihålig modell träffar flera
skilda ytor, och en fog på bara den största hade lämnat resten av skarven lös.
Varje ö får dessutom sin egen riktning, eftersom ribbor kan ligga åt olika håll. `manifold3d` används genomgående.
Nycklarna överlappar 1 mm in i den egna delen (`OVERLAP_MM`) så att booleaner
aldrig möts exakt kant-i-kant.

**Kontaktytan** — `contact_region()` sektionerar båda delarna 0,05 mm in på var
sin sida om planet och skär polygonerna mot varandra. Sektioner av
booleanbearbetade meshar innehåller nästan sammanfallande hörn, så polygonen
städas med `simplify(0.001)` innan den extruderas — annars blir prismat
degenererat och `manifold3d` avvisar det. `aligned_frame()` roterar sedan
systemet så att `u` följer kontaktytans långa riktning.

| Fogtyp | Konstruktion |
|--------|--------------|
| `pins` | Cylindrar med fasad topp, placerade på `region.buffer(-(marginal + radie))` så att 3 mm hålls till kanten. Hålet får `clearance` i radie och 0,3 mm extra djup så pinnen bottnar mot luft. |
| `dovetail` | Trapetsprisma, bredare vid `depth` än vid halsen (8° flare) — låser mot dragkraft. 1–3 st fördelade längs `u`, var och en extruderad längs `v` (glidriktningen) med 0,4 mm fas i båda ändarna. Byggs som konvext hölje av tvärsnitt på flera nivåer, vilket inte kan ge en trasig mesh. Honan öppnas mot sidorna (`region.buffer(2)`) så att laxstjärten går att skjuta in. |
| `puzzle` | Följer inte den generella metoden. En profil i (u, n)-planet — `sine` eller `keyhole` med undersnitt — extruderas genom hela tjockleken och **ersätter** det plana snittet. Omfördelningen sker bara inuti ett *band* kring snittplanet: `A = (A − band) ∪ ((A ∪ B) ∩ band ∩ prismat)` och motsvarande för B. Utan bandet kunde `allt utom vågen` ta med sig material som ligger utanför kontaktytan, och en del svälja hela sin granne. |
| `screw` | Följer inte heller den generella metoden: material tas bort ur båda delarna. Genomgående Ø3,4 mm-hål och Ø6×3 mm försänkning i A; sexkantsficka (nyckelvidd 5,5 mm) vid snittytan och hål för skruvspetsen i B. Muttern läggs i fickan före montering. Två styrpinnar varvas med skruvarna längs `u`. |

**Begränsningar mot materialet** — `build()` mäter hur långt varje del sträcker
sig från snittytan (`reach_a`, `reach_b`) innan `keys()` anropas. Laxstjärten
kapas till halva del B:s djup, pinnar till del B:s djup minus 1 mm, och
pusselamplituden till en tredjedel av respektive dels djup. En laxstjärt kräver
minst 6 mm tjocklek.

`reach_b` är dock bara del B:s **yttermått**. I en ihålig modell kan materialet
ta slut efter ett par millimeter trots att delen är decimeterstor, och en nyckel
som sticker in i tomrummet lägger till material som aldrig funnits.
`JointBuilder.material_depth()` mäter därför det verkliga djupet: den
sektionerar del B på det önskade djupet **och halvvägs dit**, och kräver att
minst `MATERIAL_COVERAGE` (90 %) av kontaktytan har material bakom sig. Räcker
det inte halveras djupet stegvis tills det gör det, eller tills
`MIN_MATERIAL_DEPTH_MM` (2 mm) underskrids och ön hoppas över.

**Begränsning mot byggvolymen** — nyckeln gör en av delarna större.
`cutter.build_volume_slack()` räknar ut hur mycket en del får växa och ändå få
plats (sorterade mått mot sorterad byggvolym, eftersom delen får vridas), och
`apply_joints()` sätter `JointParams.max_protrusion_mm` till den **minsta**
marginalen av de två delarna — vilken som får nyckeln avgörs först under bygget.
Alla fogtyper, inklusive de kompletterande styrpinnarna, lyder under taket.
Finns mindre än `MIN_USEFUL_PROTRUSION_MM` (2 mm) att växa på byggs ingen fog,
och användaren får veta att marginalen i skrivarprofilen behöver ökas.

**Städning som inte förstör** — `_clean()` kör `merge_vertices()` och kastar
degenererade trianglar efter varje boolean. `nondegenerate_faces()` kan dock
öppna hål i en mesh som redan var hel, och då avvisar `manifold3d` den i nästa
steg — fogen misslyckas trots att geometrin var i ordning. Städningen kastas
därför om den gör en hel mesh trasig.

**Robusthetskedja** — `joints.build_joint()` kontrollerar efter varje boolean att
resultatet är watertight, har konsekventa normaler och att den sammanlagda
volymen ligger mellan 50 % och 102 % av utgångsläget. Vid problem provas i tur
och ordning:

1. samma fog på städade meshar (`process(validate=True)`),
2. samma fog förskjuten 0,5 mm i planet,
3. **fogen vänd**: nyckeln läggs på den andra delen i stället. Det räddar snitt
   där den ena sidan är ihålig bakom kontaktytan men den andra har material —
   till exempel när snittet skrapar kanten på en mellanvägg,
4. en enklare fogtyp: `dovetail`/`puzzle`/`screw` → `pins` → plant snitt.

Delarna returneras alltid — `build_joint()` kraschar aldrig utan resultat, och
varje försök loggas i `JointResult.attempts`.

**Styrpinnar som komplement** — när `params.guide_pins > 0` och fogen inte redan
är `pins`, `screw` eller `puzzle` byggs pinnarna i ett andra pass. (Pusselfogen
undantas: dess vågiga skarv lämnar ingen plan yta att sätta pinnar i, och
profilen styr redan delarna i planet.) Kontaktytan räknas då om
från de färdiga delarna, så pinnarna hamnar automatiskt på den yta som är kvar
runt huvudfogen. Misslyckas det blir det en varning, inte ett fel.

**Ihopparning** — `cutter.find_pairs()` letar delar vars bounding box möts vid ett
snittplan (inom 0,05 mm) med minst 1 mm överlapp i planet. Paren tas fram
**innan** någon fog byggs, eftersom nycklarna ändrar delarnas bounding box.

**Kontroll före export** — `joints.validate_parts()` körs efter fogbygget och
rapporterar problem per del; `CutResult.validate()` är genvägen.

## split_report.json

```json
{
  "generated": "...", "source": "...",
  "printer": { ...PrinterProfile... },
  "plan": {
    "printer": "...", "orientation": "...", "assembly_intent": "glue",
    "transform": [[...]], "divisions": {"X": 3, "Y": 1, "Z": 1},
    "part_count": 3,
    "planes": [ { "origin": [...], "normal": [...], "axis": "X", "position_mm": 0.0 } ],
    "cuts": [
      {
        "index": 1,
        "plane": { ... },
        "nominal_position_mm": -86.7,
        "analysis": { "area_mm2": 0.0, "contour_count": 1, "min_wall_mm": 0.0,
                      "roundness": 0.0, "aspect_ratio": 1.0, "cuts_thin_detail": false,
                      "contours": [ ... ] },
        "score": { "total": 0.0, "feasible": true, "penalties": { "offset": 0.0 } },
        "recommendation": { "joint_type": "dovetail", "params": { ... },
                            "motivation": "...", "confidence": 0.9 },
        "alternatives": [ { ...topp 3... } ]
      }
    ],
    "part_boxes": [ ... ], "bounds_mm": [[...]]
  },
  "result": { "volume_error_percent": 0.0, "all_watertight": true,
              "warnings": [],
              "joints": [ { "cut_index": 1, "part_a": 1, "part_b": 2,
                            "joint_type": "dovetail", "requested_type": "dovetail",
                            "applied": true, "fell_back": false, "warnings": [] } ],
              "parts": [ { "index": 1, "size_mm": [...],
              "volume_mm3": 0.0, "watertight": true, "file": "part_01.stl" } ] }
}
```

Vid `--dry-run` är `result` `null`. Med `--no-analysis` är `analysis`, `score`,
`recommendation` `null` och `alternatives` tom.

### Grafiskt gränssnitt (fas 4)

`stl_cutter/gui/` anropar bara det publika kärn-API:et. Startas med
`python -m stl_cutter.gui` eller konsolskriptet `stl-cutter-gui`.

**Arbetsflöde** — vänsterpanelen läses uppifrån och ner: 1. Modell (öppna eller
dra-och-släpp, visar mått, volym och om meshen är hel), 2. Skrivare (profil +
redigerbar byggvolym och marginal, "Spara som ny profil"), 3. Montering (limmas
/ tas isär + tolerans), 4. Förslag ("Analysera" fyller en tabell med snitt,
position, fogtyp i en dropdown och motivering), 5. Kapa och exportera (målmapp +
"Kapa modellen"). Högerpanelen är 3D-vyn.

**Trådar** — `gui.workers.Worker` är en `QThread` som kör en funktion vilken tar
emot ett `progress`-argument. Kärnan anropar callbacken; trycker användaren på
Avbryt kastar den `core.progress.Cancelled`, som avslutar arbetet vid nästa
rapport. Fönstret fryser aldrig, och knappar avaktiveras medan arbete pågår.

**Fel** — `workers.friendly_error()` översätter undantag till svenska
meddelanden i statusrutan. Stacktracen går bara till loggfilen.

**Bilder på fogtyperna** — `tools/render_joints.py` bygger varje fogtyp med
`joints.build_joint()`, drar isär delarna (och vänder honan, eller lägger ett
snitt genom skruvfogen) och projicerar trianglarna till SVG med målarens
algoritm och enkel flat shading. Ingen grafikdrivrutin behövs, och bilderna kan
aldrig visa något annat än vad programmet faktiskt bygger. Två varianter skrivs
per fogtyp: `<typ>.svg` med rubrik för dokumentationen och `<typ>-plain.svg`
utan, för gränssnittet där namnet redan står bredvid. `gui.joint_images` letar
upp dem och faller tillbaka på ingen bild om de saknas, `gui.joint_help` visar
alla fem i ett fönster.

**Musen i 3D-vyn** — `ModelView` skriver över `mouseMoveEvent` så att **höger**
musknapp vrider kameran precis som vänster; pyqtgraph använder i grunden bara
vänster. Ctrl + dra flyttar vyn i stället. Ett högerklick **utan** dragning
(mindre än `CLICK_SLOP_PX`) öppnar i stället menyn med färdiga vinklar
(`STANDARD_VIEWS`) och *Anpassa till modellen*, som zoomar till
`content_bounds()` — de synliga delarnas box inklusive explosionsförskjutningen.

**3D-vyn** — bakgrunden går att växla mellan ljus (standard) och mörk med en
kryssruta; modellens och byggplattans färger byts med den, så att inget
försvinner mot underlaget. Valet sparas i inställningarna.
`gui.view3d` skiljer på ren geometri (`part_colors()`,
`explode_offsets()`, `plane_quad()`, `bed_grid()`, testbara utan grafikkort) och
`ModelView`, som ritar. Modellen visas som en mesh, snittplanen som
halvtransparenta plan, och efter kapning delarna i olika färger med en slider
som spränger isär dem radiellt från modellens mitt. Byggplattan visas som
rutnät via en kryssruta.

**Tillstånd** — `gui.settings.Settings` (dataklass) sparas som JSON i
`~/.config/stl-cutter/settings.json` när fönstret stängs. En trasig eller
saknad fil ger standardvärden i stället för ett fel.

### API-tillägg för GUI:t (minimala)

Kärnlogiken är oförändrad. Tre additiva tillägg gjordes:

| Tillägg | Varför |
|---------|--------|
| `core/progress.py` med `Cancelled` och `report()` | Framsteg och avbrott utan att kärnan känner till Qt |
| `progress=`-parameter på `plan_splits()`, `cut_mesh()` och `apply_joints()` | Progressbar och avbrytknapp |
| `recommender.build_recommendation(joint_type, analysis, ...)` | Användaren byter fogtyp manuellt i tabellen; parametrarna räknas ut som vanligt men typen är given |

Vill GUI:t byta fogtyp för ett snitt sätter det bara `CutInfo.recommendation`
innan `cut_mesh(joints=True)` anropas.

## Paketering och installation (fas 5)

`install.sh` installerar programmet på Kubuntu/Ubuntu och är **idempotent** —
kör om det för att uppdatera. Det gör i tur och ordning:

1. **Systembibliotek** — letar efter `libEGL.so.1`, `libGL.so.1` och
   `libxkbcommon-x11.so.0` med `ldconfig`, och provar `python3 -m venv --help`.
   Det är mer pålitligt än att fråga `dpkg` om paketnamn, som skiljer sig mellan
   utgåvor. Saknas något installeras det med `apt`, men **ett trasigt paketarkiv
   stoppar inte installationen**: kommandoraden fungerar ändå, och användaren får
   veta att bara GUI:t påverkas.
2. **Virtuell miljö** i `~/.local/share/stl-cutter/venv`, återanvänds om den finns.
3. **Ikon** till `~/.local/share/icons/hicolor/scalable/apps/`.
4. **Menypost** `~/.local/share/applications/stl-cutter.desktop` som pekar rakt
   på venv:ens `stl-cutter-gui`.
5. **Genvägar** i `~/.local/bin`, med varning om mappen saknas i `PATH`.
6. **Kontroll** — programmet startas *och kapar en riktig testmodell*. Att
   `--list-printers` fungerar bevisar bara att paketet importeras; ett saknat
   beroende djupare in märks först när något faktiskt ska göras.

`./install.sh --uninstall` tar bort venv, menypost, ikon och genvägar, men
lämnar användarens inställningar och logg kvar och säger var de finns.

### CI

`.github/workflows/tests.yml` kör vid PR mot `main` och vid push till `main`:

* **pytest** på Python 3.12. Qt-biblioteken installeras på runnern och ett eget
  steg kontrollerar att de går att importera — annars hade GUI-testerna hoppats
  över tyst och CI:n gett falsk trygghet. Testerna körs med
  `QT_QPA_PLATFORM=offscreen`.
* **install.sh** kontrolleras med `bash -n` och `shellcheck --severity=warning`.

### Beroenden

Utöver de uppenbara krävs två som lätt glöms bort, eftersom `trimesh` laddar dem
lat och först felar när de behövs:

| Paket | Används av |
|-------|-----------|
| `rtree` | `Path2D.polygons_full` — all snittanalys. Utan den kraschar första analysen. |
| `mapbox-earcut` | triangulering vid extrudering av polygoner (pussel- och laxstjärtsfogar) |

