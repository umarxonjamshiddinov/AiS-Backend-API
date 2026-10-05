#!/usr/bin/env python3
"""Install a prebuilt portal release and atomically select its manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ALLOWED_ROOT_FILES = {"index.html", "favicon.svg", "robots.txt", "sitemap.xml"}
ASSET_REFERENCE = re.compile(r"(?:src|href)\s*=\s*[\"'](/assets/[^\"']+)[\"']")


def absolute_path(value: Path) -> Path:
    """Normalize a path lexically without following symbolic links."""
    return Path(os.path.abspath(os.fspath(value)))


def checked_path(value: Path, root: Path) -> Path:
    target = absolute_path(value)
    parent = absolute_path(root)
    if target == parent or not target.is_relative_to(parent):
        raise ValueError(f"Path is outside the intended directory: {target}")
    return target


def reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"Symbolic links are not permitted here: {path}")
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None and is_junction():
        raise ValueError(f"Junctions are not permitted here: {path}")


def reject_link_ancestors(path: Path) -> None:
    for ancestor in [path, *path.parents]:
        reject_symlink(ancestor)


def make_directory(path: Path, root: Path) -> Path:
    target = checked_path(path, root)
    reject_symlink(target)
    if target.exists() and not target.is_dir():
        raise ValueError(f"Expected a directory: {target}")
    target.mkdir(exist_ok=True)
    return target


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checked_tree(root: Path) -> tuple[list[Path], list[Path]]:
    directories: list[Path] = []
    files: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for item in sorted(directory.iterdir()):
            checked_path(item, root)
            reject_symlink(item)
            if item.is_dir():
                directories.append(item)
                pending.append(item)
            elif item.is_file():
                files.append(item)
            else:
                raise ValueError(f"Unexpected non-file build item: {item}")
    return directories, files


def validate_dist(dist: Path) -> tuple[list[Path], list[Path]]:
    reject_link_ancestors(dist)
    if not dist.is_dir():
        raise ValueError(f"Build directory does not exist: {dist}")
    for item in dist.iterdir():
        reject_symlink(item)
        if item.is_dir():
            if item.name != "assets":
                raise ValueError(f"Unexpected directory in dist: {item.name}")
        elif item.name not in ALLOWED_ROOT_FILES or not item.is_file():
            raise ValueError(f"Unexpected root file in dist: {item.name}")

    index = dist / "index.html"
    assets = dist / "assets"
    if not index.is_file() or index.stat().st_size == 0:
        raise ValueError("The build must contain a nonempty index.html.")
    if not assets.is_dir():
        raise ValueError("The build must contain an assets directory.")

    directories, files = checked_tree(dist)
    if not any(path.is_relative_to(assets) and path.suffix == ".js" for path in files):
        raise ValueError("The build must contain JavaScript assets.")
    references = ASSET_REFERENCE.findall(index.read_text(encoding="utf-8"))
    if not references:
        raise ValueError("index.html does not reference expected /assets/ files.")
    for reference in references:
        if any(character in reference for character in ("?", "#", "%", "\\")):
            raise ValueError(f"Unexpected asset reference: {reference}")
        asset = checked_path(dist / reference.lstrip("/"), assets)
        if not asset.is_file():
            raise ValueError(f"Referenced asset is missing: {reference}")
    return directories, files


def install(backend: Path, dist: Path) -> tuple[Path, Path | None]:
    if not backend.is_absolute():
        raise ValueError("--backend-root must be an absolute path.")
    backend = absolute_path(backend)
    dist = absolute_path(dist)
    reject_link_ancestors(backend)
    if not backend.is_dir() or not (backend / "app" / "main.py").is_file():
        raise ValueError(f"Backend root must contain app/main.py: {backend}")
    directories, source_files = validate_dist(dist)
    source_hashes = {path.relative_to(dist): sha256(path) for path in source_files}

    runtime = make_directory(backend / ".runtime", backend)
    portal = make_directory(runtime / "portal", runtime)
    releases = make_directory(portal / "releases", portal)
    history = make_directory(portal / "history", portal)
    name = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S%f") + "-" + uuid.uuid4().hex
    release = make_directory(releases / name, releases)
    active = checked_path(portal / "active.json", portal)
    reject_symlink(active)
    if active.exists() and not active.is_file():
        raise ValueError("active.json must be a file; the previous selection is unchanged.")

    for source in sorted(directories, key=lambda value: len(value.parts)):
        make_directory(release / source.relative_to(dist), release)
    for source in source_files:
        reject_symlink(source)
        relative = source.relative_to(dist)
        destination = checked_path(release / relative, release)
        shutil.copy2(source, destination)
        if sha256(destination) != source_hashes[relative]:
            raise ValueError(f"Release verification failed: {relative}; active.json is unchanged.")
    _, installed_files = checked_tree(release)
    if {path.relative_to(release) for path in installed_files} != set(source_hashes):
        raise ValueError("Release files differ from the source build; active.json is unchanged.")

    backup = None
    if active.exists():
        backup = checked_path(history / f"{name}.json", history)
        shutil.copy2(active, backup)
        if sha256(active) != sha256(backup):
            raise ValueError(
                "Previous manifest backup verification failed; active.json is unchanged."
            )

    temporary = checked_path(portal / f"active-{uuid.uuid4().hex}.tmp", portal)
    manifest = {"directory": str(release)}
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if json.loads(temporary.read_text(encoding="utf-8")) != manifest:
        raise ValueError("New manifest validation failed; active.json is unchanged.")
    os.replace(temporary, active)
    return release, backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend-root",
        required=True,
        type=Path,
        help="Absolute existing backend directory containing app/main.py",
    )
    parser.add_argument(
        "--dist",
        type=Path,
        default=Path(__file__).parent.parent / "app/public_portal",
        help="Prebuilt frontend directory (default: ../app/public_portal)",
    )
    args = parser.parse_args()
    try:
        release, backup = install(args.backend_root, args.dist)
    except (OSError, ValueError) as error:
        print(f"Portal installation failed: {error}", file=sys.stderr)
        return 1

    print(f"Portal release installed and selected: {release}")
    print(f"Active manifest: {release.parent.parent / 'active.json'}")
    if backup:
        print(f"Previous manifest for rollback: {backup}")
    else:
        print("This is the first active manifest; there is no previous manifest backup.")
    print("Restart the existing backend through its normal process manager when ready.")
    print("If AIS_PORTAL_DIR is set, the backend uses that directory instead of this manifest.")
    print(
        "No build, process restart, database initialization, or environment change was performed."
    )
    print("Cloudflare was not changed and previous releases were retained.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
