"""Multi-process mock load test; slow (~20 s)."""
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.slow
@pytest.mark.skipif(os.name != 'posix', reason='process-group cleanup requires POSIX')
def test_load_test_survives_killed_worker(tmp_path):
    root = Path(__file__).resolve().parent.parent
    proc = subprocess.Popen([sys.executable, 'scripts/load_test_mock.py', '--workers', '3',
                             '--jobs', '9', '--kill-one'], cwd=root,
                            env=dict(os.environ, TMPDIR=str(tmp_path)), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=150)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
    assert proc.returncode == 0, stderr[-2000:]
    result = json.loads(stdout.strip().splitlines()[-1])
    assert result['status'] == 'PASS' and result['killed_job_requeued'] is True
