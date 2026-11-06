import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from ollama_preflight import check_backend  # noqa: E402

MODEL = "qwen2.5-coder:1.5b"


def client_for(tags, loaded):
    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": tags})
        return httpx.Response(200, json={"models": loaded})
    return httpx.Client(transport=httpx.MockTransport(handler))


def meminfo(tmp_path, kib):
    path = tmp_path / "meminfo"
    path.write_text(f"MemTotal: 16000000 kB\nMemAvailable: {kib} kB\n")
    return path


def test_accepts_installed_idle_model_with_enough_ram(tmp_path):
    with client_for([{"name": MODEL, "size": 1}], []) as c:
        assert check_backend(c, MODEL, 3.0, meminfo=meminfo(tmp_path, 4 * 1024 * 1024))["name"] == MODEL


@pytest.mark.parametrize("tags,loaded,kib,message", [
    ([], [], 8 * 1024 * 1024, "not installed"),
    ([{"name": MODEL}], [{"name": "other"}], 8 * 1024 * 1024, "Another model"),
    ([{"name": MODEL}], [], 1024 * 1024, "Not enough"),
])
def test_rejects_unsafe_states(tmp_path, tags, loaded, kib, message):
    with client_for(tags, loaded) as c, pytest.raises(RuntimeError, match=message):
        check_backend(c, MODEL, 3.0, meminfo=meminfo(tmp_path, kib))
