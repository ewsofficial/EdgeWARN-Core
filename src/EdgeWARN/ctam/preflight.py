"""Read-only Core startup and StormProb input dependency checks."""

from __future__ import annotations

import difflib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

from common.config.loader import load_config
from common.config.mrms_products import parse_product_id
from common.ingest.mrms.core_contract import LEGACY_ALIASES, PROTECTED_IDS
from common.ingest.mrms.registry import build_registry
from EdgeWARN.stormprob import features

from . import discovery
from .manifest import ManifestError, _catalog_products, parse_selector


class PreflightError(RuntimeError):
    """A deployment dependency must be corrected before Core starts."""


class StormProbDependencyError(RuntimeError):
    """Fatal, process-wide StormProb input failure (never a module skip)."""


@dataclass(frozen=True)
class StormProbSource:
    product: str
    features: tuple[str, ...]
    role: str = "current"
    max_age_seconds: float = 180.0


STORMPROB_DEPENDENCY_VERSION = 1
STORMPROB_RAP_SCALARS = frozenset({
    "u10m", "v10m", "temp_2m", "dewpoint_2m", "dewpoint_depression",
    "freezing_level_height", "freezing_level_m",
})
# The sources of these statistics are the integration mapping and ProbSevere
# field map, rather than the similarly named optional raw MESH product.
STORMPROB_MRMS_SOURCES = (
    StormProbSource("Reflectivity_0C_00.50", ("Ref0",)),
    StormProbSource("Reflectivity_-5C_00.50", ("Ref5",)),
    StormProbSource("Reflectivity_-15C_00.50", ("Ref15",)),
    StormProbSource("MergedReflectivityAtLowestAltitude_00.50", ("maxRALA",)),
    StormProbSource("PrecipRate_00.00", ("maxPrecipRate",)),
    StormProbSource("VIL_00.50", ("maxVIL", "p50VIL", "p90VIL", "p95VIL")),
    StormProbSource("VIL_Density_00.50", ("maxVILDensity", "p50VILDensity", "p90VILDensity", "p95VILDensity")),
    StormProbSource("VII_00.50", ("maxVII",)),
    StormProbSource("EchoTop_18_00.50", ("maxEchoTop18", "p90EchoTop18", "p95EchoTop18")),
    StormProbSource("EchoTop_30_00.50", ("maxEchoTop30", "p90EchoTop30")),
    StormProbSource("EchoTop_50_00.50", ("p90EchoTop50",)),
    StormProbSource("MergedAzShear_0-2kmAGL_00.50", ("maxAzShearLow", "p95AzShearLow")),
    StormProbSource("MergedAzShear_3-6kmAGL_00.50", ("maxAzShearMid", "p95AzShearMid")),
)


def _enabled_mrms(config_dir: str | Path | None, base_dir: str | Path) -> frozenset[str]:
    ingest = load_config("ingest", config_dir=config_dir)
    if ingest["schema_version"] == 2:
        return frozenset(spec.product_id for spec in build_registry(
            ingest["mrms"], Path(base_dir).expanduser().resolve()).products)
    # The shipped v1 tree remains supported until the coordinated phase 8
    # release. ProbSevere has a null source modifier but a real manifest ID.
    configured = frozenset(
        "ProbSevere" if item["region"] == "ProbSevere" else item["product"]
        for item in ingest["mrms"]["products"]
    )
    missing_core = PROTECTED_IDS - configured
    if missing_core:
        raise PreflightError(
            "v1 ingest.yaml omits protected Core products: "
            + ", ".join(sorted(missing_core))
            + "; restore them or migrate to ingest v2 before starting Core"
        )
    return configured


