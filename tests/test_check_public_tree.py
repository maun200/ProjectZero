"""Synthetic fixtures below are built from string parts at runtime so this file itself never
contains a literal secret/IP/path pattern that would trip a real scan of its own source."""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_public_tree as cpt  # noqa: E402

DOT = "."
AT = "@"


def join(*parts: str) -> str:
    return "".join(parts)


REAL_EMAIL = join("alice", AT, "example-real", DOT, "net")
GH_NOREPLY_EMAIL = join("123456+alice", AT, "users.noreply.github", DOT, "com")
ANTHROPIC_EMAIL = join("noreply", AT, "anthropic", DOT, "com")
EXAMPLE_DOT_COM_EMAIL = join("bob", AT, "example", DOT, "com")

IP_10 = join("10", DOT, "1", DOT, "2", DOT, "3")
IP_172 = join("172", DOT, "20", DOT, "0", DOT, "9")
IP_192 = join("192", DOT, "168", DOT, "77", DOT, "23")
IP_CGNAT = join("100", DOT, "101", DOT, "7", DOT, "9")
IP_PUBLIC = join("8", DOT, "8", DOT, "8", DOT, "8")
IP_LOOPBACK = join("127", DOT, "0", DOT, "0", DOT, "1")

TS_NET_NAME = join("example-host.tail0abc12", DOT, "ts", DOT, "net")
TAILNET_NAME = join("tail", "0abc12")
MNT_PATH = join("/mnt/", "backup")
HOME_PATH = join("/home/", "someone")
ROOT_PATH = join("/root/", "checkout")
CT_ID = join("CT ", "301")
PCT_EXEC = join("pct ", "exec")
PVE_WORD = "pve"
PVE_SUBSTRING_NOT_WORD = "pvesh"  # contains "pve" but not as a whole word

CAG_TOKEN = join("cag_", "x" * 40)
GH_TOKEN = join("ghp_", "a" * 36)
GH_PAT_TOKEN = join("github_pat_", "b" * 40)
AWS_KEY = join("AKIA", "B" * 16)
PEM_HEADER = join("-----BEGIN ", "RSA PRIVATE KEY-----")


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    return repo


