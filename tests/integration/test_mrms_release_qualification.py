"""Run the reproducible offline release workload in fresh processes."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_mrms_release_transport_qualification():
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(root / 'src'))
    env.pop('EDGEWARN_CONFIG_DIR', None)
    result = subprocess.run([sys.executable, str(root / 'scripts/qualify_mrms_release.py')],
                            cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    measurements = json.loads(result.stdout)
    for scenario, cycles in measurements.items():
        assert len(cycles) == 2
        for cycle in cycles:
            assert cycle['schema_version'] == 2
            assert len(cycle['registry_fingerprint']) == 64
            assert cycle['max_concurrency'] == 8
            assert cycle['active_after'] == cycle['queued_after'] == cycle['pending_tasks_after'] == 0
            assert cycle['optional_failed'] == (18 if scenario == 'short_deadline' else 0)
        assert cycles[0]['published_files'] == cycles[1]['published_files']
