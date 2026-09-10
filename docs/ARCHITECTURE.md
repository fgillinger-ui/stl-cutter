# Arkitektur

Detta dokument beskriver modulerna och dataklasserna i `stl_cutter`.
**Kommande faser ska läsa och uppdatera den här filen.**

## Teknikval (fastställt)

Python 3.12, `trimesh` (mesh-IO och snitt), `manifold3d` (boolean-motor),
`shapely` (2D-geometri), `numpy`, `scipy`, `networkx`.
GUI i `PySide6` + `pyqtgraph.opengl` (fas 4). Allt körs lokalt, inga nätverksanrop.
Alla mått är i millimeter internt.

## Modulöversikt

```
stl_cutter/
  cli.py              # kommandorad: cut / printers / --list-printers / --dry-run
  __main__.py         # python -m stl_cutter
  core/
    mesh_io.py        # ladda och spara STL/3MF, reparera, rapportera watertight
    printers.py       # skrivarprofiler (byggvolym, marginal, tolerans)
    planner.py        # orientering + beräkning av snittplan
    cutter.py         # utför plansnitten, kvalitetskontroll
    exporter.py       # skriver part_NN.stl + split_report.json
data/printers.json    # inbyggda profiler
```

Dataflöde:

```
fil -> mesh_io.load_mesh -> MeshInfo
    -> planner.plan_splits(mesh, printer) -> SplitPlan
    -> cutter.cut_mesh(mesh, plan)        -> CutResult (lista av Part)
    -> exporter.export_parts(...)         -> STL-filer + split_report.json
```

## Dataklasser

| Klass | Modul | Innehåll |
|-------|-------|----------|
| `MeshInfo` | `mesh_io` | `path`, `mesh`, `watertight`, `winding_consistent`, `volume_mm3`, `extents_mm`, `repairs` |
| `PrinterProfile` | `printers` | `name`, `bed_x/y/z`, `margin_mm` (5), `clearance_mm` (0.15); `usable` = bädd − 2·marginal, `fits(extents)` |
| `Plane` | `planner` | `origin`, `normal`, `axis` (0=X, 1=Y, 2=Z) |
| `PartBox` | `planner` | `index`, `grid`, `size_mm` — förväntad låda per del, före snitt |
| `SplitPlan` | `planner` | `planes`, `part_count`, `part_boxes`, `transform` (4×4), `orientation_name`, `divisions`, `bounds`, `printer_name` |
| `Part` | `cutter` | `index`, `mesh`; härlett: `volume_mm3`, `extents_mm`, `watertight` |
| `CutResult` | `cutter` | `parts`, `original_volume_mm3`, `plan`, `warnings`; härlett: `volume_error`, `all_watertight` |
| `ExportResult` | `exporter` | `directory`, `part_files`, `report_file` |

## Nyckelalgoritmer (fas 1)

**Orientering** — `planner.best_fit_orientation()` provar identitet, rotationer
runt X/Y/Z i steg om 15° (0–165°) samt en PCA-baserad orientering som lägger
modellens huvudaxlar längs X/Y/Z. Kandidaten som ger minst antal delar vinner;
vid lika antal vinner minst bounding box-volym. Transformen sparas i
`SplitPlan.transform` och appliceras på meshen innan snitten.

**Antal snitt** — per axel: `ceil(storlek / (byggmått − 2·marginal))`.
Snittplanen läggs jämnt fördelade inuti bounding boxen.

**Snittning** — `cutter.cut_mesh()` kör planen sekventiellt. Varje plan delar
varje befintlig bit i två med `trimesh.intersections.slice_mesh_plane(cap=True)`,
med `manifold3d` som motor när det finns installerat (`cutter.preferred_engine()`).
Bitar med försumbar volym kastas, så en modell med hål (t.ex. en torus) ger färre
delar än rutnätets `part_count` — det är förväntat.

**Kvalitetskontroll** — efter snittet fylls hål och normaler rättas per del.
Delar som inte är watertight loggas som varning, och volymskillnaden mot
originalet jämförs mot `cutter.VOLUME_TOLERANCE` (0,5 %).

## split_report.json

```json
{
  "generated": "...", "source": "...",
  "printer": { ...PrinterProfile... },
  "plan":    { ...SplitPlan.to_dict()... },
  "result":  { "volume_error_percent": 0.0, "all_watertight": true,
                "warnings": [], "parts": [ {"index": 1, "size_mm": [...],
                "volume_mm3": 0.0, "watertight": true, "file": "part_01.stl"} ] }
}
```

Vid `--dry-run` är `result` `null`.

## Planerade utökningar

* **Fas 2** — `core/analysis.py` (mått på snittytan) och `core/recommender.py`
  (val av fogtyp). `planner` poängsätter kandidatplan i ett intervall runt den
  nödvändiga positionen. `split_report.json` utökas med analys och rekommendation
  per snitt.
* **Fas 3** — `core/joints/` med `dovetail`, `pins`, `puzzle`, `screw`;
  booleaner via `manifold3d`. Hooks läggs in i `cutter.py`.
* **Fas 4** — `stl_cutter/gui/` (PySide6). GUI:t anropar endast befintligt API.
* **Fas 5** — `install.sh`, `.desktop`, ikon, CI-workflow.
