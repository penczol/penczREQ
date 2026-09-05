#!/usr/bin/env python3
"""Build a deterministic, versioned penczREQ TrueNAS installer archive."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import re
import tarfile
from dataclasses import dataclass
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


@dataclass(frozen=True)
class ReleaseIdentity:
    """Explicit public locators; no environment discovery or network lookup."""

    github_repository: str

    def __post_init__(self) -> None:
        slug = self.github_repository
        if not isinstance(slug, str) or not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}",
            slug,
        ):
            raise ValueError("repository must be a concrete GitHub owner/repo slug")
        owner, repository = slug.split("/")
        if owner.lower() in {"owner", "placeholder"} or repository in {".", ".."}:
            raise ValueError("repository must not contain a placeholder or dot path")

    @property
    def github_url(self) -> str:
        return f"https://github.com/{self.github_repository}"

    @property
    def github_release_base(self) -> str:
        return f"{self.github_url}/releases/download/v{VERSION}"

    @property
    def versioned_image(self) -> str:
        owner = self.github_repository.split("/")[0].lower()
        return f"ghcr.io/{owner}/penczreq:{VERSION}"

    @property
    def stable_image(self) -> str:
        owner = self.github_repository.split("/")[0].lower()
        return f"ghcr.io/{owner}/penczreq:stable"


def render_public_locators(name: str, payload: bytes, identity: ReleaseIdentity) -> bytes:
    """Render only known full locators, never the installer's <owner> guard."""
    text = payload.decode("utf-8")
    if name in {"installer.py", "answers.example.json", "INSTALL.md"}:
        text = text.replace(f"ghcr.io/<owner>/penczreq:{VERSION}", identity.versioned_image)
        text = text.replace("ghcr.io/<owner>/penczreq:stable", identity.stable_image)
    if name == "INSTALL.md":
        # Resolve the documented shell URL to the exact version as well as owner/repo.
        text = text.replace(
            "https://github.com/<owner>/penczREQ/releases/download/v${version}",
            identity.github_release_base,
        )
        text = text.replace("https://github.com/<owner>/penczREQ", identity.github_url)
    if "ghcr.io/<owner>/" in text or "github.com/<owner>/" in text:
        raise ValueError(f"Unresolved public locator in installer source: {name}")
    return text.encode("utf-8")


def build(output_dir: Path, *, repository: str | None = None) -> tuple[Path, Path]:
    # Standalone packaging may remain generic; public release builds pass identity.
    identity = ReleaseIdentity(repository) if repository is not None else None
    payloads = {}
    for name in FILES:
        source = SOURCE_ROOT / name
        if not source.is_file():
            raise FileNotFoundError(f"Missing installer source: {name}")
        payload = source.read_bytes()
        payloads[name] = render_public_locators(name, payload, identity) if identity else payload

    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"penczreq-installer-{VERSION}.tar.gz"
    checksum = archive.with_name(f"{archive.name}.sha256")
    prefix = f"penczreq-installer-{VERSION}"

    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as package:
                for name in FILES:
                    payload = payloads[name]
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
    parser.add_argument("--repository", help="Render public locators for this GitHub owner/repo")
    args = parser.parse_args()
    try:
        archive, checksum = build(args.output_dir, repository=args.repository)
    except ValueError as error:
        parser.error(str(error))
    print(archive)
    print(checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
