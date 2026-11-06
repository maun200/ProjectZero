"""compose.yaml must forward every CAG_* setting the coordinator reads.

compose only passes variables listed under `environment:`; a value that exists only in .env is
silently ignored by the container. This happened twice in this project.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_compose_forwards_every_coordinator_setting():
    read = set(re.findall(r'os\.getenv\("(CAG_[A-Z_]+)"', (ROOT / "coordinator/app/main.py").read_text()))
    compose = (ROOT / "compose.yaml").read_text()
    missing = sorted(name for name in read if not re.search(rf"^\s+{name}:", compose, re.M))
    assert not missing, f"compose.yaml does not forward: {missing}"


def test_env_example_documents_every_coordinator_setting():
    read = set(re.findall(r'os\.getenv\("(CAG_[A-Z_]+)"', (ROOT / "coordinator/app/main.py").read_text()))
    example = (ROOT / ".env.example").read_text()
    missing = sorted(name for name in read if f"{name}=" not in example)
    assert not missing, f".env.example does not document: {missing}"
