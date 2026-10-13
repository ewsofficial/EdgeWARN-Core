from common.ingest.replay import guard_cleanup
from datetime import datetime
from functools import partial
from pathlib import Path
import heapq
import asyncio
import os
import sys
from types import MappingProxyType

MRMS_REGISTRY = None
MRMS_CONFIG_DIR = None
MRMS_PATHS = MappingProxyType({})
_MRMS_ALIASES = MappingProxyType({})


def __getattr__(name):
    if name in MRMS_PATHS:
        return MRMS_PATHS[name]
    if name in _MRMS_ALIASES:
        return _MRMS_ALIASES[name]
    raise AttributeError(f"util.file has no enabled path {name!r}")


def verify_mrms_containment(registry):
    base = registry.base_dir.resolve()
    data = registry.base_dir / "data"
    if not data.resolve().is_relative_to(base):
        raise ValueError(f"MRMS data directory escapes base directory: {data}")
    for spec in registry.products:
        if not spec.directory.resolve().is_relative_to(data.resolve()):
            raise ValueError(f"MRMS product directory escapes data directory: {spec.directory}")


def bind_mrms_paths(registry, *, expected_fingerprint=None):
    """Bind without creating directories; children may verify a parent generation."""
    global MRMS_REGISTRY, MRMS_PATHS, _MRMS_ALIASES
    from common.ingest.mrms.core_contract import LEGACY_ALIASES
    if expected_fingerprint is not None and registry.fingerprint != expected_fingerprint:
        raise ValueError("MRMS registry fingerprint differs from parent generation")
    verify_mrms_containment(registry)
    for name in LEGACY_ALIASES:
        globals().pop(name, None)
    MRMS_REGISTRY = registry
    MRMS_PATHS = registry.paths_by_name()
    _MRMS_ALIASES = registry.legacy_paths()


def ensure_mrms_directories(registry):
    """Explicit producer startup hook: call only after complete preflight."""
    verify_mrms_containment(registry)
    for spec in registry.products:
        spec.directory.mkdir(parents=True, exist_ok=True)
    verify_mrms_containment(registry)

from util.file_config import cleanup_max_age_minutes, cleanup_max_files
from util.io import IOManager

io_manager = IOManager("[Util]")
BASE_DIR: Path = Path(".")


class _FromCatalog:
    """Sentinel for ``max_files``, whose ``None`` already means "no count cap".

    The RAP pre-download sweep passes ``None`` deliberately to clean by age alone,
    so ``None`` cannot double as "caller said nothing".
    """

    def __repr__(self) -> str:
        return "<from filesystem.yaml>"


_FROM_CATALOG = _FromCatalog()


def _log(method, message):
    if hasattr(io_manager, method):
        getattr(io_manager, method)(message)


def _find_existing_path(candidates, missing_message=None):
    for candidate in candidates:
        candidate_path = Path(candidate)
        if candidate_path.exists():
            return candidate_path
    if missing_message:
        io_manager.write_warning(missing_message)
    return Path(candidates[0]) if candidates else Path(".")


