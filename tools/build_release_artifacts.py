#!/usr/bin/env python3
"""Build deterministic local release assets without publishing them."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from build_installer import ROOT, VERSION, build as build_installer


RELEASE_NOTES = ROOT / "docs" / f"RELEASE-NOTES-{VERSION}.md"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(output_dir: Path) -> tuple[Path, ...]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if not RELEASE_NOTES.is_file():
        raise FileNotFoundError(f"Missing release notes: {RELEASE_NOTES}")

    installer, installer_checksum = build_installer(output_dir)
    notes = output_dir / f"penczREQ-{VERSION}-release-notes.md"
    notes.write_bytes(RELEASE_NOTES.read_bytes())

    payloads = (installer, installer_checksum, notes)
    manifest = output_dir / "release-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product": "penczREQ",
                "version": VERSION,
                "image": {
                    "immutable": f"ghcr.io/<owner>/penczreq:{VERSION}",
                    "moving": "ghcr.io/<owner>/penczreq:stable",
                },
                "update_policy": {
                    "stable_requires_approved_release": True,
                    "main_push_updates_production": False,
                    "numbered_image_is_rollback_anchor": True,
                    "compose_or_schema_change_requires_migrator": True,
                    "current_release_requires_migrator": True,
                    "database_schema_changes": ["requests.title_en"],
                },
                "artifacts": [
                    {
                        "name": path.name,
                        "bytes": path.stat().st_size,
                        "sha256": sha256(path),
                    }
                    for path in payloads
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    checksums = output_dir / "SHA256SUMS"
    checksum_targets = (*payloads, manifest)
    checksums.write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in sorted(checksum_targets)),
        encoding="ascii",
        newline="\n",
    )
    return (*checksum_targets, checksums)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    for artifact in build(args.output_dir):
        print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
