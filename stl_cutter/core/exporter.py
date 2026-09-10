"""Skriv delarna till disk plus en rapport i JSON."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import mesh_io
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


def export_parts(
    result: CutResult,
    out_dir: str | Path,
    printer: PrinterProfile,
    source: Path | None = None,
    file_format: str = "stl",
) -> ExportResult:
    """Skriv `part_01.stl` … `part_NN.stl` samt `split_report.json`."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    part_files: list[Path] = []
    for part in result.parts:
        name = f"part_{part.index:02d}"
        if file_format.lower() == "3mf":
            path = mesh_io.save_3mf(part.mesh, out_dir / f"{name}.3mf")
        else:
            path = mesh_io.save_stl(part.mesh, out_dir / f"{name}.stl")
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
