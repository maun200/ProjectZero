"""Local AI-chat history for the ProjectZero client.

One JSON file per conversation under ``<root>/<account-key>/<chat-id>.json``. Chats never leave
this computer; like the config file they are written owner-only (POSIX) and replaced atomically.
"""
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

ID_RE = re.compile(r"^[0-9a-f]{16}$")
TITLE_MAX = 60


def account_key(coordinator_url: str, name: str) -> str:
    """Stable, filesystem-safe key so each account (server + name) has its own history."""
    return hashlib.sha256(f"{coordinator_url}\n{name}".encode()).hexdigest()[:16]


def _check(value: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError("invalid id")
    return value


class ChatStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _dir(self, account: str, create: bool = False) -> Path:
        path = self.root / _check(account)
        if create:
            for directory in (self.root, path):
                directory.mkdir(parents=True, exist_ok=True)
                if os.name == "posix":
                    os.chmod(directory, 0o700)
        return path

    def _read(self, path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("id") == path.stem else None

    def list(self, account: str) -> list[dict]:
        directory = self._dir(account)
        chats = []
        for path in directory.glob("*.json") if directory.is_dir() else []:
            if ID_RE.fullmatch(path.stem) and (chat := self._read(path)):
                chats.append({k: chat.get(k) for k in ("id", "title", "model", "created", "updated")})
        return sorted(chats, key=lambda c: c.get("updated") or 0, reverse=True)

    def get(self, account: str, chat_id: str) -> dict | None:
        try:
            return self._read(self._dir(account) / f"{_check(chat_id)}.json")
        except ValueError:
            return None

    def save(self, account: str, chat_id: str | None, title: str, model: str, messages: list[dict]) -> dict:
        directory = self._dir(account, create=True)
        now = time.time()
        if chat_id is None:
            chat_id, created = secrets.token_hex(8), now
        else:
            existing = self.get(account, _check(chat_id))
            if existing is None:
                raise KeyError(chat_id)
            created = existing.get("created", now)
        chat = {"id": chat_id, "title": " ".join(title.split())[:TITLE_MAX], "model": model,
                "created": created, "updated": now, "messages": messages}
        path = directory / f"{chat_id}.json"
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(chat, handle, ensure_ascii=False)
        os.replace(tmp, path)
        return chat

    def delete(self, account: str, chat_id: str) -> bool:
        try:
            path = self._dir(account) / f"{_check(chat_id)}.json"
        except ValueError:
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
