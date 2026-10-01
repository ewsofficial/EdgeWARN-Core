"""Topology-aware implementation of ``edgewarn run``.

This module deliberately contains no scientific imports. Configuration loading
and launcher imports are deferred until dispatch so parser construction and
help remain lightweight.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType


# Public mode names map to the existing internal service names. Tuples preserve
# startup order, which is significant for the EWMRS producer dependency.
TOPOLOGIES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "all": ("ingest", "edgewarn", "ewmrs", "nexrad"),
        "core": ("ingest", "edgewarn"),
        "ewmrs": ("ingest", "edgewarn", "ewmrs"),
        "ingest": ("ingest",),
        "nexrad": ("nexrad",),
    }
)

WORKERS: tuple[str, ...] = ("ingest", "core", "ewmrs", "nexrad")
_WORKER_TO_SERVICE: Mapping[str, str] = MappingProxyType(
    {"ingest": "ingest", "core": "edgewarn", "ewmrs": "ewmrs", "nexrad": "nexrad"}
)
_WRAPPER_OWNED_OPTIONS = frozenset(
    {
        "--config-dir",
        "--config-path",
        "--services",
        "--disable-ewmrs",
        "--no-disable-ewmrs",
        "--disable-nexrad",
        "--no-disable-nexrad",
        "--mrms-core-only",
        "--no-mrms-core-only",
    }
)


def add_run_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the run command from the topology table."""
    mode_help = "; ".join(
        f"{mode}: {', '.join(services)}" for mode, services in TOPOLOGIES.items()
    )
    parser = subparsers.add_parser(
        "run",
        help="run a validated EdgeWARN service topology",
        description=f"Run EdgeWARN services ({mode_help}).",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=tuple(TOPOLOGIES),
        default="all",
        help="service topology to run (default: all)",
    )
    parser.add_argument(
        "--config-path",
        type=Path,
        default=None,
        help="complete configuration directory (default: registered config root)",
    )
    parser.add_argument(
        "--args",
        action="append",
        nargs=2,
        metavar=("WORKER", "JSON_ARGV"),
        default=[],
        help=(
            "worker-scoped JSON array of arguments; repeat for ingest, core, ewmrs, or "
            "nexrad"
        ),
    )
    parser.set_defaults(handler=run_from_namespace, parser=parser)


def _is_wrapper_owned(argument: str) -> bool:
    option = argument.split("=", 1)[0]
    return option in _WRAPPER_OWNED_OPTIONS


def parse_worker_argv(
    entries: Sequence[Sequence[str]], selected_services: Sequence[str]
) -> dict[str, tuple[str, ...]]:
    """Validate repeated worker/JSON pairs and return internal-service argv."""
    selected = frozenset(selected_services)
    parsed: dict[str, tuple[str, ...]] = {}

    for worker, json_argv in entries:
        if worker not in _WORKER_TO_SERVICE:
            raise ValueError(
                f"unknown worker {worker!r}; expected one of {', '.join(WORKERS)}"
            )
        service = _WORKER_TO_SERVICE[worker]
        if service not in selected:
            raise ValueError(
                f"worker {worker!r} is not part of the selected topology"
            )
        if service in parsed:
            raise ValueError(f"worker {worker!r} may be specified only once")

        try:
            value = json.loads(json_argv)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(
                f"arguments for worker {worker!r} are not valid JSON: {exc}"
            ) from exc
        if not isinstance(value, list):
            raise ValueError(f"arguments for worker {worker!r} must be a JSON array")
        if any(not isinstance(item, str) for item in value):
            raise ValueError(
                f"arguments for worker {worker!r} must contain only strings"
            )
        forbidden = next((item for item in value if _is_wrapper_owned(item)), None)
        if forbidden is not None:
            raise ValueError(
                f"argument {forbidden!r} for worker {worker!r} is owned by "
                "the edgewarn wrapper"
            )
        parsed[service] = tuple(value)

    return parsed


def canonicalize_worker_paths(
    worker_argv: Mapping[str, Sequence[str]], invocation_dir: Path
) -> dict[str, tuple[str, ...]]:
    """Resolve worker base-directory arguments before the launcher changes CWD."""
    result: dict[str, tuple[str, ...]] = {}
    for service, argv in worker_argv.items():
        normalized = list(argv)
        index = 0
        while index < len(normalized):
            item = normalized[index]
            if item in {"--base-dir", "--base_dir"} and index + 1 < len(normalized):
                normalized[index + 1] = str(
                    (invocation_dir / Path(normalized[index + 1]).expanduser()).resolve()
                )
                index += 2
                continue
            for prefix in ("--base-dir=", "--base_dir="):
                if item.startswith(prefix):
                    normalized[index] = prefix + str(
                        (invocation_dir / Path(item[len(prefix):]).expanduser()).resolve()
                    )
                    break
            index += 1
        result[service] = tuple(normalized)
    return result