def _define_paths(base_path, config_dir=None):
    global BASE_DIR
    global DATA_DIR, ALERTS_DIR
    global EDGEWARN_ALERTS_DIR, EDGEWARN_ALERTS_IDS_DIR, EDGEWARN_ALERTS_TS_DIR
    global MRMS_RALA_DIR, MRMS_NLDN_DIR, MRMS_ECHOTOP18_DIR, MRMS_ECHOTOP30_DIR, MRMS_ECHOTOP50_DIR
    global MRMS_QPE_DIR, MRMS_PRECIPRATE_DIR, MRMS_PROBSEVERE_DIR
    global MRMS_FLASH_CREST_MAXUNIT_DIR, MRMS_FLASH_ARIMAX_DIR, MRMS_FLASH_ARI30M_DIR, MRMS_FLASH_ARI01H_DIR
    global MRMS_FLASH_HP_MAXUNIT_DIR, MRMS_FLASH_SAC_MAXSOIL_DIR, MRMS_FLASH_FFGMAX_DIR
    global MRMS_RQI_DIR, MRMS_DVIL_DIR, MRMS_VIL_DIR, MRMS_VII_DIR
    global MRMS_AZSHEARLOW_DIR, MRMS_AZSHEARMID_DIR, MRMS_COMPOSITE_DIR
    global MRMS_REF_0C_DIR, MRMS_REFM5C_DIR, MRMS_REFM15C_DIR
    global MRMS_RHOHV_DIR, MRMS_PRECIPTYP_DIR, MRMS_MESH_DIR
    global GOES_GLM_DIR
    global RAP_DIR, STORMCELL_DIR, CELL_DIR
    global GUI_DIR, GUI_NEXRAD_DIR
    global STORMCELL_JSON
    global NEXRAD_LEVEL2_DIR, NEXRAD_LEVEL2_LOW_DIR, NEXRAD_LEVEL2_HIGH_DIR, NEXRAD_LEVEL2_MANIFEST_DIR

    global MRMS_NWS_DIR

    base_path = Path(base_path)
    data_dir = base_path / "data"
    gui_dir = base_path / "gui"
    alerts_dir = data_dir / "Alerts"
    edgewarn_alerts_dir = alerts_dir / "EdgeWARN"
    official_alerts_dir = alerts_dir / "official"
    # Core still matches published official alerts; acquisition has a separate owner.
    MRMS_NWS_DIR = official_alerts_dir

    BASE_DIR = base_path
    DATA_DIR = data_dir
    ALERTS_DIR = alerts_dir
    EDGEWARN_ALERTS_DIR = edgewarn_alerts_dir
    EDGEWARN_ALERTS_IDS_DIR = edgewarn_alerts_dir / "ids"
    EDGEWARN_ALERTS_TS_DIR = edgewarn_alerts_dir / "timestamps"
    MRMS_RALA_DIR = data_dir / "MRMS_ReflectivityAtLowestAltitude"
    MRMS_NLDN_DIR = data_dir / "MRMS_NLDN_CG_001min_AvgDensity"
    MRMS_ECHOTOP18_DIR = data_dir / "MRMS_EchoTop18"
    MRMS_ECHOTOP30_DIR = data_dir / "MRMS_EchoTop30"
    MRMS_ECHOTOP50_DIR = data_dir / "MRMS_EchoTop50"
    MRMS_QPE_DIR = data_dir / "MRMS_QPE"
    MRMS_PRECIPRATE_DIR = data_dir / "MRMS_PrecipRate"
    MRMS_PROBSEVERE_DIR = data_dir / "MRMS_ProbSevere"
    MRMS_FLASH_CREST_MAXUNIT_DIR = data_dir / "MRMS_FLASH_CREST_MAXUNIT"
    MRMS_FLASH_ARIMAX_DIR = data_dir / "MRMS_FLASH_ARIMAX"
    MRMS_FLASH_ARI30M_DIR = data_dir / "MRMS_FLASH_ARI30M"
    MRMS_FLASH_ARI01H_DIR = data_dir / "MRMS_FLASH_ARI01H"
    MRMS_FLASH_HP_MAXUNIT_DIR = data_dir / "MRMS_FLASH_HP_MAXUNIT"
    MRMS_FLASH_SAC_MAXSOIL_DIR = data_dir / "MRMS_FLASH_SAC_MAXSOIL"
    MRMS_FLASH_FFGMAX_DIR = data_dir / "MRMS_FLASH_FFGMAX"
    MRMS_RQI_DIR = data_dir / "MRMS_RadarQualityIndex"
    MRMS_DVIL_DIR = data_dir / "MRMS_VILDensity"
    MRMS_VIL_DIR = data_dir / "MRMS_VIL"
    MRMS_VII_DIR = data_dir / "MRMS_VII"
    MRMS_AZSHEARLOW_DIR = data_dir / "MRMS_MergedAzShear_0-2kmAGL"
    MRMS_AZSHEARMID_DIR = data_dir / "MRMS_MergedAzShear_3-6kmAGL"
    MRMS_COMPOSITE_DIR = data_dir / "MRMS_MergedReflectivityQC"
    MRMS_REF_0C_DIR = data_dir / "MRMS_ReflectivityAt0C"
    MRMS_REFM5C_DIR = data_dir / "MRMS_ReflectivityAtM5C"
    MRMS_REFM15C_DIR = data_dir / "MRMS_ReflectivityAtM15C"
    MRMS_RHOHV_DIR = data_dir / "MRMS_MergedRhoHV"
    MRMS_PRECIPTYP_DIR = data_dir / "MRMS_PrecipFlag"
    MRMS_MESH_DIR = data_dir / "MRMS_MESH"
    GOES_GLM_DIR = data_dir / "GOES_GLM"
    RAP_DIR = data_dir / "RAP"
    STORMCELL_DIR = data_dir / "stormcells"
    CELL_DIR = data_dir / "cells"

    GUI_DIR = gui_dir
    GUI_NEXRAD_DIR = gui_dir / "NEXRAD"
    STORMCELL_JSON = data_dir / "stormcells.json"
    NEXRAD_LEVEL2_DIR = data_dir / "NEXRAD_Level2"
    NEXRAD_LEVEL2_LOW_DIR = NEXRAD_LEVEL2_DIR / "Low"
    NEXRAD_LEVEL2_HIGH_DIR = NEXRAD_LEVEL2_DIR / "High"
    NEXRAD_LEVEL2_MANIFEST_DIR = NEXRAD_LEVEL2_DIR / "manifests"