def _raw_declaration_errors(
    root: Path, enabled: frozenset[str], retention: dict[str, tuple[float, int | None]],
    config_dir: str | Path | None,
) -> list[str]:
    """Audit before discovery can turn a malformed enabled module into a status."""
    if not root.exists():
        return []
    if not root.is_dir():
        raise PreflightError(f"CTAM module root {root} is not a directory")
    errors = []
    resolved_root = root.resolve()
    for path in sorted(root.glob("*/module.toml")):
        if path.parent.name.startswith((".", "_")):
            continue
        if not path.parent.resolve().is_relative_to(resolved_root):
            continue  # Discovery reports the escaping module directory.
        try:
            with path.open("rb") as handle:
                raw = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        module_enabled = raw.get("enabled", True)
        if not isinstance(module_enabled, bool):
            errors.append(f"{path}: enabled must be a boolean")
            continue
        if not module_enabled:
            continue
        if "requires" not in raw:
            errors.append(
                f"{path}: enabled module must declare requires = [] or [[requires]]; "
                "add its host input selectors, then restart"
            )
            continue
        blocks = raw["requires"]
        if not isinstance(blocks, list):
            errors.append(f"{path}: requires must be an array or [[requires]] tables")
            continue
        for index, block in enumerate(blocks):
            if not isinstance(block, dict) or "selector" not in block:
                errors.append(f"{path}: requires[{index}] must declare a selector")
                continue
            try:
                selector = parse_selector(block["selector"])
            except ManifestError as exc:
                errors.append(f"{path}: {exc}")
                continue
            required = block.get("required", True)
            if not isinstance(required, bool):
                errors.append(f"{path}: requires[{index}].required must be a boolean")
                continue
            if selector.kind != "input":
                continue
            if selector.family == "mrms":
                try:
                    parse_product_id(f"MRMS_{selector.product}")
                except ValueError as exc:
                    errors.append(f"{path}: invalid MRMS selector {selector.raw}: {exc}")
                    continue
            if selector.family == "mrms" and selector.product not in enabled:
                if required:
                    near = difflib.get_close_matches(selector.product, sorted(enabled), n=3)
                    errors.append(
                        f"CTAM module {raw.get('id', path.parent.name)} requires {selector.product}, "
                        f"but MRMS_{selector.product} is not enabled. Manifest: {path}. "
                        f"Config: {Path(config_dir or 'config') / 'ingest.yaml'}. "
                        f"Add MRMS_{selector.product} to mrms.products or disable the module"
                        + (f". Nearby enabled IDs: {', '.join(near)}" if near else "")
                    )
                continue
            if selector.family != "mrms":
                known = _catalog_products(selector.family)
                if known and selector.product not in known:
                    errors.append(
                        f"{path}: selector {selector.raw} names unknown "
                        f"{selector.family} product {selector.product}"
                    )
            if selector.role == "previous" and required:
                age_limit, count_limit = retention[selector.family]
                if count_limit is not None and count_limit < 2:
                    errors.append(
                        f"{path}: {selector.raw} requires previous input but local "
                        f"retention keeps only {count_limit} file(s)"
                    )
                max_age = block.get("max_age_seconds")
                if (isinstance(max_age, (int, float)) and not isinstance(max_age, bool)
                        and max_age > age_limit):
                    errors.append(
                        f"{path}: {selector.raw} needs {max_age:g}s history but "
                        f"local retention keeps only {age_limit:g}s"
                    )
    return errors


def _explicitly_disabled(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle).get("enabled") is False
    except (OSError, tomllib.TOMLDecodeError):
        return False


def _validate_stormprob_sources(enabled: frozenset[str], config_dir) -> list[str]:
    missing = [source for source in STORMPROB_MRMS_SOURCES if source.product not in enabled]
    errors = [
        f"MRMS_{source.product} (features: {', '.join(source.features)})"
        for source in missing
    ]
    # An enabled product is insufficient when its statistic was removed from
    # integration.yaml. This check is pure and does not resolve input paths.
    integration = load_config("integration", config_dir=config_dir)
    stats_by_key = {item["key"]: item for item in integration["stats_datasets"]}
    for source in STORMPROB_MRMS_SOURCES:
        absent = set(source.features) - stats_by_key.keys()
        if absent:
            errors.append(f"integration.yaml lacks {', '.join(sorted(absent))} from MRMS_{source.product}")
        for feature in source.features:
            entry = stats_by_key.get(feature)
            if entry is None:
                continue
            actual = entry.get("product") or LEGACY_ALIASES.get(entry.get("filepath"))
            if actual != source.product:
                errors.append(
                    f"integration.yaml maps StormProb {feature} to {actual!r}; "
                    f"expected MRMS_{source.product}"
                )
    declared = {name for source in STORMPROB_MRMS_SOURCES for name in source.features}
    if not declared <= set(features.CURRENT_FEATURE_ORDER):
        errors.append("StormProb source declaration differs from the model feature order")
    probsevere_features = (set(features.IMPORTANT_SCALAR_PROPERTY_FEATURES)
                           - declared - STORMPROB_RAP_SCALARS)
    unmapped_probsevere = probsevere_features - set(integration["probsevere_field_map"])
    if unmapped_probsevere:
        errors.append("integration.yaml lacks ProbSevere fields: "
                      + ", ".join(sorted(unmapped_probsevere)))
    rap = integration["rap_products"]
    rap_keys = {item.get("key") for item in rap["products"]}
    rap_keys.update(item["key"] for item in rap["derived"])
    if not STORMPROB_RAP_SCALARS <= rap_keys:
        errors.append("integration.yaml lacks RAP features: "
                      + ", ".join(sorted(STORMPROB_RAP_SCALARS - rap_keys)))
    if tuple(rap["isobaric_levels_mb"][::-1]) != features.PRESSURE_LEVELS_HPA:
        errors.append("integration.yaml RAP wind levels differ from StormProb model")
    return errors