def preflight_worker_argv(worker_argv: Mapping[str, Sequence[str]]) -> None:
    """Apply each selected service's argparse grammar before any process starts."""
    from util.cli import build_service_parser

    one_shot = {"-h", "--help", "--list-ctam-modules", "--check-ctam-modules"}
    for service, argv in worker_argv.items():
        command = next((item for item in argv if item in one_shot), None)
        if command is not None:
            raise ValueError(
                f"one-shot argument {command!r} is not valid in supervised service mode"
            )
        parser = build_service_parser(service, add_help=False)
        try:
            parser.parse_args(argv)
        except SystemExit as exc:
            raise ValueError(f"invalid arguments for worker {service!r}") from exc


def run_from_namespace(args: argparse.Namespace) -> int:
    """Validate package-owned inputs and dispatch through ``run_all``."""
    invocation_dir = Path.cwd()
    # Import only at execution time: help and parser errors need neither YAML
    # nor the supervisor module. Validation intentionally precedes command
    # construction and therefore every subprocess/filesystem side effect.
    from common.config import loader as config_loader, overlay
    from edgewarn_cli.config_path import resolve_config_root
    from yaml import YAMLError

    try:
        config_loader.reset_cache()
        config_root = resolve_config_root(args.config_path)
        config_loader.export_config_root(config_root)
        from util.runtime.mrms_migration import require_completed_migration
        require_completed_migration(config_root)
        config_loader.validate_all_configs(config_dir=config_root)
    except (
        config_loader.ConfigError,
        RuntimeError,
        json.JSONDecodeError,
        OSError,
        UnicodeError,
        ValueError,
        YAMLError,
    ) as exc:
        args.parser.error(str(exc))

    import run_all

    launcher_args = argparse.Namespace(
        base_dir=str(
            overlay.resolve_base_dir(
                None,
                config_loader.load_config("filesystem", config_dir=config_root),
            ).expanduser().resolve()
        ),
        config_dir=str(config_root),
        profile=None,
        lat_limits=None,
        lon_limits=None,
        disable_ctam=None,
        disable_ctam_modules=None,
        disable_stormprob=None,
        disable_tracking=None,
        disable_polygon_expansion=None,
        disable_goes=None,
        disable_metar=None,
        disable_nws=None,
        disable_wpc=None,
        refl_threshold=None,
        min_seed_percentage=None,
        drop_offset=None,
        disable_ewmrs=None,
        disable_nexrad=None,
        mrms_core_only=None,
    )
    try:
        services = tuple(
            run_all.resolve_services(launcher_args, TOPOLOGIES[args.mode])
        )
        worker_argv = canonicalize_worker_paths(
            parse_worker_argv(args.args, services), invocation_dir
        )
        preflight_worker_argv(worker_argv)
        run_all.preflight_topology(launcher_args, services, worker_argv)
        if "edgewarn" in services:
            from util.cli import build_service_parser
            from util.ctam_config import resolve_ctam_module_dir
            from EdgeWARN.ctam.preflight import check_core_startup
            core_args = build_service_parser("edgewarn", add_help=False).parse_args(
                worker_argv.get("edgewarn", ())
            )
            run_cfg = config_loader.load_config("runtime", config_dir=config_root)["run"]
            check_core_startup(
                config_dir=config_root, base_dir=launcher_args.base_dir,
                module_root=resolve_ctam_module_dir(core_args.ctam_module_dir, config_dir=config_root),
                disable_ctam=overlay.resolve(core_args.disable_ctam, yaml_value=run_cfg["disable_ctam"]),
                disable_ctam_modules=core_args.disable_ctam_modules,
                disable_stormprob=overlay.resolve(core_args.disable_stormprob, yaml_value=run_cfg["disable_stormprob"]),
                mrms_core_only=launcher_args.mrms_core_only or bool(core_args.mrms_core_only),
            )
    except (ValueError, RuntimeError) as exc:
        args.parser.error(str(exc))
    src_root = str(Path(run_all.__file__).resolve().parent)
    commands = run_all.build_service_commands(
        launcher_args,
        services,
        src_root,
        service_argv=worker_argv,
    )
    return int(run_all.supervise(commands, src_root=src_root))
