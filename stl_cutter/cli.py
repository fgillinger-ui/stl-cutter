"""Kommandoradsgränssnitt för stl_cutter.

Exempel:
    python -m stl_cutter.cli cut modell.stl --printer "Bambu P1S" --out ./ut
    python -m stl_cutter.cli --list-printers
    python -m stl_cutter.cli cut modell.stl --printer "Prusa MK4" --dry-run
    python -m stl_cutter.cli resize modell.stl --y 550 --out modell_550.stl
    python -m stl_cutter.cli export modell.3mf --out ./ut/modell.stl
    python -m stl_cutter.cli analyze-spans modell.stl --axis y
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .core import assembly as assembly_core
from .core import exporter, mesh_io, resize as resize_core
from .core.cutter import cut_mesh, parts_fit
from .core.planner import plan_splits
from .core.printers import PrinterProfile, get_printer, load_printers, save_profile
from .core.resize import ResizeError

AXIS_FROM_LETTER = {"x": 0, "y": 1, "z": 2}


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


def _add_resize_options(parser: argparse.ArgumentParser) -> None:
    """Flaggor som styr måttändringen. Delas av `resize` och `cut`."""
    parser.add_argument(
        "--mode",
        choices=["preserve", "scale"],
        default="preserve",
        help="preserve ändrar bara prismatiska partier; scale skalar rakt av och deformerar.",
    )
    parser.add_argument(
        "--select",
        choices=["auto", "longest", "distribute"],
        default="auto",
        help="Var materialet läggs. auto (standard) håller modellen symmetrisk och "
        "fördelar över de jämnstora partierna; longest lägger allt i det längsta; "
        "distribute fördelar proportionellt över alla.",
    )
    parser.add_argument(
        "--distribute",
        action="store_true",
        help="Samma sak som --select distribute.",
    )
    parser.add_argument(
        "--longest",
        action="store_true",
        help="Samma sak som --select longest.",
    )
    parser.add_argument(
        "--span",
        type=int,
        default=None,
        metavar="N",
        help="Använd parti nummer N (1 och uppåt) ur analyze-spans-listan.",
    )
    parser.add_argument(
        "--step",
        type=float,
        default=resize_core.DEFAULT_STEP_MM,
        help="Avstånd mellan provade tvärsnitt i mm.",
    )
    parser.add_argument(
        "--tol",
        type=float,
        default=resize_core.DEFAULT_TOL,
        help="Relativ tolerans när två tvärsnitt jämförs.",
    )
    parser.add_argument(
        "--min-span",
        type=float,
        default=resize_core.MIN_SPAN_LENGTH_MM,
        help="Kortaste parti som räknas som prismatiskt, i mm. Höj det för att "
        "hindra --distribute från att också sträcka korta detaljer som hyllplan.",
    )
    parser.add_argument(
        "--part",
        type=int,
        default=1,
        metavar="N",
        help="Vilket objekt måttet gäller när filen innehåller flera (1 och uppåt, "
        "vänster till höger). Övriga objekt får samma tillskott i mm.",
    )
    parser.add_argument(
        "--no-link",
        action="store_true",
        help="Ändra bara det valda objektet och lämna filens övriga objekt orörda.",
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
    for letter in ("x", "y", "z"):
        cut.add_argument(
            f"--resize-{letter}",
            type=float,
            default=None,
            metavar="MM",
            help=f"Ändra måttet i {letter.upper()} till MM innan snitten planeras.",
        )
    _add_resize_options(cut)
    cut.add_argument(
        "--no-lay-flat",
        action="store_true",
        help="Vänd inte delarna platt inför utskrift - skriv dem i modellens "
        "egen orientering.",
    )
    cut.add_argument(
        "--no-split-bodies",
        action="store_true",
        help="Skriv lösa kroppar i samma del i en och samma fil i stället för "
        "en fil per kropp.",
    )
    cut.add_argument(
        "--no-analysis",
        action="store_true",
        help="Hoppa över analys av snittytor - snabbare, men snitten läggs jämnt fördelade.",
    )

    resize_cmd = sub.add_parser(
        "resize",
        help="Ändra ett eller flera mått utan att deformera godstjocklek och hål.",
    )
    resize_cmd.add_argument("model", type=Path, help="Sökväg till STL- eller 3MF-fil.")
    for letter in ("x", "y", "z"):
        resize_cmd.add_argument(
            f"--{letter}",
            type=float,
            default=None,
            metavar="MM",
            help=f"Önskat mått i {letter.upper()}, i mm.",
        )
    resize_cmd.add_argument(
        "--out", type=Path, default=None, help="Målfil. Standard: <modell>_resized.stl"
    )
    resize_cmd.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Mapp för resize_report.json. Standard: samma mapp som målfilen.",
    )
    _add_resize_options(resize_cmd)

    export_cmd = sub.add_parser(
        "export",
        help="Skriv modellen som den är, utan att dela den.",
    )
    export_cmd.add_argument("model", type=Path, help="Sökväg till STL- eller 3MF-fil.")
    export_cmd.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Målfil. Standard: <modell>_export.stl. Flera objekt i filen "
        "numreras _01, _02 och så vidare.",
    )
    export_cmd.add_argument(
        "--format",
        choices=["stl", "3mf"],
        default=None,
        help="Filformat. Standard: följer målfilens ändelse.",
    )
    export_cmd.add_argument(
        "--merge",
        action="store_true",
        help="Skriv alla objekt i en enda fil i stället för en fil per objekt.",
    )

    spans_cmd = sub.add_parser(
        "analyze-spans",
        help="Visa var modellen går att sträcka - partierna med konstant tvärsnitt.",
    )
    spans_cmd.add_argument("model", type=Path, help="Sökväg till STL- eller 3MF-fil.")
    spans_cmd.add_argument(
        "--axis",
        choices=["x", "y", "z", "all"],
        default="all",
        help="Vilken axel som ska analyseras.",
    )
    spans_cmd.add_argument(
        "--step", type=float, default=resize_core.DEFAULT_STEP_MM, help="Avstånd mellan tvärsnitt i mm."
    )
    spans_cmd.add_argument(
        "--tol", type=float, default=resize_core.DEFAULT_TOL, help="Relativ tolerans."
    )
    spans_cmd.add_argument(
        "--min-span",
        type=float,
        default=resize_core.MIN_SPAN_LENGTH_MM,
        help="Kortaste parti som räknas, i mm.",
    )

    printers = sub.add_parser("printers", help="Hantera skrivarprofiler.")
    printers.add_argument("--list", action="store_true", help="Visa profiler.")
    printers.add_argument("--add", metavar="NAMN", help="Lägg till eller uppdatera en profil.")
    printers.add_argument("--bed", nargs=3, type=float, metavar=("X", "Y", "Z"))
    printers.add_argument("--margin", type=float, default=5.0)
    printers.add_argument("--clearance", type=float, default=0.15)

    return parser


def _span_selection(args: argparse.Namespace) -> tuple[str, int | None]:
    """Hur `delta` ska fördelas, utifrån flaggorna."""
    if args.span is not None:
        if args.span < 1:
            raise ValueError("--span numreras från 1 och uppåt.")
        return "manual", args.span - 1
    if getattr(args, "distribute", False):
        return "distribute", None
    if getattr(args, "longest", False):
        return "longest", None
    return getattr(args, "select", "auto"), None


def _print_resize(result) -> None:
    for entry in result.axes:
        name = resize_core.AXIS_NAMES[entry.axis]
        print(
            f"  {name}: {entry.from_mm:.1f} -> {entry.to_mm:.1f} mm "
            f"({entry.delta_mm:+.1f} mm, {entry.mode}, {entry.resolved_selection})"
        )
        print(f"      {entry.placement}")
        for item in entry.insertions:
            print(
                f"      {item.delta:+7.2f} mm vid {name.lower()}={item.cut_at:.1f} mm "
                f"i {item.span.describe()}"
            )
        if entry.mode == "preserve" and entry.chosen:
            print(
                f"      volym {entry.actual_volume_change_mm3 / 1000.0:+.2f} cm3 "
                f"(väntat {entry.expected_volume_change_mm3 / 1000.0:+.2f} cm3)"
            )
    for warning in result.warnings:
        print(f"  VARNING: {warning}")


def _resize_targets(args: argparse.Namespace, prefix: str = "") -> list[float | None]:
    return [getattr(args, f"{prefix}{letter}") for letter in ("x", "y", "z")]


def _cmd_resize(args: argparse.Namespace) -> int:
    targets = _resize_targets(args)
    if all(target is None for target in targets):
        print("Ange minst ett mått med --x, --y eller --z.", file=sys.stderr)
        return 2

    info = mesh_io.load_mesh(args.model)
    print(info.summary())
    for repair in info.repairs:
        print(f"  reparation: {repair}")

    selection, span_index = _span_selection(args)

    parts = assembly_core.split_parts(info.mesh)
    if len(parts) > 1 and not args.no_link:
        return _resize_linked(args, parts, targets, selection, span_index)
    result = resize_core.resize(
        info.mesh,
        targets,
        mode=args.mode,
        span_selection=selection,
        span_index=span_index,
        step=args.step,
        tol=args.tol,
        min_span_mm=args.min_span,
    )
    print("\nMåttändring:")
    _print_resize(result)

    out = args.out or args.model.with_name(f"{args.model.stem}_resized.stl")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() == ".3mf":
        mesh_io.save_3mf(result.mesh, out)
    else:
        mesh_io.save_stl(result.mesh, out)
    report_dir = args.report or out.parent
    report = resize_core.write_resize_report(result, report_dir, source=args.model)
    x, y, z = result.mesh.extents
    print(f"\nSkrev {out} ({x:.1f} x {y:.1f} x {z:.1f} mm)")
    print(f"Rapport: {report}")
    return 0


def _resize_linked(args, parts, targets, selection, span_index) -> int:
    """Måttändring av en fil med flera objekt.

    Måttet gäller objektet som `--part` pekar ut (det första som standard).
    Övriga får samma tillskott, inte samma mått - se `core.assembly`.
    """
    leader = args.part - 1
    if not 0 <= leader < len(parts):
        print(
            f"Filen har {len(parts)} objekt, så --part måste vara 1-{len(parts)}.",
            file=sys.stderr,
        )
        return 2

    print(f"\nFilen innehåller {len(parts)} separata objekt:")
    for index, part in enumerate(parts, start=1):
        mark = " <- måttet gäller detta" if index - 1 == leader else ""
        print(f"  {index}. {part.summary()}{mark}")

    current = parts
    for axis, target in enumerate(targets):
        if target is None:
            continue
        try:
            report = assembly_core.resize_together(
                current,
                axis=axis,
                target_mm=float(target),
                leader=leader,
                mode=args.mode,
                span_selection=selection,
                span_index=span_index,
                step=args.step,
                tol=args.tol,
                min_span_mm=args.min_span,
            )
        except ResizeError as error:
            print(f"\n{error.message} {error.suggestion}".rstrip(), file=sys.stderr)
            return 1
        current = report.parts
        print()
        print(assembly_core.describe_assembly(report))
        for note in report.notes:
            print(f"  {note}")
        for warning in report.warnings:
            print(f"  VARNING: {warning}")

    out = args.out or args.model.with_name(f"{args.model.stem}_resized.stl")
    out.parent.mkdir(parents=True, exist_ok=True)
    stem, suffix = out.stem, out.suffix or ".stl"
    written = []
    for index, part in enumerate(current, start=1):
        target_path = out.with_name(f"{stem}_{index:02d}{suffix}")
        if suffix.lower() == ".3mf":
            written.append(mesh_io.save_3mf(part.mesh, target_path))
        else:
            written.append(mesh_io.save_stl(part.mesh, target_path))
    print("\nSkrev:")
    for path, part in zip(written, current):
        x, y, z = part.extents_mm
        print(f"  {path} ({x:.1f} x {y:.1f} x {z:.1f} mm)")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    """Skriv modellen utan att kapa den.

    Nyttigt efter en måttändring, eller bara för att laga och konvertera en
    fil: inläsningen reparerar meshen och exporten städar bort det som STL:s
    precision annars skulle göra till trasiga kanter.
    """
    info = mesh_io.load_mesh(args.model)
    print(info.summary())
    for repair in info.repairs:
        print(f"  reparation: {repair}")

    out = args.out or args.model.with_name(f"{args.model.stem}_export.stl")

    meshes = [info.mesh]
    if not args.merge:
        parts = assembly_core.split_parts(info.mesh)
        if len(parts) > 1:
            print(f"\nFilen innehåller {len(parts)} separata objekt:")
            for index, part in enumerate(parts, start=1):
                print(f"  {index}. {part.summary()}")
            meshes = [part.mesh for part in parts]

    written = exporter.export_model(meshes, out, file_format=args.format)
    print("\nSkrev:")
    for path in written:
        x, y, z = mesh_io.load_mesh(path, repair=False).extents_mm
        print(f"  {path} ({x:.1f} x {y:.1f} x {z:.1f} mm)")
    return 0


def _cmd_analyze_spans(args: argparse.Namespace) -> int:
    info = mesh_io.load_mesh(args.model)
    print(info.summary())
    axes = [0, 1, 2] if args.axis == "all" else [AXIS_FROM_LETTER[args.axis]]
    found = False
    for axis in axes:
        spans = resize_core.find_prismatic_spans(
            info.mesh, axis, step=args.step, tol=args.tol, min_length_mm=args.min_span
        )
        found = found or bool(spans)
        print()
        print(resize_core.describe_spans(info.mesh, axis, spans))
    if not found:
        print(
            "\nModellen har inga partier med konstant tvärsnitt. Måtten går bara att "
            "ändra med --mode scale, vilket förändrar godstjocklek och hål."
        )
    return 0


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

    # Måttändringen sker alltid före snittplaneringen - annars planeras snitten
    # för en modell som inte längre finns.
    mesh = info.mesh
    targets = _resize_targets(args, prefix="resize_")
    if any(target is not None for target in targets):
        selection, span_index = _span_selection(args)
        resized = resize_core.resize(
            mesh,
            targets,
            mode=args.mode,
            span_selection=selection,
            span_index=span_index,
            step=args.step,
            tol=args.tol,
            min_span_mm=args.min_span,
        )
        mesh = resized.mesh
        print("\nMåttändring:")
        _print_resize(resized)
        resize_core.write_resize_report(resized, args.out, source=args.model)

    plan = plan_splits(
        mesh,
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
        mesh,
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
    if result.inherited_damage and not result.all_watertight:
        print(
            "Delarna ärver hålen från originalmodellen. De går oftast att skriva ut "
            "ändå - laga modellen och kapa om om din slicer klagar."
        )

    problems = result.validate()
    for index, issues in problems.items():
        print(f"VARNING: del {index:02d}: {'; '.join(issues)}")

    too_big = parts_fit(result, printer)
    if too_big:
        print(f"VARNING: delarna {too_big} får fortfarande inte plats i byggvolymen.")

    export = exporter.export_parts(
        result,
        args.out,
        printer,
        source=args.model,
        file_format=args.format,
        lay_flat=not args.no_lay_flat,
        split_bodies=not args.no_split_bodies,
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
        if args.command == "resize":
            return _cmd_resize(args)
        if args.command == "export":
            return _cmd_export(args)
        if args.command == "analyze-spans":
            return _cmd_analyze_spans(args)
        if args.command == "printers":
            return _cmd_printers(args)
    except ResizeError as exc:
        print(f"Fel: {exc.message}", file=sys.stderr)
        if exc.suggestion:
            print(exc.suggestion, file=sys.stderr)
        return 3
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(f"Fel: {exc}", file=sys.stderr)
        return 2

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
