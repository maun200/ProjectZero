"""The mock demonstration must remain independent of operator backend settings."""
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.name != 'posix', reason='Isolated process-group cleanup requires POSIX')
def test_mock_smoke_ignores_inherited_ollama_configuration(tmp_path):
    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ, TMPDIR=str(tmp_path), CAG_LLM_BACKEND='ollama',
               CAG_OLLAMA_NUM_CTX='invalid', CAG_LLM_API_KEY='synthetic-unused-key')
    proc = subprocess.Popen([sys.executable, 'scripts/smoke_test.py'], cwd=root, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        # On timeout, terminate the whole test-only group, including server grandchildren.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
    assert proc.returncode == 0, stderr
    assert 'PASS: coordinator + mock inference + worker + completed job on loopback' in stdout
