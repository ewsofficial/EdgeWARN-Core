"""EWMRS render orchestration settings read from ``config/ewmrs_pipeline.yaml``.

Accessors rather than module constants so the catalog is read per call. Render
layers run in ``ProcessPoolExecutor`` workers spawned with no argv, which
re-resolve the config root from ``EDGEWARN_CONFIG_DIR`` after this module is
imported; a module-level read would have frozen the repo default at import time.

The two ``caches`` accessors are the exception: they feed ``lru_cache(maxsize=)``
decorator arguments, which Python evaluates once at import. Changing a cache size
therefore needs a restart, unlike every other key here.
"""

from common.config.loader import load_config
from common.config.overlay import resolve

_CONFIG_NAME = "ewmrs_pipeline"

GOES_CLEANUP_MIN_INTERVAL_ENV = "EWMRS_GOES_CLEANUP_MIN_INTERVAL_SECONDS"
WORKER_BUDGET_MB_ENV = "EWMRS_WORKER_BUDGET_MB"
WORKER_RESERVE_MB_ENV = "EWMRS_WORKER_RESERVE_MB"
TILE_THREADS_ENV = "EWMRS_TILE_THREADS"


def _section(name: str):
    """One top-level section. ``load_config`` is memoized, so this is cheap."""
    return load_config(_CONFIG_NAME)[name]


def render_phase_name() -> str:
    """Label for the generic render phase.

    Only the generic default lives here. ``run_mrms_render_pipeline`` and
    ``run_goes_render_pipeline`` pass "MRMS" and "GOES" as literals because those
    name which caller is running, not a setting an operator would retune -- and
    :func:`worker_budget_mb` dispatches on the "GOES" prefix, so renaming that
    phase would silently change the memory budget.
    """
    return _section("render")["phase_name"]


def render_cleanup_after() -> bool:
    """Whether the generic render phase sweeps the GUI tree when it finishes."""
    return _section("render")["cleanup_after"]


def gui_cleanup_max_age_minutes() -> int:
    """Age above which rendered GUI outputs are removed.

    Distinct from :func:`nexrad_source_max_age_minutes` despite matching it
    today: this is output retention, that is input freshness.
    """
    return _section("render")["gui_cleanup_max_age_minutes"]


def goes_cleanup_max_age_minutes() -> int:
    """Age above which GOES GUI outputs are removed.

    Separate from :func:`gui_cleanup_max_age_minutes` because the GOES phase
    sweeps on its own rate-limited schedule, so the two can be retuned apart.
    """
    return _section("render")["goes_cleanup_max_age_minutes"]


def goes_cleanup_min_interval_seconds() -> float:
    """Minimum spacing between GOES GUI sweeps; ``0`` disables the rate limit.

    Floored at zero so a negative override cannot invert the elapsed-time
    comparison into "never sweep".
    """
    configured = resolve(
        None,
        env_names=(GOES_CLEANUP_MIN_INTERVAL_ENV,),
        yaml_value=float(_section("render")["goes_cleanup_min_interval_seconds"]),
        key="ewmrs_pipeline.render.goes_cleanup_min_interval_seconds",
    )
    return max(0.0, float(configured))


def nexrad_source_max_age_minutes() -> int:
    """How stale a NEXRAD artifact may be and still be worth rendering."""
    return _section("nexrad_gui")["retention_minutes"]


def nexrad_poll_interval_seconds() -> float:
    """Sleep between NEXRAD render poll cycles."""
    return _section("nexrad_gui")["poll_interval_seconds"]


def nexrad_poll_interval_min_seconds() -> float:
    """Floor applied to a caller-supplied poll interval, so no caller can spin."""
    return _section("nexrad_gui")["poll_interval_min_seconds"]


def nexrad_render_max_workers() -> int:
    """Ceiling on the NEXRAD render thread pool; the effective size is
    ``min(this, pending artifact count)``."""
    return _section("nexrad_gui")["max_workers"]


def worker_budget_mb(phase_name: str) -> float:
    """Assumed peak memory of one render worker, in MiB.

    GOES workers hold a full-disk ABI array through reprojection, so they get a
    larger budget than MRMS and everything else. One environment variable covers
    both, matching the pre-extraction behavior.
    """
    budgets = _section("workers")["budget_mb"]
    default = budgets["goes"] if phase_name.upper().startswith("GOES") else budgets["default"]
    return float(
        resolve(
            None,
            env_names=(WORKER_BUDGET_MB_ENV,),
            yaml_value=float(default),
            key="ewmrs_pipeline.workers.budget_mb",
        )
    )


def worker_reserve_mb() -> float:
    """Memory held back for the OS and the parent process, in MiB."""
    return float(
        resolve(
            None,
            env_names=(WORKER_RESERVE_MB_ENV,),
            yaml_value=float(_section("workers")["reserve_mb"]),
            key="ewmrs_pipeline.workers.reserve_mb",
        )
    )


def worker_psutil_fallback_max() -> int:
    """Worker ceiling when ``psutil`` is unavailable and free memory is unknown."""
    return _section("workers")["psutil_fallback_max"]


def worker_max_workers() -> int:
    """Hard ceiling on the EWMRS render process pool."""
    return _section("workers")["max_workers"]


def numeric_thread_cap_value() -> int:
    """Per-worker thread cap for the BLAS-family libraries.

    Set to 1 so the process pool, not the math libraries, owns the parallelism.
    """
    return _section("workers")["numeric_thread_caps"]["value"]


def numeric_thread_cap_variables() -> tuple[str, ...]:
    """Environment variables that receive :func:`numeric_thread_cap_value`.

    One shared value across the whole list; the catalog cannot express per
    variable caps because the code sets them in a single loop.
    """
    return _section("workers")["numeric_thread_caps"]["variables"]


def max_tile_threads() -> int:
    """Ceiling on the tile writer thread pool.

    Combined with the CPU count, unlike ``EWMRS_TILE_THREADS``, which bypasses
    the CPU cap deliberately -- see :func:`~EWMRS.render.render._resolve_tile_workers`.
    """
    return _section("render_threads")["max_tile_threads"]


def tile_index_cache_entries() -> int:
    """``lru_cache`` size for parsed chunk indexes. Read once at import."""
    return _section("caches")["tile_index_entries"]


def colormap_cache_entries() -> int:
    """``lru_cache`` size for parsed colormaps. Read once at import."""
    return _section("caches")["colormap_entries"]