def commit_all(repo: Path, message: str, name: str = "Test User", email: str = "test@example.com") -> None:
    git(repo, "add", "-A")
    git(repo, "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "-m", message)


# ---------------------------------------------------------------- pure pattern/helper checks


def test_email_allowlisted_domains_and_suffixes_pass_others_do_not():
    assert cpt.email_allowed(GH_NOREPLY_EMAIL)
    assert cpt.email_allowed(ANTHROPIC_EMAIL)
    assert cpt.email_allowed(EXAMPLE_DOT_COM_EMAIL)
    assert not cpt.email_allowed(REAL_EMAIL)


@pytest.mark.parametrize("ip", [IP_10, IP_172, IP_192, IP_CGNAT])
def test_private_ip_ranges_are_flagged(ip):
    categories = {f.category for f in cpt.scan_line("doc.md", 1, f"host at {ip} please", [])}
    assert categories & {"rfc1918-10", "rfc1918-172", "rfc1918-192.168", "tailscale-cgnat-100.64"}


@pytest.mark.parametrize("ip", [IP_PUBLIC, IP_LOOPBACK])
def test_public_and_loopback_ips_are_not_flagged(ip):
    findings = cpt.scan_line("doc.md", 1, f"host at {ip} please", [])
    assert findings == []


def test_homelab_identifier_patterns_flagged():
    text = " ".join([TS_NET_NAME, TAILNET_NAME, MNT_PATH, HOME_PATH, ROOT_PATH, CT_ID, PCT_EXEC, PVE_WORD])
    categories = {f.category for f in cpt.scan_line("doc.md", 1, text, [])}
    assert categories == {"tailscale-ts-net", "tailnet-name", "mnt-path", "home-path",
                          "root-path", "ct-id", "pct-exec", "pve-word"}


def test_pve_word_boundary_does_not_match_substring():
    findings = cpt.scan_line("doc.md", 1, PVE_SUBSTRING_NOT_WORD, [])
    assert not any(f.category == "pve-word" for f in findings)


@pytest.mark.parametrize("token,category", [
    (CAG_TOKEN, "token-cag"), (GH_TOKEN, "token-github"),
    (GH_PAT_TOKEN, "token-github"), (AWS_KEY, "token-aws"), (PEM_HEADER, "pem-header"),
])
def test_credential_shaped_tokens_flagged_and_masked(token, category):
    findings = cpt.scan_line("secret.txt", 1, f"value={token}", [])
    assert any(f.category == category for f in findings)
    match = next(f for f in findings if f.category == category)
    assert token not in match.text  # never printed in full
    assert match.text != token


def test_non_secret_categories_are_not_masked():
    findings = cpt.scan_line("doc.md", 1, ROOT_PATH, [])
    assert findings[0].text == "/root/"  # the pattern only matches the literal prefix


@pytest.mark.parametrize("name,flagged", [
    ("state.sqlite3", True), ("grid.db", True), ("model.gguf", True),
    (".env", True), (".env.local", True), ("id_ed25519", True),
    (".env.example", False), (".env.worker.example", False), ("id_ed25519.pub", False),
    ("README.md", False),
])
def test_forbidden_filenames(name, flagged):
    assert bool(cpt.check_filename(name)) == flagged


def test_allowlist_suppresses_only_matching_glob_and_regex(tmp_path):
    allowlist_file = tmp_path / ".public-allowlist"
    allowlist_file.write_text(f"README.md\t{IP_192.replace(chr(46), chr(92) + chr(46))}\n")
    allowlist = cpt.load_allowlist(allowlist_file)
    assert cpt.is_allowlisted("README.md", IP_192, allowlist)
    assert not cpt.is_allowlisted("other.md", IP_192, allowlist)       # wrong file
    assert not cpt.is_allowlisted("README.md", IP_10, allowlist)       # wrong text


def test_malformed_allowlist_line_is_skipped(tmp_path, capsys):
    allowlist_file = tmp_path / ".public-allowlist"
    allowlist_file.write_text("no-tab-on-this-line\n# a comment\n\n")
    assert cpt.load_allowlist(allowlist_file) == []
    assert "malformed" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------- tree/history scanning (real git repos)


def test_scan_tree_only_sees_tracked_files(tmp_path):
    repo = init_repo(tmp_path)
    (repo / "untracked.md").write_text(ROOT_PATH)
    findings = cpt.scan_tree(repo, [])
    assert findings == []  # never `git add`-ed


def test_scan_tree_finds_content_and_filename_issues_once_tracked(tmp_path):
    repo = init_repo(tmp_path)
    (repo / "notes.md").write_text(f"see {ROOT_PATH} and {REAL_EMAIL}\n")
    (repo / "dump.sqlite3").write_text("not really sqlite, just a forbidden name")
    commit_all(repo, "add files")
    findings = cpt.scan_tree(repo, [])
    categories_by_path = {f.location.split(":")[0]: f.category for f in findings}
    assert categories_by_path.get("dump.sqlite3") == "forbidden-filetype"
    assert any(f.category == "root-path" and f.location.startswith("notes.md") for f in findings)
    assert any(f.category == "email" and f.location.startswith("notes.md") for f in findings)


def test_scan_history_finds_secret_removed_from_current_tree(tmp_path):
    repo = init_repo(tmp_path)
    secret_file = repo / "config.txt"
    secret_file.write_text(f"token={CAG_TOKEN}\n")
    commit_all(repo, "add secret by mistake")
    secret_file.write_text("token=removed\n")
    commit_all(repo, "scrub secret")

    assert cpt.scan_tree(repo, []) == []  # gone from the current tree
    history_findings = cpt.scan_history(repo, [])
    assert any(f.category == "token-cag" for f in history_findings)


def test_scan_history_flags_author_and_committer_email(tmp_path):
    repo = init_repo(tmp_path)
    (repo / "a.txt").write_text("hello\n")
    commit_all(repo, "initial", name="Real Name", email=REAL_EMAIL)
    findings = cpt.scan_history(repo, [])
    identity_findings = [f for f in findings if f.location.endswith(("author", "committer"))]
    assert identity_findings and all(f.category == "email" for f in identity_findings)


def test_scan_history_allows_the_projects_own_attribution_email(tmp_path):
    repo = init_repo(tmp_path)
    (repo / "a.txt").write_text("hello\n")
    commit_all(repo, "initial", name="Claude", email=ANTHROPIC_EMAIL)
    findings = cpt.scan_history(repo, [])
    assert not any(f.location.endswith(("author", "committer")) for f in findings)


def test_cli_exit_code_reflects_findings(tmp_path):
    repo = init_repo(tmp_path)
    (repo / "clean.md").write_text("nothing sensitive here\n")
    commit_all(repo, "clean")
    script = str(Path(__file__).resolve().parent.parent / "scripts" / "check_public_tree.py")
    clean_run = subprocess.run([sys.executable, script, "--repo", str(repo)], capture_output=True, text=True)
    assert clean_run.returncode == 0

    (repo / "dirty.md").write_text(f"{REAL_EMAIL}\n")
    commit_all(repo, "dirty")
    dirty_run = subprocess.run([sys.executable, script, "--repo", str(repo)], capture_output=True, text=True)
    assert dirty_run.returncode == 1
    assert "email" in dirty_run.stdout
