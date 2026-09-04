#!/usr/bin/env python3
"""Scan the tracked public tree for private identifiers and sensitive files."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Split literals keep the scanner's own source neutral while preserving an
# explicit, testable list of historical identifiers required by Frozen Scope.
FORBIDDEN_IDENTIFIERS = (
    "penc" + "zol",
    "pencz" + "flix",
    "pencz" + "flix.ddns.net",
    "pencz" + "req.ddns.net",
    "pencz" + "req-control.home.arpa",
)
ALLOWED_EMAIL_DOMAINS = {"example.com", "example.invalid", "users.noreply.github.com"}
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,}|example\.invalid)\b")
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    line: int | None = None


def tracked_files(root: Path, git_dir: Path | None = None) -> list[Path]:
    command = ["git"]
    if git_dir is None:
        command.extend(["-C", str(root)])
    else:
        command.extend([f"--git-dir={git_dir}", f"--work-tree={root}"])
    command.extend(["ls-files", "-z"])
    result = subprocess.run(command, check=True, capture_output=True)
    return [Path(item.decode("utf-8")) for item in result.stdout.split(b"\0") if item]


def _sensitive_path(path: Path) -> bool:
    normalized = path.as_posix().lower()
    name = path.name.lower()
    if name in {".env.example", ".env.container.example"}:
        return False
    if name == ".env" or name.startswith(".env."):
        return True
    if path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".log", ".jsonl"}:
        return True
    if any(part in {"backups", "dev-data", "migration-test", "private-docs", "posters"} for part in normalized.split("/")):
        return True
    return "credentials" in name and not name.endswith(".example")


def scan(root: Path, paths: list[Path], *, require_license: bool = False) -> list[Finding]:
    findings: list[Finding] = []
    normalized_paths = {path.as_posix() for path in paths}
    if require_license and not any(path.upper().startswith("LICENSE") for path in normalized_paths):
        findings.append(Finding("license-decision-required", "LICENSE"))

    for relative in paths:
        if _sensitive_path(relative):
            findings.append(Finding("sensitive-path", relative.as_posix()))
        absolute = root / relative
        try:
            payload = absolute.read_bytes()
        except OSError:
            findings.append(Finding("unreadable-tracked-file", relative.as_posix()))
            continue
        if b"\0" in payload:
            continue
        text = payload.decode("utf-8", errors="replace")
        folded_text = text.casefold()
        for identifier in FORBIDDEN_IDENTIFIERS:
            start = 0
            while True:
                index = folded_text.find(identifier.casefold(), start)
                if index < 0:
                    break
                findings.append(
                    Finding(
                        "private-identifier",
                        relative.as_posix(),
                        text.count("\n", 0, index) + 1,
                    )
                )
                start = index + len(identifier)
        for match in EMAIL_RE.finditer(text):
            if match.group(1).lower() not in ALLOWED_EMAIL_DOMAINS:
                findings.append(
                    Finding(
                        "private-email",
                        relative.as_posix(),
                        text.count("\n", 0, match.start()) + 1,
                    )
                )
        if PRIVATE_KEY_RE.search(text):
            findings.append(Finding("private-key", relative.as_posix()))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--git-dir", type=Path)
    parser.add_argument("--require-license", action="store_true")
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    paths = tracked_files(root, args.git_dir.resolve() if args.git_dir else None)
    findings = scan(root, paths, require_license=args.require_license)
    report = {
        "status": "blocked" if findings else "ok",
        "files_scanned": len(paths),
        "findings": [asdict(item) for item in findings],
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
