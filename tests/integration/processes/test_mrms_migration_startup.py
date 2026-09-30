"""Actual entry points reject interrupted migrations before runtime creation."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize('argv', [
    ['src/run_edgewarn.py', '--disable-stormprob', '--disable-ctam'],
    ['src/run_ewmrs.py', '--disable-nws'],
    ['src/run_nexrad.py'],
    ['src/run_all.py', '--services', 'ewmrs'],
    ['src/process_historical.py', '--start', '2024-01-01T00:00:00', '--end', '2024-01-01T00:01:00'],
    ['-c', 'from edgewarn_cli.main import main; raise SystemExit(main())', 'run', 'core'],
])
def test_interrupted_migration_prevents_startup(tmp_path, argv):
    config = tmp_path / 'config'
    shutil.copytree(ROOT / 'config', config)
    journal = config / '.mrms-migration/journal.json'
    journal.parent.mkdir()
    journal.write_text(json.dumps(dict(schema_version=1, status='applying')))
    runtime = tmp_path / 'runtime'
    result = subprocess.run(
        [sys.executable, *argv], cwd=ROOT,
        env={**os.environ, 'PYTHONPATH': str(ROOT / 'src'),
             'EDGEWARN_CONFIG_DIR': str(config), 'EDGEWARN_BASE_DIR': str(runtime)},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0
    assert '--resume or --rollback' in result.stdout + result.stderr
    assert not runtime.exists()
