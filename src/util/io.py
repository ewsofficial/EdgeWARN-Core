from datetime import datetime, timezone
import argparse
import sys
import time

from common.config import loader as config_loader
from common.config import overlay
from util import cli
from util.ctam_config import export_ctam_module_dir, resolve_ctam_module_dir


class TimestampedOutput:
    def __init__(self, stream):
        self.stream = stream

    def write(self, message):
        if not message:
            return

        if "\r" in message:
            self.stream.write(message)
            return

        for chunk in message.splitlines(keepends=True):
            if not chunk.strip():
                self.stream.write(chunk)
                continue

            timestamp = datetime.now(timezone.utc).isoformat()
            if chunk.endswith("\n"):
                self.stream.write(f"[{timestamp}] {chunk[:-1]}\n")
            else:
                self.stream.write(f"[{timestamp}] {chunk}")

    def flush(self):
        self.stream.flush()

    def isatty(self):
        return bool(getattr(self.stream, "isatty", lambda: False)())

    def fileno(self):
        return self.stream.fileno()


class QueueWriter:
    def __init__(self, queue):
        self.queue = queue

    def write(self, message):
        if message.strip():
            self.queue.put(message.rstrip("\n"))

    def flush(self):
        pass

    def isatty(self):
        return False


class IOManager:
    def __init__(self, header, *, include_timestamps=False):
        self.header = header
        self.include_timestamps = include_timestamps

    def _timestamp_prefix(self):
        if not self.include_timestamps:
            return ""
        return f"[{datetime.now(timezone.utc).isoformat()}] "

    def _write(self, level, msg):
        print(f"{self._timestamp_prefix()}{self.header} {level} {msg}")

    @staticmethod
    def get_base_dir_arg():
        """Read ``--base_dir`` from ``sys.argv`` before the real parser exists.

        Phase one of the two-phase resolution ``util.file`` performs at import:
        it binds 113 path globals at module scope, long before an entry point
        reaches ``get_args()``, so it needs the answer earlier than the parser
        can give it. Unknown arguments are ignored, which is what makes this safe
        to call from any argv -- a pytest or notebook process simply gets
        ``None`` and the platform default.

        ``allow_abbrev=False`` because this runs at import time: an ambiguous
        prefix such as ``--base`` matches both spellings and would otherwise
        exit here, ahead of the real parser that can report it in context.
        """
        parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        parser.add_argument("--base_dir", "--base-dir", dest="base_dir", type=str, default=None)
        args, _ = parser.parse_known_args()
        filesystem = config_loader.load_config(
            "filesystem", config_dir=IOManager.get_config_dir_arg()
        )
        return str(overlay.resolve_base_dir(args.base_dir, filesystem))

    @staticmethod
    def get_config_dir_arg():
        """Read ``--config-dir`` from ``sys.argv``, for the same reason as above.

        ``util.file`` reads ``filesystem.yaml`` during its module-scope bind to
        resolve the colormap search path, which is earlier than
        ``export_config_root`` publishes ``EDGEWARN_CONFIG_DIR``. Without this
        peek that one read would resolve the repo default however
        ``--config-dir`` was set -- silently, because ``load_config`` is keyed by
        resolved root and so would simply cache both.
        """
        parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        parser.add_argument("--config-dir", dest="config_dir", type=str, default=None)
        args, _ = parser.parse_known_args()
        return args.config_dir

    @staticmethod
    def _validate_common_args(args):
        if args.min_seed_percentage < 0:
            print("ERROR: --min-seed-percentage must be non-negative.")
            sys.exit(1)

    @staticmethod
    def _add_common_processing_args(parser):
        # Composed from the ownership-scoped builders in util.cli so every
        # entry point assembles the exact flag set it honors from the same
        # definitions. Order of registration follows the builder groups.
        cli.add_base_directory_flags(parser)
        cli.add_primary_processing_flags(parser)
        cli.add_ctam_diagnostic_flags(parser)

    @staticmethod
    def _export_config_dir(args):
        """Publish the resolved config root so spawned children inherit it."""
        root = config_loader.export_config_root(args.config_dir)
        # This gate deliberately precedes runtime/filesystem setup. A malformed
        # selected tree must fail before workers, listeners, or output dirs can
        # be created; individual accessors then reuse the immutable cache.
        config_loader.validate_all_configs(config_dir=root)

    @staticmethod
    def _run_ctam_diagnostics(args):
        """Serve ``--list-ctam-modules``/``--check-ctam-modules``, then exit.

        Called immediately after the config root is published and before any
        pipeline setup, so a diagnostic never creates an output directory, starts
        a worker, or opens a listener. It also never executes module code: it
        goes through ``discovery``, which parses ``module.toml`` and touches
        nothing else, so a broken or hostile module cannot influence the report
        on itself.

        ``EdgeWARN.ctam.discovery`` is imported here rather than at module scope
        for two reasons. ``EdgeWARN/__init__.py`` imports ``EdgeWARN.pipeline``,
        which imports this module, so a module-scope import would be a cycle; and
        ``EdgeWARN/ctam/__init__.py`` pulls in ``.interface`` and ``.engine``,
        which every CLI startup would then pay for to serve two rarely-used flags.
        """
        if not (args.list_ctam_modules or args.check_ctam_modules):
            return

        from EdgeWARN.ctam import discovery

        root = resolve_ctam_module_dir(args.ctam_module_dir, config_dir=args.config_dir)
        result = discovery.discover_modules(root)

        print(f"CTAM module root: {result.root}")
        if args.check_ctam_modules:
            # Said outright rather than implied: readiness is decided per cycle
            # against that cycle's input catalog, which does not exist outside a
            # running cycle, so this cannot and does not check it.
            print("Checking installed manifests only; module readiness is evaluated per cycle.")

        if not result.root_present:
            print("OK   no module root; an operator who installs no modules is a supported configuration")
            print("0 CTAM module manifest(s) checked")
            sys.exit(0)

        failures = 0
        for module in result.modules:
            if module.state == discovery.STATE_INVALID:
                failures += 1
                print(f"FAIL {module.module_id} ({module.state}) -> {module.reason}")
            else:
                print(f"OK   {module.module_id} ({module.state}) -> {module.reason or 'manifest is valid'}")

        total = len(result.modules)
        if failures:
            print(f"{failures}/{total} CTAM module manifest(s) failed validation")
        else:
            print(f"All {total} CTAM module manifest(s) passed validation")

        # --list is a report and succeeds whatever it found; --check is the gate.
        sys.exit(1 if failures and args.check_ctam_modules else 0)

    @staticmethod
    def _resolve_common_processing_args(args):
        # Both parsers share these four, so resolving here is what gives
        # process_historical.py the same YAML defaults run.py gets.
        filesystem = config_loader.load_config("filesystem", config_dir=args.config_dir)
        args.base_dir = str(overlay.resolve_base_dir(args.base_dir, filesystem))
        run_cfg = config_loader.load_config("runtime", config_dir=args.config_dir)["run"]
        args.profile = overlay.resolve(args.profile, yaml_value=run_cfg["profile"], key="run.profile")
        args.disable_ctam = overlay.resolve(args.disable_ctam, yaml_value=run_cfg["disable_ctam"], key="run.disable_ctam")
        args.disable_tracking = overlay.resolve(args.disable_tracking, yaml_value=run_cfg["disable_tracking"], key="run.disable_tracking")
        args.disable_polygon_expansion = overlay.resolve(args.disable_polygon_expansion, yaml_value=run_cfg["disable_polygon_expansion"], key="run.disable_polygon_expansion")
        # Resolved through util.ctam_config so the repo-root anchoring for a
        # relative value happens once, and exported so an accessory child -- which
        # is spawned with no argv -- discovers modules from the same root.
        args.ctam_module_dir = resolve_ctam_module_dir(args.ctam_module_dir, config_dir=args.config_dir)
        export_ctam_module_dir(args.ctam_module_dir)

        detection_cfg = config_loader.load_config("detection", config_dir=args.config_dir)["detection"]
        args.refl_threshold = overlay.resolve(args.refl_threshold, yaml_value=detection_cfg["refl_threshold"], key="detection.refl_threshold")
        args.min_seed_percentage = overlay.resolve(args.min_seed_percentage, yaml_value=detection_cfg["min_seed_percentage"], key="detection.min_seed_percentage")
        args.drop_offset = overlay.resolve(args.drop_offset, yaml_value=detection_cfg["drop_offset"], key="detection.drop_offset")

    def get_args(self):
        parser = cli.build_service_parser("edgewarn")
        args = parser.parse_args()
        self._export_config_dir(args)
        self._run_ctam_diagnostics(args)

        runtime_cfg = config_loader.load_config("runtime", config_dir=args.config_dir)["run"]
        args.lat_limits = overlay.resolve(args.lat_limits, yaml_value=list(runtime_cfg["lat_limits"]), key="run.lat_limits")
        args.lon_limits = overlay.resolve(args.lon_limits, yaml_value=list(runtime_cfg["lon_limits"]), key="run.lon_limits")
        args.disable_ewmrs = overlay.resolve(args.disable_ewmrs, yaml_value=runtime_cfg["disable_ewmrs"], key="run.disable_ewmrs")
        args.disable_nws = overlay.resolve(args.disable_nws, yaml_value=runtime_cfg["disable_nws"], key="run.disable_nws")
        args.disable_metar = overlay.resolve(args.disable_metar, yaml_value=runtime_cfg["disable_metar"], key="run.disable_metar")
        args.disable_wpc = overlay.resolve(args.disable_wpc, yaml_value=runtime_cfg["disable_wpc"], key="run.disable_wpc")
        args.disable_goes = overlay.resolve(args.disable_goes, yaml_value=runtime_cfg["disable_goes"], key="run.disable_goes")
        args.disable_nexrad = overlay.resolve(args.disable_nexrad, yaml_value=runtime_cfg["disable_nexrad"], key="run.disable_nexrad")
        args.mrms_core_only = overlay.resolve(args.mrms_core_only, yaml_value=runtime_cfg["mrms_core_only"], key="run.mrms_core_only")
        self._resolve_common_processing_args(args)

        if len(args.lat_limits) != 2 or len(args.lon_limits) != 2:
            print("ERROR: Latitude and longitude limits must each have exactly 2 numeric values.")
            sys.exit(1)
        if args.lat_limits == [0, 0] or args.lon_limits == [0, 0]:
            print("ERROR: lat_limits or lon_limits not specified! They must be two numeric values each.")
            sys.exit(1)
        self._validate_common_args(args)

        args.lon_limits = [lon % 360 for lon in args.lon_limits]
        return args

    def get_historical_args(self):
        parser = argparse.ArgumentParser(description="Process EdgeWARN data historically.")
        parser.add_argument("--start", type=str, required=True, help="Start timestamp (ISO, e.g. 2023-01-01T12:00:00)")
        parser.add_argument("--end", type=str, required=True, help="End timestamp (ISO)")
        parser.add_argument("--lat", nargs=2, type=float, default=None, help="Latitude limits (min max) (default: from historical.yaml)")
        parser.add_argument("--lon", nargs=2, type=float, default=None, help="Longitude limits (min max) (default: from historical.yaml)")
        self._add_common_processing_args(parser)
        args = parser.parse_args()
        self._export_config_dir(args)
        self._run_ctam_diagnostics(args)

        historical_cfg = config_loader.load_config("historical", config_dir=args.config_dir)["historical"]
        args.lat = overlay.resolve(args.lat, yaml_value=list(historical_cfg["lat"]), key="historical.lat")
        args.lon = overlay.resolve(args.lon, yaml_value=list(historical_cfg["lon"]), key="historical.lon")
        self._resolve_common_processing_args(args)
        self._validate_common_args(args)
        return args

    def write_info(self, msg):
        self._write("INFO:", msg)

    def write_debug(self, msg):
        self._write("DEBUG:", msg)

    def write_warning(self, msg):
        self._write("WARN:", msg)

    def write_error(self, msg):
        self._write("ERROR:", msg)

    def write_perf(self, msg):
        self._write("[PERF]", msg)


class PerformanceTimer:
    def __init__(self, io_manager, operation, trace_id=None, threshold_ms=0):
        self.io_manager = io_manager
        self.operation = operation
        self.trace_id = trace_id or "NO_TRACE"
        self.threshold_ms = threshold_ms
        self.start_time = 0.0

    def __enter__(self):
        self.start_time = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = (time.time() - self.start_time) * 1000
        if duration_ms >= self.threshold_ms:
            self.io_manager.write_perf(f"[{self.trace_id}] {self.operation}: {duration_ms:.2f}ms")
