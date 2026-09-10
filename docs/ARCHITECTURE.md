# Arkitektur

Detta dokument beskriver modulerna och dataklasserna i `stl_cutter`.
**Kommande faser ska läsa och uppdatera den här filen.**

Status: fas 1 (grundstruktur, plana snitt) och fas 2 (analys och rekommendation) är klara.

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
    cutter.py         # utför plansnitten, kvalitetskontroll
    exporter.py       # skriver part_NN.stl + split_report.json
data/printers.json    # inbyggda profiler
```

Dataflöde:

```
fil -> mesh_io.load_mesh  -> MeshInfo
    -> planner.plan_splits(mesh, printer, assembly_intent)
         |-> analysis.analyse_section()   per kandidatplan
         |-> planner.score_candidate()    väljer bästa läge
         |-> recommender.recommend_joint() per valt snitt
       -> SplitPlan (lista av CutInfo)
    -> cutter.cut_mesh(mesh, plan)        -> CutResult (lista av Part)
    -> exporter.export_parts(...)         -> STL-filer + split_report.json
```

## Dataklasser

| Klass | Modul | Innehåll |
|-------|-------|----------|
| `MeshInfo` | `mesh_io` | `path`, `mesh`, `watertight`, `winding_consistent`, `volume_mm3`, `extents_mm`, `repairs` |
| `PrinterProfile` | `printers` | `name`, `bed_x/y/z`, `margin_mm` (5), `clearance_mm` (0.15); `usable` = bädd − 2·marginal, `fits(extents)` |
| `ContourInfo` | `analysis` | `area_mm2`, `perimeter_mm`, `thickness_mm` (största inskrivna cirkelns diameter), `bbox_mm` |
| `SectionAnalysis` | `analysis` | `position_mm`, `axis`, `area_mm2`, `perimeter_mm`, `contour_count`, `min_wall_mm`, `roundness`, `aspect_ratio`, `bbox_mm`, `contours`, `empty`; egenskaper `cuts_thin_detail`, `is_flat`, `is_round`, `is_elongated` |
| `JointRecommendation` | `recommender` | `joint_type`, `params`, `motivation` (svenska), `confidence` (0–1) |
| `Plane` | `planner` | `origin`, `normal`, `axis` (0=X, 1=Y, 2=Z), `position` |
| `CandidateScore` | `planner` | `position_mm`, `total` (straff, lägre är bättre), `penalties` per kriterium, `feasible` |
| `CutInfo` | `planner` | `index`, `plane`, `analysis`, `score`, `recommendation`, `alternatives` (topp 3), `nominal_position_mm` |
| `PartBox` | `planner` | `index`, `grid`, `size_mm` — förväntad låda per del, före snitt |
| `SplitPlan` | `planner` | `cuts`, `part_count`, `part_boxes`, `transform` (4×4), `orientation_name`, `divisions`, `bounds`, `printer_name`, `assembly_intent`; `planes` är en egenskap härledd ur `cuts` |
| `Part` | `cutter` | `index`, `mesh`; härlett: `volume_mm3`, `extents_mm`, `watertight` |
| `CutResult` | `cutter` | `parts`, `original_volume_mm3`, `plan`, `warnings`; härlett: `volume_error`, `all_watertight` |
| `ExportResult` | `exporter` | `directory`, `part_files`, `report_file` |

## Nyckelalgoritmer

### Orientering (fas 1)

`planner.best_fit_orientation()` provar identitet, rotationer runt X/Y/Z i steg
om 15° (0–165°) samt en PCA-baserad orientering. Kandidaten som ger minst antal
delar vinner; vid lika antal vinner minst bounding box-volym. Transformen sparas
i `SplitPlan.transform` och appliceras innan snitten.

### Antal snitt (fas 1)

Per axel: `ceil(storlek / (byggmått − 2·marginal))`.

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
              "warnings": [], "parts": [ { "index": 1, "size_mm": [...],
              "volume_mm3": 0.0, "watertight": true, "file": "part_01.stl" } ] }
}
```

Vid `--dry-run` är `result` `null`. Med `--no-analysis` är `analysis`, `score`,
`recommendation` `null` och `alternatives` tom.

## Planerade utökningar

* **Fas 3** — `core/joints/` med `base.py`, `dovetail.py`, `pins.py`, `puzzle.py`,
  `screw.py`. Gemensamt gränssnitt `build(mesh_a, mesh_b, plane, params)`.
  Booleaner via `manifold3d`, med fallback-kedja och `validate_parts()` före
  export. `cutter.py` läser `CutInfo.recommendation` och bygger fogen; CLI får
  `--joint dovetail` för att tvinga en typ. Parametrarna i
  `JointRecommendation.params` är avsedda som defaults till `JointParams`.
* **Fas 4** — `stl_cutter/gui/` (PySide6). GUI:t anropar endast befintligt API:
  `mesh_io.load_mesh`, `plan_splits`, `cut_mesh`, `export_parts` samt
  `CutInfo.alternatives` för fogvalsdropdownen.
* **Fas 5** — `install.sh`, `.desktop`, ikon, `docs/JOINTS.md`, CI-workflow.
