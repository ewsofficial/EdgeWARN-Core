"""Scan-time GLM acquisition for Core integration."""
from common.ingest.mrms.config import get_goes_modifiers
from common.ingest.mrms.downloader import download_goes_product
from common.ingest.manifest import staged_input_from_path


def acquire_glm_inputs_for_scan(dt):
    """Validate scan-time GLM completions without deleting inventory-owned files.

    The synchronous caller owns this blocking source/NetCDF work; the ingest
    service must dispatch it to a bounded owned worker, never its timer thread.
    """
    import hashlib
    from pathlib import Path
    import xarray as xr
    import util.file as fs
    from common.ingest.objects import CommittedInput
    from common.ingest.manifest import CycleInputManifest
    spec = next((spec for spec in get_goes_modifiers() if spec.is_glm), None)
    if spec is None:
        return ()
    from dataclasses import replace
    import os
    import tempfile
    base = Path(fs.BASE_DIR).resolve()
    staging = base / 'state' / 'ingest-staging'
    if not staging.resolve().is_relative_to(base) or not Path(spec.outdir).resolve().is_relative_to(base):
        raise ValueError("GLM directories escape runtime base directory")
    staging.mkdir(parents=True, exist_ok=True)
    completed = []
    # The existing downloader may merge/replace files. Isolate that work until
    # validation, then publish without replacing any already-pinned bytes.
    with tempfile.TemporaryDirectory(prefix='glm-', dir=staging) as directory:
        stage = Path(directory)
        paths = download_goes_product(replace(spec, outdir=stage), dt, cleanup=False)
        if not paths:
            raise RuntimeError("GLM acquisition returned no usable inputs")
        for path in paths:
            path = Path(path)
            if path.is_symlink() or path.parent.resolve() != stage.resolve():
                raise ValueError("GLM input escapes staging directory")
            if not path.name.startswith('OR_' + spec.product) or path.suffix != '.nc':
                raise ValueError("Unexpected GLM product filename")
            record = staged_input_from_path(spec.label, path, source='s3_sync', family='goes')
            errors = CycleInputManifest(dt, (record,)).validate_alignment()
            if errors:
                raise ValueError('; '.join(errors))
            with xr.open_dataset(path) as dataset:
                for variable in ('flash_lat', 'flash_lon', 'flash_energy'):
                    if variable not in dataset:
                        raise ValueError(f"GLM missing {variable}")
                    dataset[variable].load()
                if not (dataset.flash_lat.shape == dataset.flash_lon.shape == dataset.flash_energy.shape):
                    raise ValueError("GLM flash coordinate/energy shapes differ")
            with path.open('rb') as handle:
                digest = hashlib.file_digest(handle, 'sha256').hexdigest()
            destination = Path(spec.outdir) / path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_symlink():
                raise ValueError("Refusing symlink at GLM publication target")
            reused = False
            try:
                os.link(path, destination)
            except FileExistsError:
                with destination.open('rb') as handle:
                    existing = hashlib.file_digest(handle, 'sha256').hexdigest()
                if digest != existing:
                    raise ValueError("Conflicting content for an already published GLM observation")
                reused = True
            final = replace(record, path=str(destination))
            completed.append(CommittedInput(final, digest, str(destination), reused=reused))
    return tuple(completed)
