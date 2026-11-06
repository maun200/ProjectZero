import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from manage_worker import run  # noqa: E402


def test_cli_calls_admin_endpoints_with_bearer():
    seen = []
    def handler(request):
        seen.append((request.method, request.url.path, request.headers["Authorization"]))
        return httpx.Response(200, json={"ok": True})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run("revoke", "w1", client, "http://127.0.0.1:8765", "synthetic-admin")
        run("rotate", "w1", client, "http://127.0.0.1:8765", "synthetic-admin")
        run("list", None, client, "http://127.0.0.1:8765", "synthetic-admin")
    assert seen == [("POST", "/api/admin/workers/w1/revoke", "Bearer synthetic-admin"),
                    ("POST", "/api/admin/workers/w1/rotate", "Bearer synthetic-admin"),
                    ("GET", "/api/admin/workers", "Bearer synthetic-admin")]


def test_cli_requires_worker_id_for_mutations():
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
        with pytest.raises(ValueError):
            run("revoke", None, client, "http://127.0.0.1:8765", "synthetic-admin")
