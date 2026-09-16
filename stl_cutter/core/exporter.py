"""Skriv delarna till disk plus en rapport i JSON."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import mesh_io
from . import orient as orient_core
from .cutter import CutResult
from .printers import PrinterProfile

log = logging.getLogger(__name__)

REPORT_NAME = "split_report.json"


@dataclass
class ExportResult:
    """Vad som skrevs till disk."""

    directory: Path
    part_files: list[Path]
    report_file: Path


def build_report(
    result: CutResult,
    printer: PrinterProfile,
    source: Path | None = None,
    part_files: list[Path] | None = None,
) -> dict:
    """Bygg rapportstrukturen. Fas 2 utökar den med analys och rekommendationer."""
    report = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": str(source) if source else None,
        "printer": printer.to_dict(),
        "plan": result.plan.to_dict(),
        "result": result.to_dict(),
    }
    if part_files:
        for entry, path in zip(report["result"]["parts"], part_files):
            entry["file"] = path.name
    return report


def _bodies_of(mesh):
    """Delens sammanhängande kroppar var för sig.

    Ett snitt kan lämna en del i flera lösa klumpar - en ribba som kapades av
    på båda sidor hänger inte ihop med resten. Ligger de i samma fil ser
    slicern dem som ett objekt och man kan varken vända eller placera dem var
    för sig. Kroppar som *möts* hålls ihop; det är bara de som ligger isär som
    skiljs åt.
    """
    from . import assembly as assembly_core

    try:
        return [part.mesh for part in assembly_core.split_parts(mesh)]
    except Exception:  # pragma: no cover - försvar mot udda geometri
        log.exception("Kunde inte dela upp delen i kroppar - skriver den hel")
        return [mesh]


def export_parts(
    result: CutResult,
    out_dir: str | Path,
    printer: PrinterProfile,
    source: Path | None = None,
    file_format: str = "stl",
    lay_flat: bool = True,
    split_bodies: bool = True,
) -> ExportResult:
    """Skriv `part_01.stl` … `part_NN.stl` samt `split_report.json`.

    `lay_flat` vänder varje del till sitt plattaste läge, vilket tar bort
    stödbehov och lägger lagren rätt för hållfastheten - se `core.orient`.

    `split_bodies` skriver lösa kroppar i samma del som egna filer, numrerade
    `part_03a`, `part_03b` och så vidare, så att varje utskrift blir en fil.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    part_files: list[Path] = []
    for part in result.parts:
        bodies = _bodies_of(part.mesh) if split_bodies else [part.mesh]
        for order, body in enumerate(bodies):
            name = f"part_{part.index:02d}"
            if len(bodies) > 1:
                name += chr(ord("a") + order)
            if lay_flat:
                body, height = orient_core.lay_flat(body)
                log.info("%s: %s", name, orient_core.describe_orientation(part.mesh, body))
            if file_format.lower() == "3mf":
                path = mesh_io.save_3mf(body, out_dir / f"{name}.3mf")
            else:
                path = mesh_io.save_stl(body, out_dir / f"{name}.stl")
            part_files.append(path)
            log.info("Skrev %s", path.name)

    report_file = out_dir / REPORT_NAME
    report = build_report(result, printer, source=source, part_files=part_files)
    report_file.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Skrev %s", report_file.name)

    return ExportResult(directory=out_dir, part_files=part_files, report_file=report_file)


def write_plan_only(
    plan, out_dir: str | Path, printer: PrinterProfile, source: Path | None = None
) -> Path:
    """`--dry-run`: skriv bara planen, inga delar."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_file = out_dir / REPORT_NAME
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": str(source) if source else None,
        "printer": printer.to_dict(),
        "plan": plan.to_dict(),
        "result": None,
    }
    report_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return report_file


def export_model(
    meshes,
    path: str | Path,
    file_format: str | None = None,
    lay_flat: bool = True,
) -> list[Path]:
    """Skriv modellen utan att dela den, vänd platt inför utskrift.

    Det vanliga flödet kapar modellen, men efter en måttändring vill man ofta
    bara ha ut den ändrade modellen - den kanske får plats på plattan som den
    är, eller ska tillbaka in i CAD.

    `meshes` är en mesh eller en lista av meshar (filens objekt). Med flera
    objekt numreras filerna `<namn>_01`, `<namn>_02` och så vidare, precis som
    `resize` på kommandoraden gör, så att varje objekt blir en egen utskrift.

    Med `lay_flat` (standard) läggs varje objekt i sitt plattaste axelriktade
    läge, precis som delarna får efter en kapning. Det är nästan alltid det man
    vill: en hyllplatta som står upp i CAD-filen kom tidigare ut stående, och
    då skriver slicern den på högkant med stöd överallt och lagren tvärs den
    riktning lasten böjer den. Vill man ha filen i modellens eget läge - för
    att ta den tillbaka in i CAD - stängs vändningen av.

    Formatet följer filändelsen om inget annat anges. Returnerar sökvägarna
    som faktiskt skrevs - 3MF kan falla tillbaka på STL om biblioteksstödet
    saknas, och då är det den filen man vill visa användaren.
    """
    path = Path(path)
    if not isinstance(meshes, (list, tuple)):
        meshes = [meshes]
    if not meshes:
        raise ValueError("Ingen geometri att exportera.")

    suffix = path.suffix or ".stl"
    fmt = (file_format or suffix.lstrip(".")).lower()
    if fmt not in ("stl", "3mf"):
        raise ValueError(f"Okänt filformat: {fmt!r}. Använd stl eller 3mf.")

    path.parent.mkdir(parents=True, exist_ok=True)

    if lay_flat:
        meshes = [orient_core.lay_flat(mesh)[0] for mesh in meshes]

    written: list[Path] = []
    for index, mesh in enumerate(meshes, start=1):
        if len(meshes) == 1:
            target = path.with_suffix(f".{fmt}")
        else:
            target = path.with_name(f"{path.stem}_{index:02d}.{fmt}")
        if fmt == "3mf":
            written.append(mesh_io.save_3mf(mesh, target))
        else:
            written.append(mesh_io.save_stl(mesh, target))
    return written
