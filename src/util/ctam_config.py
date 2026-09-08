"""The CTAM module install root, read from ``config/runtime.yaml``.

An accessor rather than a module constant, for the same reason as
``util.file_config``: a ``--config-dir`` may be resolved after this module is
imported, and a module-level read would have frozen the repo default at import
time.

It lives in ``src/util/`` rather than beside the rest of CTAM in
``src/EdgeWARN/ctam/`` for one reason. ``src/util/io.py`` needs
:func:`export_ctam_module_dir` at module scope, and ``EdgeWARN/__init__.py``
imports ``EdgeWARN.pipeline``, which imports ``util.io`` -- so a module-scope
``util`` -> ``EdgeWARN.ctam`` import would close a ``util`` -> ``EdgeWARN`` ->
``util`` cycle. The dependency therefore runs the other way: ``discovery.py``
imports *this* module to find its default root. Do not "tidy" this file into the
``EdgeWARN.ctam`` package.
"""

from __future__ import annotations

import os
from pathlib import Path

from common.config import loader as config_loader
from common.config import overlay

# Named here rather than spelled at the read site so the inventory in
# ``tests/architecture/test_surface_baseline.py`` can resolve it, and so the one
# writer (:func:`export_ctam_module_dir`) and the one reader (the ``env_names``
# below) cannot drift apart.
CTAM_MODULE_DIR_ENV = "EDGEWARN_CTAM_MODULE_DIR"

_CONFIG_NAME = "runtime"


def resolve_ctam_module_dir(cli_value=None, *, config_dir=None) -> Path:
    """Where installed CTAM module manifests are discovered from.

    CLI > environment > ``runtime.yaml``'s ``run.ctam_module_dir``, resolved
    through :func:`common.config.overlay.resolve` so the winning layer is
    recorded for ``overlay.overrides()`` exactly as every other setting is,
    rather than by a private precedence chain only this key would have.

    ``overlay.resolve`` hands a path value back as the raw ``str`` it received --
    its ``_coerce`` converts only ``bool``/``int``/``float`` and falls through for
    anything else -- so the ``Path`` wrap belongs here, at the call site.

    A relative value resolves against the repository root, the way
    ``overlay.resolve_base_dir`` does, and deliberately not against the working
    directory: children spawned by the supervisor inherit neither argv nor a
    predictable CWD, so a bare ``ctam_modules`` has to name one fixed place or
    the parent and its children would scan different trees.
    """
    run_cfg = config_loader.load_config(_CONFIG_NAME, config_dir=config_dir)["run"]
    selected = overlay.resolve(
        cli_value,
        env_names=(CTAM_MODULE_DIR_ENV,),
        yaml_value=run_cfg["ctam_module_dir"],
        key="run.ctam_module_dir",
    )
    path = Path(selected)
    if path.is_absolute():
        return path
    return config_loader.repo_root(config_dir) / path


def export_ctam_module_dir(path) -> None:
    """Publish the resolved module root so spawned children inherit it.

    The same reason ``loader.export_config_root`` exists: an accessory process is
    spawned with no argv, so an operator's ``--ctam-module-dir`` reaches it only
    through the environment. Without this the parent would honour the flag while
    its children fell back to ``runtime.yaml``, and the two would disagree about
    which modules are installed -- silently, because an empty module root is a
    supported configuration rather than an error.
    """
    os.environ[CTAM_MODULE_DIR_ENV] = str(path)