def initialize_filesystem(base_dir=None, *, config_dir=None, expected_mrms_fingerprint=None):
    """Rebind paths without creating runtime directories or starting workers."""
    global MRMS_REGISTRY, MRMS_PATHS, _MRMS_ALIASES, MRMS_CONFIG_DIR
    from common.config.loader import load_config
    from common.ingest.mrms.registry import build_registry
    catalog = load_config("ingest", config_dir=config_dir)
    base = Path(base_dir or BASE_DIR).expanduser().resolve()
    registry = build_registry(catalog["mrms"], base) if catalog["schema_version"] == 2 else None
    if expected_mrms_fingerprint is not None and (
        registry is None or registry.fingerprint != expected_mrms_fingerprint
    ):
        raise ValueError("MRMS registry fingerprint differs from parent generation")
    if registry is not None:
        verify_mrms_containment(registry)
    _define_paths(base, config_dir=config_dir)
    MRMS_CONFIG_DIR = config_dir
    MRMS_REGISTRY = None
    MRMS_PATHS = MappingProxyType({})
    _MRMS_ALIASES = MappingProxyType({})
    if registry is not None:
        bind_mrms_paths(registry, expected_fingerprint=expected_mrms_fingerprint)


# Phase one of base-directory resolution. Binding 113 path globals is a module-
# scope side effect, and it cannot become lazy cheaply: 36 modules read them as
# `fs.<NAME>` attributes across 250-odd sites. So instead of deferring the bind,
# the bind is made already-correct -- `get_base_dir_arg` is a throwaway parser
# over `sys.argv` that answers `--base_dir` before the real parser exists, which
# closes the window in which a path read between this import and the entry
# point's `initialize_filesystem` call would have pointed at the platform
# default. Phase two is `initialize_filesystem`, still needed for a spawned
# child (no argv) and for a programmatic caller that passes a directory.
#
# `--config-dir` is peeked the same way, and only here: this bind reads
# `filesystem.yaml` for the colormap search path before `export_config_root` has
# published anything. `initialize_filesystem` does not pass it, because by then
# the exported EDGEWARN_CONFIG_DIR is the more current answer.
_define_paths(
    Path(IOManager.get_base_dir_arg()),
    config_dir=IOManager.get_config_dir_arg(),
)


def latest_files(directory, count):
    directory = Path(directory)
    if not directory.exists():
        _log("write_warning", f"{directory} doesn't exist!")
        return None

    if count <= 0:
        return []

    try:
        scandir_iter = os.scandir(directory)
    except Exception:
        _log("write_warning", f"{directory} could not be scanned!")
        return None

    file_entries = []
    names_present = set()
    with scandir_iter as it:
        for entry in it:
            names_present.add(entry.name)
            try:
                if entry.is_file():
                    file_entries.append(entry)
            except OSError:
                continue

    files = []
    for entry in file_entries:
        name = entry.name
        dot = name.rfind(".")
        suffix_lower = name[dot:].lower() if dot >= 0 else ""
        if suffix_lower == ".idx" or ".part" in name.lower():
            continue
        if suffix_lower == ".gz" and name[:dot] in names_present:
            continue

        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue

        files.append((mtime, suffix_lower == ".gz", entry.path))

    top_files = heapq.nlargest(count, files, key=lambda item: (item[0], item[1]))
    top_files.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in top_files]


