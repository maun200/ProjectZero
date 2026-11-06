"""End-to-end smoke test of an installed `projectzero` command (used by CI on Linux/macOS/Windows).

    python scripts/installer_smoke.py /path/to/projectzero

Starts `projectzero demo --no-browser` in a temporary home, registers an account through the
local app, shares this machine with the mock backend, asks the AI one question and checks that
the answer arrives and credits add up. Uses only the standard library plus httpx (installed
with ProjectZero), no model, no internet.
"""
import os
import re
import subprocess
import sys
import tempfile
import time

import httpx


def wait_for(pattern: str, read, timeout: float = 60) -> re.Match:
    deadline, seen = time.monotonic() + timeout, ""
    while time.monotonic() < deadline:
        seen += read()
        match = re.search(pattern, seen)
        if match:
            return match
        time.sleep(0.2)
    raise SystemExit(f"Timed out waiting for {pattern!r}; output so far:\n{seen}")


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "projectzero"
    home = tempfile.mkdtemp(prefix="pz-smoke-")
    log_path = os.path.join(home, "demo.log")
    log = open(log_path, "w+")
    proc = subprocess.Popen([command, "demo", "--no-browser"], stdout=log, stderr=subprocess.STDOUT,
                            env={**os.environ, "PROJECTZERO_HOME": home, "PYTHONUNBUFFERED": "1"})
    position = 0

    def read() -> str:
        nonlocal position
        with open(log_path) as handle:
            handle.seek(position)
            chunk = handle.read()
            position += len(chunk)
            return chunk

    try:
        match = wait_for(r"app (http://127\.0\.0\.1:\d+/)", read)
        base = match.group(1).rstrip("/")
        client = httpx.Client(base_url=base, timeout=20, trust_env=False)
        for _ in range(50):
            try:
                page = client.get("/")
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        session = re.search(r'const SESSION="([^"]+)"', page.text).group(1)
        client.headers["X-PZ-Session"] = session
        assert client.post("/local/register", json={"name": "smoke-user"}).status_code == 200
        assert client.post("/local/machine", json={"name": "smoke-pc", "model": "demo-model"}).status_code == 200
        assert client.post("/local/settings", json={"contribute": True, "backend": "mock"}).status_code == 200
        job = client.post("/api/jobs", json={"model": "demo-model", "privacy": "community", "allow_untrusted": True,
                                             "messages": [{"role": "user", "content": "smoke test"}]})
        assert job.status_code == 201, job.text
        for _ in range(60):
            status = client.get(f"/api/jobs/{job.json()['id']}").json()
            if status["status"] == "completed":
                break
            time.sleep(0.5)
        assert status["status"] == "completed", status
        assert "smoke test" in status["output"]["content"]
        me = client.get("/api/me").json()
        assert me["credits"] == 50 and me["earned_credits"] == 5, me
        print("installer smoke test: PASS")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


if __name__ == "__main__":
    main()
