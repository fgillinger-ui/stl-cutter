"""Kommandoradsgränssnitt för stl_cutter.

Exempel:
    python -m stl_cutter.cli cut modell.stl --printer "Bambu P1S" --out ./ut
    python -m stl_cutter.cli --list-printers
    python -m stl_cutter.cli cut modell.stl --printer "Prusa MK4" --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .core import exporter, mesh_io
from .core.cutter import cut_mesh, parts_fit
from .core.planner import plan_splits
from .core.printers import PrinterProfile, get_printer, load_printers, save_profile


def _print_printers() -> None:
    print("Tillgängliga skrivarprofiler:")
    for profile in load_printers():
        x, y, z = profile.bed
        ux, uy, uz = profile.usable
        print(
            f"  {profile.name:<16} bädd {x:g} x {y:g} x {z:g} mm  "
            f"(användbart {ux:g} x {uy:g} x {uz:g}, marginal {profile.margin_mm:g} mm, "
            f"tolerans {profile.clearance_mm:g} mm)"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stl-cutter",
        description="Dela upp STL/3MF-modeller så att delarna får plats på byggplattan.",
    )
    parser.add_argument(
        "--list-printers", action="store_true", help="Visa alla skrivarprofiler och avsluta."
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Utförlig loggning.")

    sub = parser.add_subparsers(dest="command")

    cut = sub.add_parser("cut", help="Kapa en modell.")
    cut.add_argument("model", type=Path, help="Sökväg till STL- eller 3MF-fil.")
    cut.add_argument("--printer", default="Bambu Lab P1S", help="Namn på skrivarprofil.")
    cut.add_argument("--out", type=Path, default=Path("./ut"), help="Målmapp för delarna.")
    cut.add_argument(
        "--dry-run", action="store_true", help="Skriv bara planen, kapa inte modellen."
    )
    cut.add_argument(
        "--no-orient", action="store_true", help="Rotera inte modellen för bästa passform."
    )
    cut.add_argument(
        "--format", choices=["stl", "3mf"], default="stl", help="Filformat för delarna."
    )
    cut.add_argument("--margin", type=float, default=None, help="Överstyr marginal i mm.")
    cut.add_argument(
        "--assembly",
        choices=["glue", "demountable"],
        default="glue",
        help="Ska delarna limmas ihop (glue) eller kunna tas isär (demountable)?",
    )
    cut.add_argument(
        "--explain",
        action="store_true",
        help="Skriv analys och motivering för varje snitt på svenska.",
    )
    cut.add_argument(
        "--joint",
        choices=["auto", "none", "pins", "dovetail", "puzzle", "screw"],
        default="auto",
        help="Fogtyp. 'auto' följer rekommendationen per snitt.",
    )
    cut.add_argument(
        "--no-joints",
        action="store_true",
        help="Bygg ingen foggeometri - bara plana snitt.",
    )
    cut.add_argument(
        "--no-analysis",
        action="store_true",
        help="Hoppa över analys av snittytor - snabbare, men snitten läggs jämnt fördelade.",
    )

    printers = sub.add_parser("printers", help="Hantera skrivarprofiler.")
    printers.add_argument("--list", action="store_true", help="Visa profiler.")
    printers.add_argument("--add", metavar="NAMN", help="Lägg till eller uppdatera en profil.")
    printers.add_argument("--bed", nargs=3, type=float, metavar=("X", "Y", "Z"))
    printers.add_argument("--margin", type=float, default=5.0)
    printers.add_argument("--clearance", type=float, default=0.15)

    return parser


def _cmd_cut(args: argparse.Namespace) -> int:
    printer = get_printer(args.printer)
    if args.margin is not None:
        printer = PrinterProfile(
            name=printer.name,
            bed_x=printer.bed_x,
            bed_y=printer.bed_y,
            bed_z=printer.bed_z,
            margin_mm=args.margin,
            clearance_mm=printer.clearance_mm,
        )

    info = mesh_io.load_mesh(args.model)
    print(info.summary())
    for repair in info.repairs:
        print(f"  reparation: {repair}")

    plan = plan_splits(
        info.mesh,
        printer,
        auto_orient=not args.no_orient,
        analyse=not args.no_analysis,
        assembly_intent=args.assembly,
    )
    print()
    print(plan.describe())

    if args.explain:
        print()
        print(plan.explain())

    if args.dry_run:
        report = exporter.write_plan_only(plan, args.out, printer, source=args.model)
        print(f"\nTorrkörning - skrev endast planen till {report}")
        return 0

    if not plan.needs_cutting:
        print("\nModellen får plats som den är - inget att kapa.")
        return 0

    build_joints = not args.no_joints and args.joint != "none"
    result = cut_mesh(
        info.mesh,
        plan,
        joints=build_joints,
        printer=printer,
        force_joint=None if args.joint == "auto" else args.joint,
    )
    print(f"\nKapade i {len(result.parts)} delar.")
    if result.joints:
        built = [j for j in result.joints if j.applied]
        print(f"Byggde {len(built)} av {len(result.joints)} fogar.")
        for joint in result.joints:
            status = joint.joint_type if joint.applied else "ingen fog"
            note = f" (önskad: {joint.requested_type})" if joint.fell_back else ""
            print(
                f"  Snitt {joint.cut_index}: del {joint.part_a:02d}-{joint.part_b:02d} "
                f"-> {status}{note}"
            )
    print(f"Volymavvikelse: {result.volume_error * 100:.3f} %")
    for warning in result.warnings:
        print(f"VARNING: {warning}")

    problems = result.validate()
    for index, issues in problems.items():
        print(f"VARNING: del {index:02d}: {'; '.join(issues)}")

    too_big = parts_fit(result, printer)
    if too_big:
        print(f"VARNING: delarna {too_big} får fortfarande inte plats i byggvolymen.")

    export = exporter.export_parts(
        result, args.out, printer, source=args.model, file_format=args.format
    )
    print(f"Skrev {len(export.part_files)} filer till {export.directory}")
    print(f"Rapport: {export.report_file}")
    return 0 if not too_big else 1


def _cmd_printers(args: argparse.Namespace) -> int:
    if args.add:
        if not args.bed:
            print("Ange --bed X Y Z när du lägger till en profil.", file=sys.stderr)
            return 2
        profile = PrinterProfile(
            name=args.add,
            bed_x=args.bed[0],
            bed_y=args.bed[1],
            bed_z=args.bed[2],
            margin_mm=args.margin,
            clearance_mm=args.clearance,
        )
        path = save_profile(profile)
        print(f"Sparade profilen {profile.name!r} i {path}")
        return 0
    _print_printers()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.list_printers:
        _print_printers()
        return 0

    try:
        if args.command == "cut":
            return _cmd_cut(args)
        if args.command == "printers":
            return _cmd_printers(args)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(f"Fel: {exc}", file=sys.stderr)
        return 2

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
