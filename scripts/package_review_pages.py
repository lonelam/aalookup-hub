#!/usr/bin/env python3
"""Package the reviewed file set; never package the candidate's other files."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tarfile

SHA = re.compile(r"\A[0-9a-f]{40}\Z")
HASH = re.compile(r"\A[0-9a-f]{64}\Z")
PATHS = {prefix + page for prefix in ("", "zh-cn/") for page in (
    "index.html", "support/index.html", "privacy/index.html", "pricing/index.html",
    "terms/index.html", "refunds/index.html",
)} | {"assets/paddle-review-navigation-v1.js"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def regular(path: Path, root: Path) -> None:
    if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
        raise ValueError(f"candidate file is not a regular single-link file: {path}")
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError(f"candidate parent is a symlink: {parent}")
        if parent == root:
            break


def package(candidate: Path, source_sha: str, manifest_sha: str, output: Path) -> Path:
    if not SHA.fullmatch(source_sha) or not HASH.fullmatch(manifest_sha):
        raise ValueError("expected exact source SHA and approved manifest SHA-256")
    manifest_path = candidate / "manifest.json"
    regular(manifest_path, candidate)
    if manifest_path.stat().st_size > 32768 or digest(manifest_path) != manifest_sha:
        raise ValueError("candidate manifest differs from the reviewed hash")
    manifest = json.loads(manifest_path.read_bytes())
    if (set(manifest) != {"schemaVersion", "kind", "sourceSha", "files"}
            or type(manifest["schemaVersion"]) is not int or manifest["schemaVersion"] != 1
            or manifest["kind"] != "aalookup-public-review-pages" or manifest["sourceSha"] != source_sha
            or not isinstance(manifest["files"], list) or not 1 <= len(manifest["files"]) <= len(PATHS)):
        raise ValueError("invalid review manifest")
    names = set()
    for entry in manifest["files"]:
        if (set(entry) != {"path", "sha256", "baseSha256"} or entry["path"] not in PATHS
                or entry["path"] in names or not HASH.fullmatch(entry["sha256"])
                or (entry["baseSha256"] is not None and not HASH.fullmatch(entry["baseSha256"]))):
            raise ValueError("invalid or duplicate review path")
        names.add(entry["path"])
        path = candidate / "website" / entry["path"]
        regular(path, candidate)
        if not 0 < path.stat().st_size <= 1024 * 1024 or digest(path) != entry["sha256"]:
            raise ValueError(f"candidate content differs: {entry['path']}")
    archive = output / f"aalookup-review-pages-{source_sha}-{manifest_sha}.tar.gz"
    with archive.open("xb") as writer:
        try:
            with tarfile.open(fileobj=writer, mode="w:gz", format=tarfile.USTAR_FORMAT) as payload:
                for name in ["manifest.json", *("website/" + name for name in sorted(names))]:
                    path = candidate / name
                    info = payload.gettarinfo(str(path), arcname=name)
                    info.uid = info.gid = info.mtime = 0
                    info.uname = info.gname = ""
                    info.mode = 0o644
                    with path.open("rb") as content:
                        payload.addfile(info, content)
        except BaseException:
            archive.unlink(missing_ok=True)
            raise
    print(json.dumps({"archive": archive.name, "sha256": digest(archive),
                      "sourceSha": source_sha, "manifestSha256": manifest_sha, "files": sorted(names)}))
    return archive


if __name__ == "__main__":
    package(Path("review-candidate"), os.environ["SOURCE_SHA"],
            os.environ["REVIEW_MANIFEST_SHA256"], Path.cwd())