def check_core_startup(
    *, config_dir: str | Path | None, base_dir: str | Path,
    module_root: str | Path | None, disable_ctam: bool,
    disable_ctam_modules: bool, disable_stormprob: bool,
    mrms_core_only: bool = False,
) -> discovery.DiscoveryResult | None:
    """Validate dependencies without creating directories or starting workers."""
    enabled = _enabled_mrms(config_dir, base_dir)
    issues = []
    result = None
    if not disable_ctam and not disable_ctam_modules:
        root = Path(module_root) if module_root is not None else discovery.discover_modules(config_dir=config_dir).root
        ingest_mrms = load_config("ingest", config_dir=config_dir)["mrms"]
        retention_seconds = (ingest_mrms["cleanup_max_age_minutes"] * 60
                             if ingest_mrms["remove_old_files"] else float("inf"))
        ingest = load_config("ingest", config_dir=config_dir)
        rap = load_config("synoptic_rap", config_dir=config_dir)["rap"]
        filesystem = load_config("filesystem", config_dir=config_dir)
        retention = {
            "mrms": (retention_seconds, filesystem["cleanup_defaults"]["max_files"]),
            "goes": (ingest["goes"]["cleanup_max_age_minutes"] * 60,
                     ingest["goes"]["max_files_per_spec"]),
            "rap": (rap["max_age_minutes"] * 60, rap["max_files"]),
        }
        issues.extend(_raw_declaration_errors(root, enabled, retention, config_dir))
        result = discovery.discover_modules(root)
        for module in result.modules:
            if module.state == discovery.STATE_SKIPPED_DISABLED:
                continue
            if module.state == discovery.STATE_INVALID:
                if (module.directory.resolve().is_relative_to(root.resolve())
                        and _explicitly_disabled(module.directory / "module.toml")):
                    continue
                if "requires" in (module.reason or "") or "selector" in (module.reason or ""):
                    issues.append(f"{module.directory / 'module.toml'}: {module.reason}")
                continue
    if not disable_ctam and not disable_stormprob:
        missing = _validate_stormprob_sources(enabled, config_dir)
        if mrms_core_only:
            missing.append("RAP environment and wind fields (mrms-core-only disables RAP)")
        if missing:
            issues.append(
                "WARNING: StormProb required inputs are disabled or unmapped: "
                + "; ".join(missing)
                + f". Enable them in {Path(config_dir or 'config') / 'ingest.yaml'} and integration.yaml, "
                "or set runtime.run.disable_stormprob=true / use --disable-stormprob, then restart"
            )
        else:
            from EdgeWARN.stormprob.assets import manifest_path, validate_assets
            from EdgeWARN.stormprob import onnx_runtime
            try:
                asset_root = validate_assets()
                check = features.verify_against_manifest()
                if not check["ok"]:
                    raise RuntimeError(f"model feature contract: {check['reason']}")
                manifest = json.loads(manifest_path().read_text(encoding="utf-8"))
                for model in ("radial", "motion"):
                    info = manifest["onnx_export"]["models"][model]
                    if onnx_runtime._hash(asset_root / info["file"]) != info["sha256"]:
                        raise RuntimeError(f"{model} ONNX graph hash mismatch")
                onnx_runtime.load_calibrator(asset_root, manifest_path())
                features.load_normalization()
            except (FileNotFoundError, RuntimeError, ValueError, KeyError, OSError) as exc:
                issues.append(f"WARNING: StormProb assets are incompatible: {exc}")
    if issues:
        raise PreflightError("Cannot start Core:\n" + "\n".join(issues) + "\nNo workers or downloads were started.")
    return result


def validate_stormprob_cycle(cells, input_manifest) -> None:
    """Gate all candidate cells before any forecast or alert publication."""
    if not cells:
        return
    problems = []
    if input_manifest is None:
        problems.append("final CTAM input snapshot is missing")
    else:
        cycle_time = input_manifest.cycle_time
        for source in STORMPROB_MRMS_SOURCES:
            record = input_manifest.latest_for_product(source.product)
            age = ((cycle_time - record.analysis_time).total_seconds()
                   if record is not None else float("inf"))
            if (record is None or record.family != "mrms" or not record.validated
                    or not record.local_path.is_file()
                    or age < 0 or age > source.max_age_seconds):
                problems.append(f"MRMS_{source.product}: unavailable, stale or invalid ({', '.join(source.features)})")
        rap = [record for record in input_manifest.current_inputs(family="rap")
               if record.validated and 0 <= (cycle_time - record.analysis_time).total_seconds()
               <= input_manifest.rap_max_age_seconds]
        if not rap:
            problems.append("RAP environment and wind fields: unavailable or stale")
    for cell in cells:
        observation = cell.get("stormprob", {}).get("observation")
        if not isinstance(observation, dict) or not observation.get("inference_ready"):
            problems.append(f"cell {cell.get('id')}: observation unavailable or invalid")
            continue
        bad = [name for name in features.UNIVERSAL_PROPERTY_FEATURES
               if observation.get("quality", {}).get(name) != "ok"]
        if bad:
            problems.append(f"cell {cell.get('id')}: missing or invalid features {', '.join(bad[:12])}"
                            + (f" (+{len(bad)-12} more)" if len(bad) > 12 else ""))
    if problems:
        raise StormProbDependencyError(
            "WARNING: Cannot continue Core: StormProb dependency failure: "
            + "; ".join(problems) + ". Core is exiting with a nonzero status."
        )
