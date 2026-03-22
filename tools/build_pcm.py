#!/usr/bin/env python3
"""Build PCM ZIP + repository metadata for local testing and tagged releases.

Local dev defaults:
    python tools/build_pcm.py
    python tools/build_pcm.py --install

Release usage:
    python tools/build_pcm.py --tag v1.2.10 --github-repo owner/repo --output-dir .
"""

import argparse
import copy
import hashlib
import json
import os
import shutil
import time
import zipfile
from typing import Dict, Iterable, Optional, Tuple

SCHEMA_URL = "https://go.kicad.org/pcm/schemas/v1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT, "src", "kicad_jlcimport")
METADATA_PATH = os.path.join(ROOT, "metadata.json")
DIST_DIR = os.path.join(ROOT, "dist")
RESOURCES_DIR = os.path.join(ROOT, "resources")


def _normalize_tag(tag: str) -> str:
    if tag.startswith("refs/tags/"):
        return tag[len("refs/tags/") :]
    return tag


def _version_from_tag(tag: str) -> str:
    normalized = _normalize_tag(tag)
    return normalized[1:] if normalized.startswith("v") else normalized


def _json_bytes(doc: Dict) -> bytes:
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_metadata(path: str) -> Dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: str, doc: Dict) -> None:
    with open(path, "wb") as handle:
        handle.write(_json_bytes(doc))


def _iter_source_files(src_dir: str) -> Iterable[Tuple[str, str]]:
    for dirpath, dirnames, filenames in os.walk(src_dir):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for filename in filenames:
            if filename.endswith((".pyc", ".pyo")):
                continue
            full_path = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(full_path, src_dir)
            yield full_path, os.path.join("plugins", rel_path)


def _kicad_3rdparty_plugins() -> str:
    """Return the KiCad 3rdparty plugins directory for the current platform.

    Reuses the canonical path discovery from the main package (available when
    the venv is active).  Only called by ``--install`` / ``--print-install-dir``
    which are local-dev operations.
    """
    from kicad_jlcimport.kicad.library import _detect_kicad_version, _kicad_data_base

    base = _kicad_data_base()
    ver = _detect_kicad_version()
    return os.path.join(base, ver, "3rdparty", "plugins", "com_github_jvanderberg_kicad-jlcimport")


def _release_metadata(template: Dict, version: str) -> Dict:
    metadata_doc = copy.deepcopy(template)
    versions = metadata_doc.get("versions")
    if not isinstance(versions, list) or len(versions) == 0:
        raise ValueError("metadata.json must include at least one entry in versions[]")
    metadata_doc["versions"][0]["version"] = version
    return metadata_doc


def _build_pcm_zip(zip_path: str, metadata_path: str, metadata_doc: Dict = None) -> int:
    os.makedirs(os.path.dirname(zip_path), exist_ok=True)
    install_size = 0

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if metadata_doc is None:
            zf.write(metadata_path, "metadata.json")
            install_size += os.path.getsize(metadata_path)
        else:
            metadata_bytes = _json_bytes(metadata_doc)
            zf.writestr("metadata.json", metadata_bytes)
            install_size += len(metadata_bytes)

        icon_path = os.path.join(RESOURCES_DIR, "icon.png")
        if os.path.isfile(icon_path):
            zf.write(icon_path, "resources/icon.png")
            install_size += os.path.getsize(icon_path)

        toolbar_icon_path = os.path.join(RESOURCES_DIR, "icon_toolbar.png")
        if os.path.isfile(toolbar_icon_path):
            zf.write(toolbar_icon_path, "plugins/icon.png")
            install_size += os.path.getsize(toolbar_icon_path)

        for full_path, archive_path in _iter_source_files(SRC_DIR):
            zf.write(full_path, archive_path)
            install_size += os.path.getsize(full_path)

    return install_size


def _build_packages_json(
    release_metadata: Dict,
    version: str,
    download_url: str,
    zip_size: int,
    install_size: int,
    zip_sha256: str,
) -> Dict:
    package_doc = copy.deepcopy(release_metadata)
    package_doc.pop("$schema", None)
    versions = package_doc.get("versions")
    first_version = copy.deepcopy(versions[0]) if isinstance(versions, list) and versions else {}
    first_version["version"] = version
    first_version["download_url"] = download_url
    first_version["download_sha256"] = zip_sha256
    first_version["download_size"] = zip_size
    first_version["install_size"] = install_size
    package_doc["versions"] = [first_version]
    return {"$schema": SCHEMA_URL, "packages": [package_doc]}


