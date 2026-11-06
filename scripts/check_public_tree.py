"""Privacy/secret scanner for the public ProjectZero snapshot: exits 1 on any finding.

    python3 scripts/check_public_tree.py              # tracked files only (what CI runs)
    python3 scripts/check_public_tree.py --history     # also every commit's added/removed
                                                        # lines and every author/committer

Looks for things that must never reach a public repo: email addresses (besides GitHub's own
noreply addresses and this project's own attribution line), private IPv4 ranges, this
operator's home-network identifiers (Tailscale names, local mount/home paths, container ids),
credential-shaped tokens, and forbidden file types (databases, model weights, real .env files,
private SSH keys). It reports *locations*, not raw secret values — token-shaped matches are
masked even with --show-matches, since this output may itself end up in a chat or CI log.

An allowlist at .public-allowlist (next to this script's repo root, override with --allowlist)
holds known-fine documentation placeholders. Format: one `<glob>\\t<regex>` pair per line
(blank lines and lines starting with '#' are ignored); a finding is suppressed only when its
file path matches the glob AND its matched text matches the regex. Keep it narrow — it is an
exception list for specific placeholders (e.g. `README.md\\t192\\.168\\.1\\.(5|10)`), not a way
to blanket-disable a whole category.

This script does not gate the current (private) tree: this repo's own history and paths are
full of exactly the identifiers it looks for, by design — that is what the public snapshot
process is for. Run it here to produce the findings list that process uses as input.
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org")
ALLOWED_EMAILS = {"noreply@anthropic.com"}
ALLOWED_EMAIL_SUFFIXES = ("@users.noreply.github.com",)

FORBIDDEN_FILENAMES = (
    # (predicate over the basename, label)
    (lambda n: n.endswith((".sqlite3", ".db", ".gguf")), "forbidden-filetype"),
    (lambda n: n == ".env" or (n.startswith(".env.") and not n.endswith(".example")), "real-dotenv-file"),
    (lambda n: n == "id_ed25519", "private-ssh-key-file"),
)

# Categories whose matched text is masked in output even with --show-matches: these are
# credential-shaped, not merely private, and this report may land in a chat or CI log.
SECRET_LIKE = {"token-cag", "token-github", "token-aws", "pem-header"}


def _email_pattern() -> re.Pattern:
    return re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def email_allowed(match: str) -> bool:
    if match in ALLOWED_EMAILS or match.lower().endswith(ALLOWED_EMAIL_SUFFIXES):
        return True
    domain = match.rsplit("@", 1)[-1].lower()
    return any(domain == d or domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS)


PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email", _email_pattern()),
    ("rfc1918-10", re.compile(r"\b10\.(?:[0-9]{1,3}\.){2}[0-9]{1,3}\b")),
    ("rfc1918-172", re.compile(r"\b172\.(?:1[6-9]|2[0-9]|3[0-1])\.[0-9]{1,3}\.[0-9]{1,3}\b")),
    ("rfc1918-192.168", re.compile(r"\b192\.168\.[0-9]{1,3}\.[0-9]{1,3}\b")),
    ("tailscale-cgnat-100.64", re.compile(r"\b100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b")),
    ("tailscale-ts-net", re.compile(r"\b[A-Za-z0-9-]+\.ts\.net\b")),
    ("tailnet-name", re.compile(r"\btail[0-9a-f]{6}\b")),
    ("mnt-path", re.compile(r"/mnt/")),
    ("home-path", re.compile(r"/home/[A-Za-z0-9_.-]+")),
    ("root-path", re.compile(r"/root/")),
    ("ct-id", re.compile(r"\bCT ?3\d+\b", re.IGNORECASE)),
    ("pct-exec", re.compile(r"\bpct exec\b", re.IGNORECASE)),
    ("pve-word", re.compile(r"\bpve\b", re.IGNORECASE)),
    ("token-cag", re.compile(r"\bcag_[A-Za-z0-9_-]{30,}\b")),
    ("token-github", re.compile(r"\b(?:ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("token-aws", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("pem-header", re.compile(r"-----BEGIN ")),
]


@dataclass
class Finding:
    location: str       # "path:line" or "commit:path:line" or "commit:identity"
    category: str
    text: str            # matched substring, already masked for SECRET_LIKE categories

    def render(self) -> str:
        return f"{self.location}  [{self.category}]  {self.text}"


def mask(category: str, text: str) -> str:
    if category not in SECRET_LIKE:
        return text
    if len(text) <= 8:
        return "*" * len(text)
    return text[:4] + "…" + text[-4:] + f" ({len(text)} chars, masked)"


def load_allowlist(path: Path) -> list[tuple[str, re.Pattern]]:
    if not path.is_file():
        return []
    rules = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" not in line:
            print(f"Skipping malformed allowlist line (no tab): {raw!r}", file=sys.stderr)
            continue
        glob, regex = line.split("\t", 1)
        rules.append((glob.strip(), re.compile(regex.strip())))
    return rules


def is_allowlisted(path: str, matched_text: str, allowlist: list[tuple[str, re.Pattern]]) -> bool:
    return any(fnmatch.fnmatch(path, glob) and regex.search(matched_text) for glob, regex in allowlist)


def scan_line(path: str, line_no: int, text: str, allowlist: list[tuple[str, re.Pattern]]) -> list[Finding]:
    findings = []
    for category, pattern in PATTERNS:
        for m in pattern.finditer(text):
            matched = m.group(0)
            if category == "email" and email_allowed(matched):
                continue
            if is_allowlisted(path, matched, allowlist):
                continue
            findings.append(Finding(f"{path}:{line_no}", category, mask(category, matched)))
    return findings


def check_filename(path: str) -> list[Finding]:
    name = Path(path).name
    for predicate, label in FORBIDDEN_FILENAMES:
        if predicate(name):
            return [Finding(path, label, name)]
    return []


def tracked_files(repo_root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=repo_root, capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line]


def scan_tree(repo_root: Path, allowlist: list[tuple[str, re.Pattern]]) -> list[Finding]:
    findings = []
    for path in tracked_files(repo_root):
        findings.extend(check_filename(path))
        full = repo_root / path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            findings.extend(scan_line(path, line_no, line, allowlist))
    return findings


_COMMIT_RE = re.compile(r"^commit ([0-9a-f]{40})")
_AUTHOR_RE = re.compile(r"^Author:\s*(.*)$")
_COMMITTER_RE = re.compile(r"^Commit:\s*(.*)$")
_DIFF_FILE_RE = re.compile(r"^diff --git a/(.*) b/(.*)$")


def scan_history(repo_root: Path, allowlist: list[tuple[str, re.Pattern]]) -> list[Finding]:
    proc = subprocess.run(
        ["git", "log", "--all", "-p", "--no-color", "--format=commit %H%nAuthor: %an <%ae>%nCommit: %cn <%ce>"],
        cwd=repo_root, capture_output=True, text=True, check=True,
    )
    findings: list[Finding] = []
    commit = "unknown"
    path = "unknown"
    for raw in proc.stdout.splitlines():
        if m := _COMMIT_RE.match(raw):
            commit = m.group(1)[:12]
            continue
        if m := _AUTHOR_RE.match(raw):
            for category, pattern in PATTERNS:
                for match in pattern.finditer(m.group(1)):
                    text = match.group(0)
                    if category == "email" and email_allowed(text):
                        continue
                    findings.append(Finding(f"{commit}:author", category, mask(category, text)))
            continue
        if m := _COMMITTER_RE.match(raw):
            for category, pattern in PATTERNS:
                for match in pattern.finditer(m.group(1)):
                    text = match.group(0)
                    if category == "email" and email_allowed(text):
                        continue
                    findings.append(Finding(f"{commit}:committer", category, mask(category, text)))
            continue
        if m := _DIFF_FILE_RE.match(raw):
            path = m.group(2)
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+") or raw.startswith("-"):
            findings.extend(scan_line(f"{commit}:{path}", 0, raw[1:], allowlist))
    return findings


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", type=Path, default=Path("."), help="Repository root (default: cwd)")
    p.add_argument("--allowlist", type=Path, default=None, help="Default: <repo>/.public-allowlist")
    p.add_argument("--history", action="store_true", help="Also scan every commit's diff and author/committer")
    args = p.parse_args()

    repo_root = args.repo.resolve()
    allowlist = load_allowlist(args.allowlist or repo_root / ".public-allowlist")

    findings = scan_tree(repo_root, allowlist)
    if args.history:
        findings.extend(scan_history(repo_root, allowlist))

    for finding in findings:
        print(finding.render())
    print(f"\n{len(findings)} finding(s)" + (" (tree + history)" if args.history else " (tree only)"),
          file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
