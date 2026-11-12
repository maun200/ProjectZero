"""Local AI-chat history store (projectzero/chats.py)."""
import os
import time

import pytest

from projectzero.chats import ChatStore, account_key

A = account_key("http://coord.local:8765", "alice")
B = account_key("http://coord.local:8765", "bob")
MSGS = [{"role": "user", "content": "Hallo"}, {"role": "assistant", "content": "Hi!", "model": "demo-model"}]


def test_account_key_is_stable_and_distinct():
    assert A == account_key("http://coord.local:8765", "alice")
    assert A != B and A != account_key("http://other.local:8765", "alice")
    assert len(A) == 16 and all(c in "0123456789abcdef" for c in A)


def test_save_get_roundtrip_and_update(tmp_path):
    store = ChatStore(tmp_path)
    chat = store.save(A, None, "Hallo", "demo-model", MSGS[:1])
    assert len(chat["id"]) == 16 and chat["created"] == chat["updated"]
    again = store.save(A, chat["id"], "Hallo", "demo-model", MSGS)
    assert again["id"] == chat["id"] and again["created"] == chat["created"]
    loaded = store.get(A, chat["id"])
    assert loaded["messages"] == MSGS and loaded["model"] == "demo-model" and loaded["title"] == "Hallo"


def test_list_newest_first_without_messages(tmp_path):
    store = ChatStore(tmp_path)
    first = store.save(A, None, "eins", "demo-model", MSGS)
    time.sleep(0.01)
    second = store.save(A, None, "zwei", "demo-model", MSGS)
    listed = store.list(A)
    assert [c["id"] for c in listed] == [second["id"], first["id"]]
    assert "messages" not in listed[0] and listed[0]["title"] == "zwei"
    time.sleep(0.01)
    store.save(A, first["id"], "eins", "demo-model", MSGS + MSGS)  # continuing moves it to the top
    assert store.list(A)[0]["id"] == first["id"]


def test_accounts_are_isolated(tmp_path):
    store = ChatStore(tmp_path)
    chat = store.save(A, None, "geheim", "demo-model", MSGS)
    assert store.list(B) == [] and store.get(B, chat["id"]) is None
    assert store.delete(B, chat["id"]) is False and store.get(A, chat["id"]) is not None


def test_delete(tmp_path):
    store = ChatStore(tmp_path)
    chat = store.save(A, None, "weg", "demo-model", MSGS)
    assert store.delete(A, chat["id"]) is True
    assert store.get(A, chat["id"]) is None and store.list(A) == []
    assert store.delete(A, chat["id"]) is False


@pytest.mark.parametrize("bad", ["../../config", "x" * 16, "ABCDEF0123456789", "", "0123456789abcde", "/etc/passwd"])
def test_invalid_ids_are_rejected(tmp_path, bad):
    store = ChatStore(tmp_path)
    assert store.get(A, bad) is None and store.delete(A, bad) is False
    with pytest.raises(ValueError):
        store.save(A, bad, "t", "demo-model", MSGS)
    with pytest.raises(ValueError):
        store.list("../" + A)


def test_save_with_unknown_valid_id_is_rejected(tmp_path):
    with pytest.raises(KeyError):
        ChatStore(tmp_path).save(A, "0123456789abcdef", "t", "demo-model", MSGS)


def test_corrupt_file_is_skipped(tmp_path):
    store = ChatStore(tmp_path)
    good = store.save(A, None, "ok", "demo-model", MSGS)
    (tmp_path / A / "fedcba9876543210.json").write_text("{not json")
    assert [c["id"] for c in store.list(A)] == [good["id"]]
    assert store.get(A, "fedcba9876543210") is None


def test_title_is_trimmed(tmp_path):
    chat = ChatStore(tmp_path).save(A, None, "  " + "x" * 200 + "  ", "demo-model", MSGS)
    assert chat["title"] == "x" * 60


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_files_are_owner_only(tmp_path):
    store = ChatStore(tmp_path / "chats")
    chat = store.save(A, None, "t", "demo-model", MSGS)
    assert (tmp_path / "chats" / A / f"{chat['id']}.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "chats" / A).stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "chats").stat().st_mode & 0o777 == 0o700