def clean_idx_files(folders):
    for folder in folders:
        folder = Path(folder)
        if not folder.exists():
            _log("write_error", f"Folder not found: {folder}")
            continue
        idx_files = list(folder.rglob("*.idx"))
        if not idx_files:
            _log("write_debug", f"No IDX files in folder: {folder}")
            continue
        deleted = 0
        for file_path in idx_files:
            try:
                file_path.unlink()
                deleted += 1
            except Exception as e:
                _log("write_error", f"Failed to delete IDX file {file_path}: {e}")
        if deleted > 0:
            _log("write_debug", f"Deleted {deleted} files in {folder}")


def _is_safe_directory(directory: Path, allow_logical_inside=False):
    base_dir = globals().get("BASE_DIR", Path("."))
    try:
        if allow_logical_inside:
            return directory.absolute().is_relative_to(base_dir.absolute()) or directory.resolve().is_relative_to(base_dir.resolve())
        return directory.resolve().is_relative_to(base_dir.resolve())
    except Exception:
        return False


@guard_cleanup
def clean_old_files(directory: Path, max_age_minutes=None, max_files=_FROM_CATALOG):
    directory = Path(directory)
    base_dir = globals().get("BASE_DIR", Path("."))
    if not _is_safe_directory(directory, allow_logical_inside=True):
        _log("write_error", f"SAFETY ERROR: Attempting to clean {directory} which is not inside {base_dir}")
        return

    if max_age_minutes is None:
        max_age_minutes = cleanup_max_age_minutes()
    if max_files is _FROM_CATALOG:
        max_files = cleanup_max_files()

    now = datetime.now().timestamp()
    cutoff = now - (max_age_minutes * 60)
    files_deleted = 0
    kept_files = []

    for file_path in directory.glob("*"):
        if not file_path.is_file() or file_path.suffix.lower() == ".idx":
            continue
        try:
            mtime = file_path.stat().st_mtime
            if mtime < cutoff:
                file_path.unlink()
                files_deleted += 1
            else:
                kept_files.append((file_path, mtime))
        except Exception as e:
            _log("write_error", f"Could not process/delete {file_path.name}: {e}")

    if max_files is not None and len(kept_files) > max_files:
        kept_files.sort(key=lambda item: item[1])
        for file_path, _ in kept_files[:len(kept_files) - max_files]:
            try:
                file_path.unlink()
                files_deleted += 1
            except Exception as e:
                _log("write_error", f"Could not delete {file_path.name}: {e}")

@guard_cleanup
def clean_files_by_age(directory: Path, max_age_minutes=None):
    directory = Path(directory)
    base_dir = globals().get("BASE_DIR", Path("."))
    if not _is_safe_directory(directory):
        _log("write_error", f"SAFETY ERROR: Attempting to clean {directory} which is not inside {base_dir}")
        return

    if max_age_minutes is None:
        max_age_minutes = cleanup_max_age_minutes()

    now = datetime.now().timestamp()
    cutoff = now - (max_age_minutes * 60)
    files_deleted = 0
    for file_path in directory.glob("*"):
        if not file_path.is_file():
            continue
        try:
            if file_path.stat().st_mtime < cutoff:
                file_path.unlink()
                files_deleted += 1
        except Exception as e:
            _log("write_error", f"Could not process/delete {file_path.name}: {e}")


# The async wrappers forward whatever they were given so the sync cleaners stay
# the single place either value is resolved.
async def async_clean_old_files(directory: Path, max_age_minutes=None, max_files=_FROM_CATALOG):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, partial(clean_old_files, directory, max_age_minutes=max_age_minutes, max_files=max_files))


async def async_clean_files_by_age(directory: Path, max_age_minutes=None):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, partial(clean_files_by_age, directory, max_age_minutes=max_age_minutes))