def _build_resources_zip(resources_zip_path: str, package_identifier: str) -> bool:
    os.makedirs(os.path.dirname(resources_zip_path), exist_ok=True)
    icon_path = os.path.join(RESOURCES_DIR, "icon.png")
    if not os.path.isfile(icon_path):
        return False

    with zipfile.ZipFile(resources_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(icon_path, os.path.join(package_identifier, "icon.png"))

    return True


def _build_repository_json(
    release_metadata: Dict,
    packages_url: str,
    packages_sha256: str,
    update_timestamp: int,
    resources_ref: Optional[Dict] = None,
) -> Dict:
    author = release_metadata.get("author")
    if not isinstance(author, dict):
        raise ValueError("metadata.json must include an author object")
    maintainer = {"name": author.get("name", "Unknown")}
    contact = author.get("contact")
    if isinstance(contact, dict) and contact:
        maintainer["contact"] = contact
    repository_name = f"{release_metadata.get('name', 'KiCad Add-ons')} Repository"
    repository_doc = {
        "$schema": SCHEMA_URL,
        "name": repository_name,
        "maintainer": maintainer,
        "packages": {
            "url": packages_url,
            "sha256": packages_sha256,
            "update_timestamp": update_timestamp,
        },
    }
    if resources_ref:
        repository_doc["resources"] = resources_ref
    return repository_doc


def _install(zip_path: str) -> str:
    """Extract the PCM ZIP into KiCad's 3rdparty plugins directory."""
    dest = _kicad_3rdparty_plugins()
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest)
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build PCM ZIP and repository metadata")
    parser.add_argument("--tag", default=None, help="Release tag (example: v1.2.10)")
    parser.add_argument("--version", default=None, help="Version override (default: metadata.json version)")
    parser.add_argument("--github-repo", default=None, help="GitHub repo in owner/name format")
    parser.add_argument("--download-url", default=None, help="Override download URL in packages.json")
    parser.add_argument("--packages-url", default=None, help="Override packages URL in repository.json")
    parser.add_argument("--resources-url", default=None, help="Override resources URL in repository.json")
    parser.add_argument("--zip-name", default=None, help="Override generated ZIP filename")
    parser.add_argument("--output-dir", default=DIST_DIR, help="Directory for generated assets")
    parser.add_argument("--metadata", default=METADATA_PATH, help="Path to metadata.json template")
    parser.add_argument("--install", action="store_true", help="Install generated ZIP into KiCad's 3rdparty dir")
    parser.add_argument(
        "--print-install-dir", action="store_true", help="Print KiCad plugin install directory and exit"
    )
    args = parser.parse_args()

    if args.print_install_dir:
        print(_kicad_3rdparty_plugins())
        return

    tag = _normalize_tag(args.tag) if args.tag else None
    template_metadata = _read_metadata(args.metadata)
    default_version = template_metadata["versions"][0]["version"]
    version = args.version or (_version_from_tag(tag) if tag else default_version)

    if args.zip_name:
        zip_name = args.zip_name
    elif tag:
        zip_name = f"JLCImport-{tag}.zip"
    else:
        zip_name = "JLCImport-dev.zip"
    zip_path = os.path.join(args.output_dir, zip_name)

    metadata_for_zip = None
    if version != default_version:
        metadata_for_zip = _release_metadata(template_metadata, version)
    release_metadata = metadata_for_zip or template_metadata
    install_size = _build_pcm_zip(zip_path, args.metadata, metadata_for_zip)
    zip_size = os.path.getsize(zip_path)
    zip_sha256 = _sha256_file(zip_path)

    if args.download_url:
        download_url = args.download_url
    elif tag and args.github_repo:
        download_url = f"https://github.com/{args.github_repo}/releases/download/{tag}/{zip_name}"
    else:
        download_url = f"http://localhost:8000/{zip_name}"

    packages_doc = _build_packages_json(
        release_metadata=release_metadata,
        version=version,
        download_url=download_url,
        zip_size=zip_size,
        install_size=install_size,
        zip_sha256=zip_sha256,
    )
    packages_path = os.path.join(args.output_dir, "packages.json")
    _write_json(packages_path, packages_doc)

    packages_sha256 = _sha256_file(packages_path)
    if args.packages_url:
        packages_url = args.packages_url
    elif tag and args.github_repo:
        packages_url = f"https://github.com/{args.github_repo}/releases/latest/download/packages.json"
    else:
        packages_url = "http://localhost:8000/packages.json"

    update_timestamp = int(time.time())
    resources_zip_path = os.path.join(args.output_dir, "resources.zip")
    resources_ref = None
    if _build_resources_zip(resources_zip_path, release_metadata["identifier"]):
        if args.resources_url:
            resources_url = args.resources_url
        elif args.github_repo:
            resources_url = f"https://github.com/{args.github_repo}/releases/latest/download/resources.zip"
        elif args.packages_url and "/" in args.packages_url:
            resources_url = f"{args.packages_url.rsplit('/', 1)[0]}/resources.zip"
        else:
            resources_url = "http://localhost:8000/resources.zip"
        resources_ref = {
            "url": resources_url,
            "sha256": _sha256_file(resources_zip_path),
            "update_timestamp": update_timestamp,
        }

    repository_doc = _build_repository_json(
        release_metadata=release_metadata,
        packages_url=packages_url,
        packages_sha256=packages_sha256,
        update_timestamp=update_timestamp,
        resources_ref=resources_ref,
    )
    repository_path = os.path.join(args.output_dir, "repository.json")
    _write_json(repository_path, repository_doc)

    print("Prepared PCM assets:")
    print(f"  {zip_path}")
    print(f"  {packages_path}")
    print(f"  {repository_path}")
    if resources_ref:
        print(f"  {resources_zip_path}")
    print(f"Version: {version}")
    print(f"Download URL in packages.json: {download_url}")
    print(f"Packages URL in repository.json: {packages_url}")

    if args.install:
        dest = _install(zip_path)
        print(f"Installed to: {dest}")
        print("Restart KiCad to load the updated plugin.")


if __name__ == "__main__":
    main()
