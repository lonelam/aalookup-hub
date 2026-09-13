#!/usr/bin/env python3
"""Describe final signed candidate bytes without exposing private source files."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat

ASSET_NAMES = (
    "AALookup-macos-aarch64.dmg", "AALookup-macos-aarch64.zip",
    "AALookup-macos-universal.dmg", "AALookup-macos-universal.app.tar.gz",
    "AALookup-macos-universal.app.tar.gz.sig", "AALookup-macos-universal.zip",
    "AALookup-macos-x86_64.dmg", "AALookup-macos-x86_64.zip",
    "AALookup-windows-x86_64-setup.exe", "AALookup-windows-x86_64-setup.exe.sig",
    "AALookup-windows-x86_64-update.tar.gz", "AALookup-windows-x86_64-update.tar.gz.sig",
    "AALookup-android-aarch64.apk", "AALookup-android-aarch64.apk.sig",
    "AALookup-ios-arm64.ipa",
)
PROVENANCE_NAME = "release-provenance.json"


def write_provenance(directory: Path, source_commit: str, workflow_commit: str, release_tag: str,
                     *, prerelease: bool = False) -> Path:
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (source_commit, workflow_commit)):
        raise ValueError("source and workflow commits must be full lowercase SHA values")
    version = r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    if prerelease:
        identifier = r"(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
        version += rf"-{identifier}(?:\.{identifier})*"
    if not re.fullmatch(version, release_tag):
        kind = "prerelease" if prerelease else "stable"
        raise ValueError(f"release tag must be a {kind} v-prefixed version")
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("artifact directory must be a regular directory")
    if {path.name for path in directory.iterdir()} != set(ASSET_NAMES):
        raise ValueError("candidate must contain exactly the required platform artifacts")
    assets = []
    for name in sorted(ASSET_NAMES):
        path = directory / name
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_nlink != 1:
            raise ValueError(f"candidate artifact must be a nonempty regular single-link file: {name}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        assets.append({"name": name, "sha256": digest.hexdigest(), "size": metadata.st_size})
    result = {
        "schemaVersion": 1,
        "sourceCommit": source_commit,
        "workflowCommit": workflow_commit,
        "releaseTag": release_tag,
        "assets": assets,
    }
    output = directory / PROVENANCE_NAME
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prerelease", action="store_true")
    parser.add_argument("directory", type=Path)
    parser.add_argument("source_commit")
    parser.add_argument("workflow_commit")
    parser.add_argument("release_tag")
    args = parser.parse_args()
    print(write_provenance(args.directory, args.source_commit, args.workflow_commit,
                           args.release_tag, prerelease=args.prerelease))
