#!/usr/bin/env python3
"""Build a deterministic, versioned penczREQ TrueNAS installer archive."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import re
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "deploy" / "truenas"
FILES = (
    "installer.py",
    "install.sh",
    "compose.yaml.example",
    "answers.example.json",
    "INSTALL.md",
)


def project_version() -> str:
    source = (ROOT / "request_app" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"$', source, re.MULTILINE)
    if not match:
        raise RuntimeError("Cannot determine the penczREQ version.")
    return match.group(1)


VERSION = project_version()


def build(output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"penczreq-installer-{VERSION}.tar.gz"
    checksum = archive.with_name(f"{archive.name}.sha256")
    prefix = f"penczreq-installer-{VERSION}"

    for name in FILES:
        if not (SOURCE_ROOT / name).is_file():
            raise FileNotFoundError(f"Missing installer source: {name}")

    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as package:
                for name in FILES:
                    source = SOURCE_ROOT / name
                    payload = source.read_bytes()
                    info = tarfile.TarInfo(f"{prefix}/{name}")
                    info.size = len(payload)
                    info.mode = 0o755 if name in {"installer.py", "install.sh"} else 0o644
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = "root"
                    info.gname = "root"
                    package.addfile(info, io.BytesIO(payload))

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="ascii", newline="\n")
    return archive, checksum


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    archive, checksum = build(args.output_dir)
    print(archive)
    print(checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
