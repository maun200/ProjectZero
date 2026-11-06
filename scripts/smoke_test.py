"""End-to-end loopback-only smoke test with a mock model; generates no user data.

Run `python scripts/smoke_test.py` after pip install -e '.[test]'.
"""
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_for(url: str, proc: subprocess.Popen, timeout=10):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if proc.poll() is not None:
            raise RuntimeError("Server process exited early")
        try:
            if httpx.get(url, timeout=1).is_success:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.15)
    raise RuntimeError("Server startup timed out")


def main():
    coord, mock = free_port(), free_port()
    admin, user = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="cag-smoke-") as tmp:
        env = dict(os.environ, CAG_ADMIN_KEY=admin, CAG_USER_KEY=user,
                   CAG_DB_PATH=str(Path(tmp) / "demo.sqlite3"))
        processes = []
        try:
            for appname, port in [("coordinator.app.main:app", coord), ("worker.mock_llm:app", mock)]:
                p = subprocess.Popen([sys.executable, "-m", "uvicorn", appname,
                         "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT,
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                processes.append(p)
            base = f"http://127.0.0.1:{coord}"
            wait_for(base + "/health", processes[0])
            wait_for(f"http://127.0.0.1:{mock}/openapi.json", processes[1])
            auth = {"Authorization": "Bearer " + admin}
            enrolled = httpx.post(base + "/api/admin/workers", headers=auth,
                                   json={"name": "smoke", "model": "demo-model", "trust_level": "trusted"}).json()
            assert "worker_token" in enrolled, "Worker registration failed"
            request = httpx.post(base + "/api/jobs", headers={"Authorization": "Bearer " + user},
                                 json={"model": "demo-model", "messages": [{"role": "user", "content": "health check"}]})
            request.raise_for_status()
            worker_env = dict(env, CAG_COORD_URL=base, CAG_WORKER_TOKEN=enrolled["worker_token"],
                              CAG_MODEL="demo-model", CAG_LLM_BACKEND="openai", CAG_LLM_API_KEY="",
                              CAG_LLM_URL=f"http://127.0.0.1:{mock}/v1/chat/completions")
            subprocess.run([sys.executable, "-m", "worker.client", "--once"], cwd=ROOT,
                           env=worker_env, check=True, stdout=subprocess.DEVNULL, timeout=20)
            response = httpx.get(base + "/api/jobs/" + request.json()["id"],
                                 headers={"Authorization": "Bearer " + user})
            response.raise_for_status()
            result = response.json()
            assert result["status"] == "completed", result
            assert "[MOCK" in result["output"]["content"], result
            print("PASS: coordinator + mock inference + worker + completed job on loopback")
        finally:
            for p in processes:
                p.terminate()
            for p in processes:
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()


if __name__ == "__main__":
    main()
