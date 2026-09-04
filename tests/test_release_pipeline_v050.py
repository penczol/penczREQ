from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote

import yaml

from request_app import __version__


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"


class UniqueKeyLoader(yaml.BaseLoader):
    pass


def construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise AssertionError(f"Duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_unique_mapping
)


def load_tool(module_name: str, filename: str):
    sys.path.insert(0, str(TOOLS))
    try:
        spec = importlib.util.spec_from_file_location(module_name, TOOLS / filename)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(TOOLS))


installer_builder = load_tool("penczreq_installer_builder_release", "build_installer.py")
release_builder = load_tool("penczreq_release_builder", "build_release_artifacts.py")
publication_gate = load_tool("penczreq_publication_gate", "publication_gate.py")


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def workflow(name: str) -> tuple[str, dict]:
    text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
    # GitHub Actions uses YAML 1.2 semantics for `on`; BaseLoader avoids PyYAML
    # 1.1 interpreting that key as boolean while still validating the document.
    return text, yaml.load(text, Loader=UniqueKeyLoader)


def all_uses(document: dict) -> list[str]:
    return [
        step["uses"]
        for job in document["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    ]


def public_tree_paths() -> list[Path]:
    excluded_parts = {
        ".git",
        ".pytest_cache",
        ".venv",
        "__pycache__",
        "build",
        "dev-data",
        "dist",
        "node_modules",
    }
    return [
        path.relative_to(ROOT)
        for path in ROOT.rglob("*")
        if path.is_file() and not excluded_parts.intersection(path.relative_to(ROOT).parts)
    ]


def test_project_version_has_one_canonical_source():
    assert installer_builder.project_version() == __version__ == "0.5.2"


def test_release_artifacts_are_deterministic_and_self_verifying(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_files = release_builder.build(first)
    second_files = release_builder.build(second)

    assert [path.name for path in first_files] == [path.name for path in second_files]
    assert {path.name: file_digest(path) for path in first_files} == {
        path.name: file_digest(path) for path in second_files
    }

    manifest = json.loads((first / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == __version__
    assert manifest["image"] == {
        "immutable": "ghcr.io/<owner>/penczreq:0.5.2",
        "moving": "ghcr.io/<owner>/penczreq:stable",
    }
    assert manifest["update_policy"] == {
        "compose_or_schema_change_requires_migrator": True,
        "current_release_requires_migrator": True,
        "database_schema_changes": ["requests.title_en"],
        "main_push_updates_production": False,
        "numbered_image_is_rollback_anchor": True,
        "stable_requires_approved_release": True,
    }

    for line in (first / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        expected, filename = line.split("  ", maxsplit=1)
        assert file_digest(first / filename) == expected


def test_title_en_schema_change_is_explicitly_a_versioned_migrator_release():
    update = (ROOT / "docs" / "UPDATE.md").read_text(encoding="utf-8")
    install = (ROOT / "deploy" / "truenas" / "INSTALL.md").read_text(
        encoding="utf-8"
    )
    release_notes = (ROOT / "docs" / "RELEASE-NOTES-0.5.2.md").read_text(
        encoding="utf-8"
    )

    for document in (update, install, release_notes):
        assert "requests.title_en" in document
        assert "image-only" in document
    assert "not eligible for the simple image-only path" in update
    assert "nie jest prostą aktualizacją image-only" in install
    assert "requires the versioned migrator" in release_notes


def test_publication_gate_is_clean_with_selected_license():
    paths = public_tree_paths()

    assert publication_gate.scan(ROOT, paths) == []
    assert publication_gate.scan(ROOT, paths, require_license=True) == []


def test_repository_uses_unmodified_agpl_v3_text():
    license_path = ROOT / "LICENSE"

    assert file_digest(license_path) == (
        "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"  # pragma: allowlist secret
    )


def test_publication_gate_detects_private_material_without_echoing_it(tmp_path):
    forbidden = "Pencz" + "Flix.DDNS.NET"
    private_email = "person@" + "private.invalid"
    private_key = "-----BEGIN RSA " + "PRIVATE KEY-----"
    (tmp_path / "notes.txt").write_text(
        f"{forbidden}\n{private_email}\n{private_key}\n", encoding="utf-8"
    )
    (tmp_path / "secret.pem").write_text("fixture", encoding="utf-8")

    findings = publication_gate.scan(
        tmp_path, [Path("notes.txt"), Path("secret.pem")]
    )

    assert {item.rule for item in findings} == {
        "private-identifier",
        "private-email",
        "private-key",
        "sensitive-path",
    }
    assert forbidden not in json.dumps([item.__dict__ for item in findings])
    assert private_email not in json.dumps([item.__dict__ for item in findings])


def test_ci_is_verification_only_and_pins_external_actions():
    text, document = workflow("ci.yml")

    assert set(document["on"]) == {"push", "pull_request", "workflow_dispatch"}
    assert document["permissions"] == {"contents": "read"}
    assert "packages: write" not in text
    assert "docker push" not in text
    assert "ghcr.io" not in text
    assert "--execute" not in text
    assert "midclt" not in text
    assert all(re.search(r"@[0-9a-f]{40}(?:\s|$)", item) for item in all_uses(document))
    assert "python -m pytest -q" in text
    assert "publication_gate.py" in text
    assert "pip_audit" in text
    assert "detect_secrets" in text
    assert "detect_secrets scan --no-verify" in text
    assert "spdx-json" in text
    assert "trivy-image.json" in text


def test_release_requires_approval_license_and_promotes_stable_last():
    text, document = workflow("release.yml")

    assert document["on"] == {"release": {"types": ["published"]}}
    assert document["permissions"] == {"actions": "read", "contents": "read"}
    verify = document["jobs"]["verify"]
    publish = document["jobs"]["publish"]
    assert "environment" not in verify
    assert publish["needs"] == "verify"
    assert publish["environment"] == "release"
    assert publish["permissions"] == {
        "actions": "read",
        "contents": "write",
        "packages": "write",
    }
    checkout = verify["steps"][0]
    assert checkout["with"]["fetch-depth"] == "0"
    assert checkout["with"]["persist-credentials"] == "false"
    gate = verify["steps"][1]
    assert "git merge-base --is-ancestor" in gate["run"]
    assert "refs/remotes/origin/main" in gate["run"]
    assert "release.prerelease" in gate["env"]["GITHUB_EVENT_RELEASE_PRERELEASE"]
    assert "secrets.GITHUB_TOKEN" not in str(verify)
    assert "secrets.GITHUB_TOKEN" in str(publish)
    assert not any("checkout" in step.get("uses", "") for step in publish["steps"])
    assert "release-candidate.sha256" in text
    assert "sha256sum --check" in text
    assert "dist/*" not in text
    assert "test -f LICENSE" in text
    assert "--require-license" in text
    assert "detect_secrets scan --no-verify" in text
    assert not re.search(r"\b(PAT|PERSONAL_ACCESS_TOKEN)\b", text)
    assert all(re.search(r"@[0-9a-f]{40}(?:\s|$)", item) for item in all_uses(document))
    immutable_push = text.index('docker push "${image}"')
    stable_tag = text.index('docker tag "${image}" "${stable}"')
    stable_push = text.index('docker push "${stable}"')
    assert immutable_push < stable_tag < stable_push
    assert "--execute" not in text
    assert "midclt" not in text


def test_truenas_offline_image_is_an_explicit_docker_archive():
    text, _ = workflow("release.yml")
    installer = (ROOT / "deploy" / "truenas" / "INSTALL.md").read_text(
        encoding="utf-8"
    )

    archive = 'penczreq-${version}-docker-amd64.tar'
    assert f'image_filename="{archive}"' in text
    assert 'image_archive="dist/${image_filename}"' in text
    assert 'docker save --output "${image_archive}"' in text
    assert 'tar -tf "${image_archive}" manifest.json' in text
    assert 'docker load -i "${image_archive}"' in text
    assert f"dist/{archive}" in text
    assert f"dist/{archive}.sha256" in text
    assert "docker-amd64.tar" in installer
    assert "sudo docker load -i" in installer
    assert "Pure OCI archive" in installer
    assert "nie jest wejściem dla tego workflow" in installer


def test_documentation_defines_release_update_and_privacy_boundaries():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    polish = (ROOT / "docs" / "INSTRUKCJA-PL.md").read_text(encoding="utf-8")
    english = (ROOT / "docs" / "INSTRUKCJA-EN.md").read_text(encoding="utf-8")
    update = (ROOT / "docs" / "UPDATE.md").read_text(encoding="utf-8")
    release_notes = (ROOT / "docs" / "RELEASE-NOTES-0.5.2.md").read_text(
        encoding="utf-8"
    )
    installer = (ROOT / "deploy" / "truenas" / "INSTALL.md").read_text(
        encoding="utf-8"
    )
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert readme.startswith(
        "# penczREQ\n\npenczREQ is a self-hosted movie and TV request "
        "management companion for\nJellyfin and Plex"
    )
    assert "AGPL-3.0-only" in readme
    assert "No license has been selected yet" not in readme
    assert "An image-only update" in readme
    assert "private management plane" in readme
    assert "English user and administrator guide" in readme
    assert "Polska instrukcja użytkownika i administratora" in readme
    for screenshot in (
        "public-home.png",
        "public-search.png",
        "public-request-details.png",
        "public-admin-requests.png",
        "control-overview.png",
        "mobile-public.png",
    ):
        assert f'docs/screenshots/{screenshot}' in readme
    assert "[Polski](INSTRUKCJA-PL.md) | [English](INSTRUKCJA-EN.md)" in polish
    assert "[Polski](INSTRUKCJA-PL.md) | [English](INSTRUKCJA-EN.md)" in english
    assert "jeżeli użytkownik jest jedyną aktywnie zainteresowaną osobą" in polish
    assert "if the user is the only active interested person" in english
    assert "Przywróć do requestów" in polish
    assert "Restore to requests" in english
    assert "Ten produkt korzysta z API TMDB" in polish
    assert "This product uses the TMDB API" in english
    assert "Android Chrome został empirycznie zweryfikowany" in polish
    assert "Android Chrome was empirically verified" in english
    assert "Samsung Internet was also empirically verified" in english
    assert "Samsung Internet również został empirycznie zweryfikowany" in polish
    assert "penczREQ has still not" in english
    assert "been empirically verified on iOS/iPadOS." in english
    assert "nadal nie został empirycznie zweryfikowany dla penczREQ" in polish
    assert "iOS/iPadOS 16.4+" in english
    assert "TrueNAS SCALE 25.10.6" in readme
    assert "TrueNAS SCALE 25.10.6" in polish
    assert "TrueNAS SCALE 25.10.6" in english
    assert "mutations_performed: false" in update
    assert "numbered tag is the rollback anchor" in update
    assert "fresh side-by-side" in update
    assert "Known external checks" in release_notes
    assert "final-image build" in release_notes
    assert "Zweryfikowany kontrakt TrueNAS" in installer
    assert "Etap A — fresh install side-by-side" in installer
    assert "Etap B — prywatny upgrade 0.5.0/0.5.1 → 0.5.2" in installer
    assert "Etap C — rozważenie produkcyjnego upgrade" in installer
    assert "subskrypcje **wyłącznie w kopii UAT**" in installer
    assert "public.env` — `root:root`, tryb `0600`" in installer
    assert "app/.vapid-private.pem` — `apps:apps` (`568:568`), tryb `0600`" in installer
    assert "org.opencontainers.image.licenses" not in dockerfile

    public_docs = "\n".join((readme, polish, english, update, release_notes, installer))
    for internal_wording in (
        "Frozen Scope",
        "nie jest częścią tego checkpointu",
        "osobna zgoda użytkownika",
        "separate user decision",
    ):
        assert internal_wording not in public_docs


def test_polish_and_english_guides_have_parallel_section_scope():
    polish = (ROOT / "docs" / "INSTRUKCJA-PL.md").read_text(encoding="utf-8")
    english = (ROOT / "docs" / "INSTRUKCJA-EN.md").read_text(encoding="utf-8")
    polish_headings = re.findall(r"^## (.+)$", polish, flags=re.MULTILINE)
    english_headings = re.findall(r"^## (.+)$", english, flags=re.MULTILINE)

    assert list(zip(polish_headings, english_headings, strict=True)) == [
        ("Do czego służy aplikacja", "Purpose and architecture"),
        ("Uruchomienie Windows DEV/UAT", "Windows DEV/UAT"),
        ("Konta, role i sesje", "Accounts, roles and sessions"),
        ("Język polski i angielski", "English and Polish"),
        ("Requesty i udział użytkownika", "Requests and participation"),
        ("TMDB API Read Access Token", "TMDB API Read Access Token"),
        ("Tryby LAN i reverse proxy", "LAN and reverse-proxy modes"),
        ("PWA i Web Push", "PWA and Web Push"),
        ("Dane, kopie i odzyskiwanie", "Data, backups and recovery"),
        ("TrueNAS, aktualizacja i rollback", "TrueNAS, update and rollback"),
        ("Testy i diagnostyka", "Tests and diagnostics"),
        ("Stałe granice bezpieczeństwa", "Fixed security boundaries"),
    ]


def test_relative_markdown_links_in_public_documentation_resolve():
    documents = [
        ROOT / "README.md",
        *(ROOT / "docs").glob("*.md"),
        ROOT / "deploy" / "truenas" / "INSTALL.md",
    ]
    link_pattern = re.compile(r"\[[^]]+]\(([^)]+)\)")
    missing: list[str] = []

    for document in documents:
        for raw_target in link_pattern.findall(document.read_text(encoding="utf-8")):
            target = raw_target.strip().strip("<>")
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            relative = unquote(target.split("#", maxsplit=1)[0])
            if relative and not (document.parent / relative).resolve().exists():
                missing.append(f"{document.relative_to(ROOT)} -> {target}")

    assert missing == []
