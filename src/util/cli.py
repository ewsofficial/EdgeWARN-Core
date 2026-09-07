"""Reusable, ownership-scoped CLI flag builders for the realtime entry points.

Decomposition Phase 1 (plans/realtime-runner-decomposition-plan.md): the old
monolithic runner owned every flag in one parser, so no service could expose
only the flags it honors. These builders compose that same flag set from
per-service groups so each future entry point (primary, EWMRS, NEXRAD, and the
optional launcher) can build exactly the parser it owns while ``--base-dir``
spelling, YAML-default overlay behavior, and validation stay identical.

This module deliberately lives under ``util/`` rather than
``util/runtime/``: ``util/io.py`` composes these builders, and importing
anything inside the ``util.runtime`` package would drag the full EdgeWARN /
EWMRS scientific import graph into every consumer of ``util.io``.

Flag definitions only. Resolution of YAML defaults happens through
``common.config.overlay.resolve`` at parse time, exactly as before; see
``IOManager.get_args`` / ``get_historical_args``.
"""

import argparse


def build_service_parser(service, *, add_help=True):
    """Build the argument grammar for one realtime service without resolving it."""
    parser = argparse.ArgumentParser(add_help=add_help, allow_abbrev=False)
    if service == "edgewarn":
        add_primary_domain_flags(parser)
        add_base_directory_flags(parser)
        add_primary_processing_flags(parser)
        add_ctam_diagnostic_flags(parser)
        add_service_enablement_flags(parser)
        add_mrms_core_only_flag(parser)
    elif service == "ewmrs":
        add_base_directory_flags(parser)
        parser.add_argument("--profile", action=argparse.BooleanOptionalAction, default=None)
        for flag in ("disable-metar", "disable-nws", "disable-wpc", "disable-goes"):
            parser.add_argument(f"--{flag}", action=argparse.BooleanOptionalAction, default=None)
        add_mrms_core_only_flag(parser)
    elif service == "nexrad":
        add_base_directory_flags(parser)
        parser.add_argument("--profile", action=argparse.BooleanOptionalAction, default=None)
        add_mrms_core_only_flag(parser)
    else:
        raise ValueError(f"unknown service parser {service!r}")
    return parser


def add_base_directory_flags(parser):
    """Flags every service owns: where the runtime tree lives."""
    parser.add_argument("--base_dir", "--base-dir", dest="base_dir", type=str, default=None, help="Custom base directory for input/output data")
    parser.add_argument("--config-dir", type=str, default=None, help="Override the config/ directory (else EDGEWARN_CONFIG_DIR or repo root)")


def add_ctam_diagnostic_flags(parser):
    """Primary-owned CTAM diagnostics, shared with the historical parser."""
    parser.add_argument("--list-ctam-modules", action="store_true", help="List the CTAM modules discovered in the module root and exit without running the pipeline")
    parser.add_argument("--check-ctam-modules", action="store_true", help="Validate the installed CTAM module manifests, exit nonzero if any is invalid, and run no pipeline work")


def add_primary_processing_flags(parser):
    """Flags the primary EdgeWARN pipeline owns (also used historically).

    All BooleanOptionalAction switches default to None, not the catalog value:
    ``overlay.resolve`` distinguishes "not given" from "given" by ``is None``,
    and a real default here would make run.ctam_module_dir unreachable.
    """
    parser.add_argument("--profile", action=argparse.BooleanOptionalAction, default=None, help="Enable performance profiling (default: from runtime.yaml; --no-profile disables)")
    parser.add_argument("--disable-ctam", action=argparse.BooleanOptionalAction, default=None, help="Skip CTAM module execution during integration (default: from runtime.yaml; --no-disable-ctam re-enables)")
    parser.add_argument("--disable-ctam-modules", action=argparse.BooleanOptionalAction, default=False, help="Skip discovered external CTAM modules but retain built-in StormCast")
    # default=None, not the catalog value: `overlay.resolve` distinguishes
    # "not given" from "given" by `is None`, and a real default here would
    # make run.ctam_module_dir unreachable.
    parser.add_argument("--ctam-module-dir", type=str, default=None, help="Override the directory installed CTAM module manifests are discovered from, relative to the repo root unless absolute (default: from runtime.yaml)")
    parser.add_argument("--disable-tracking", action=argparse.BooleanOptionalAction, default=None, help="Skip lineage detection and Kalman tracking in storm cell detection (default: from runtime.yaml; --no-disable-tracking re-enables)")
    parser.add_argument("--disable-polygon-expansion", action=argparse.BooleanOptionalAction, default=None, help="Use original ProbSevere polygons directly and skip radar gate mapping plus watershed expansion (default: from runtime.yaml; --no-disable-polygon-expansion re-enables)")
    parser.add_argument("--refl-threshold", type=float, default=None, help="Override the baseline reflectivity threshold used by storm cell detection (default: from detection.yaml)")
    parser.add_argument("--min-seed-percentage", type=float, default=None, help="Override the minimum polygon seed coverage ratio used during gate expansion (default: from detection.yaml)")
    parser.add_argument("--drop-offset", type=float, default=None, help="Override the dynamic reflectivity drop offset used during gate expansion (default: from detection.yaml)")


def add_primary_domain_flags(parser):
    """Geographic domain selection: primary-only."""
    parser.add_argument("--lat_limits", type=float, nargs=2, metavar=("LAT_MIN", "LAT_MAX"), default=None, help="Latitude limits for processing (default: from runtime.yaml)")
    parser.add_argument("--lon_limits", type=float, nargs=2, metavar=("LON_MIN", "LON_MAX"), default=None, help="Longitude limits for processing (default: from runtime.yaml)")


def add_mrms_core_only_flag(parser):
    """Primary-only reduced mode implying every accessory service is off."""
    parser.add_argument(
        "--mrms-core-only",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Run only MRMS detection, MRMS feature integration, and CTAM; "
            "disable EWMRS, GOES/GLM, RAP, NEXRAD, NWS, METAR, and WPC. "
            "(default: from runtime.yaml)"
        ),
    )


def add_service_enablement_flags(parser):
    """Service enablement switches routed to their owning services.

    ``--disable-ewmrs`` and ``--disable-nexrad`` have no direct meaning inside a
    single decomposed process; they exist so the optional all-services launcher
    can omit those children. They are parsed alongside the rest today because
    the monolithic runner still starts everything in one process.
    """
    parser.add_argument("--disable-ewmrs", action=argparse.BooleanOptionalAction, default=None, help="Disable EWMRS workers and rendering pipeline (default: from runtime.yaml)")
    parser.add_argument("--disable-nws", action=argparse.BooleanOptionalAction, default=None, help="Disable background NWS alert ingestion (default: from runtime.yaml)")
    parser.add_argument("--disable-metar", action=argparse.BooleanOptionalAction, default=None, help="Disable background METAR ingestion (default: from runtime.yaml)")
    # Decomposition Phase 4 CLI contract: WPC is EWMRS-owned and previously had
    # no dedicated flag (it ran unless --mrms-core-only).
    parser.add_argument("--disable-wpc", action=argparse.BooleanOptionalAction, default=None, help="Disable background WPC surface analysis ingestion (default: from runtime.yaml)")
    parser.add_argument("--disable-goes", action=argparse.BooleanOptionalAction, default=None, help="Disable GOES ingest, GLM ingest, and GOES rendering components (default: from runtime.yaml)")
    parser.add_argument("--disable-nexrad", action=argparse.BooleanOptionalAction, default=None, help="Disable background NEXRAD ingest and rendering (default: from runtime.yaml)")
