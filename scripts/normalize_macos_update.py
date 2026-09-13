#!/usr/bin/env python3
"""Normalize Tauri's Unix type bits without changing a signed app bundle."""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tarfile
import tempfile
import unicodedata

ROOT = "AALookup.app"
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_BYTES = 2 * 1024 * 1024 * 1024
MAX_ENTRIES = 100_000


def require(condition, message):
    if not condition:
        raise ValueError(message)


def safe_path(name):
    name = name.rstrip("/")
    require(0 < len(name.encode("utf-8")) <= 1024, "Invalid archive path length")
    for part in name.split("/"):
        stem = part.split(".")[0].upper()
        reserved = stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9]", stem)
        require(part and part not in {".", ".."} and not part.endswith((".", " ")) and not reserved,
                "Unsafe archive path")
        require(not any(unicodedata.category(char) == "Cc" or char in '\\:<>"|?*' for char in part),
                "Unsafe archive path character")
    require(name == ROOT or name.startswith(ROOT + "/"), "Unexpected application root")
    return name


def link_path(name, target):
    require(target and not target.startswith("/") and "\\" not in target and ":" not in target
            and len(target.encode("utf-8")) <= 1024, "Unsafe symlink target")
    parts = name.split("/")[:-1]
    for part in target.split("/"):
        if part == "..":
            require(len(parts) > 1, "Symlink escapes application root")
            parts.pop()
        elif part not in {"", "."}:
            safe_path(ROOT + "/" + part)
            parts.append(part)
    return safe_path("/".join(parts))


def strict_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "Duplicate metadata key")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique)


def inspect_archive(path: Path, version: str, *, normalized=False):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 < info.st_size <= MAX_FILE_BYTES,
            "Archive must be a bounded regular single-link file")
    entries = []
    nodes = {}
    explicit = set()
    total = 0
    metadata = None
    with tarfile.open(path, "r|gz") as archive:
        for member in archive:
            require(len(entries) < MAX_ENTRIES, "Too many archive entries")
            name = safe_path(member.name)
            require(name not in explicit, "Duplicate archive entry")
            explicit.add(name)
            require(member.sparse is None, "Sparse archive entries are forbidden")
            kinds = {tarfile.REGTYPE: ("file", stat.S_IFREG), tarfile.AREGTYPE: ("file", stat.S_IFREG),
                     tarfile.DIRTYPE: ("directory", stat.S_IFDIR), tarfile.SYMTYPE: ("symlink", stat.S_IFLNK)}
            require(member.type in kinds, "Unsupported archive entry type")
            kind, type_bits = kinds[member.type]
            # Some Tauri tar headers contain the whole Unix st_mode. Only the
            # redundant, matching type bits may be removed; never repair a
            # privileged mode or a disagreement with the actual tar entry type.
            allowed_types = {0} if normalized else {0, type_bits}
            require(member.mode >= 0 and member.mode & ~0o777 in allowed_types,
                    "Privileged or mismatched archive mode")
            require(0 <= member.size <= MAX_FILE_BYTES, "Archive file exceeds size limit")
            total += member.size
            require(total <= MAX_BYTES, "Archive exceeds expanded size limit")
            require(kind == "file" or member.size == 0, "Non-file entry contains data")
            require(not any("sparse" in key.lower() or key.lower().endswith("mode")
                            for key in member.pax_headers), "Unsupported PAX mode or sparse override")
            parts = name.split("/")
            for index in range(1, len(parts)):
                parent = "/".join(parts[:index])
                require(parent not in nodes or nodes[parent]["kind"] == "directory",
                        "Archive parent is not a directory")
                nodes.setdefault(parent, {"kind": "directory"})
            require(name not in nodes or nodes[name]["kind"] == kind == "directory",
                    "Conflicting archive entry")
            record = {"name": name, "kind": kind, "mode": member.mode & 0o777, "size": member.size,
                      "type": member.type.decode("ascii"), "linkname": member.linkname,
                      "uid": member.uid, "gid": member.gid, "uname": member.uname,
                      "gname": member.gname, "mtime": member.mtime, "paxHeaders": member.pax_headers}
            if kind == "symlink":
                record["target"] = link_path(name, member.linkname)
            elif kind == "file":
                digest = hashlib.sha256()
                data = bytearray()
                with archive.extractfile(member) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                        if name == ROOT + "/Contents/Resources/aalookup-update.json":
                            require(member.size <= 4096, "Update metadata exceeds size limit")
                            data.extend(chunk)
                record["sha256"] = digest.hexdigest()
                if name == ROOT + "/Contents/Resources/aalookup-update.json":
                    metadata = strict_json(data)
            entries.append(record)
            nodes[name] = record
            require(len(nodes) <= MAX_ENTRIES, "Too many archive paths")
    aliases = ["".join(char.lower() for char in unicodedata.normalize("NFC", name)) for name in nodes]
    require(len(set(aliases)) == len(aliases), "Case or Unicode archive path collision")
    require(isinstance(metadata, dict) and type(metadata.get("format")) is int
            and metadata == {"format": 1, "version": version, "platform": "macos"},
            "Unexpected staged update metadata")
    for executable in (ROOT + "/Contents/MacOS/aalookup", ROOT + "/Contents/Helpers/aalookup-update-helper"):
        entry = nodes.get(executable, {})
        require(entry.get("kind") == "file" and entry["size"] > 0 and entry["mode"] & 0o111,
                "Missing executable app or update helper")
    for name, node in nodes.items():
        if node["kind"] != "symlink":
            continue
        # Resolve components in filesystem order: a symlink must be followed
        # before a subsequent '..', otherwise a lexically contained link can
        # actually escape the app. No files or links are created for this check.
        resolved = name.split("/")[:-1]
        remaining = node["linkname"].split("/")
        followed = 0
        while remaining:
            part, remaining = remaining[0], remaining[1:]
            if part in {"", "."}:
                continue
            if part == "..":
                require(len(resolved) > 1, "Resolved symlink escapes application root")
                resolved.pop()
                continue
            candidate = "/".join(resolved + [part])
            found = nodes.get(candidate)
            require(found is not None, "Dangling symlink")
            if found["kind"] == "symlink":
                followed += 1
                require(followed <= 40, "Symlink cycle or excessive indirection")
                remaining = found["linkname"].split("/") + remaining
            else:
                require(not remaining or found["kind"] == "directory", "Symlink traverses a non-directory")
                resolved.append(part)
        require("/".join(resolved) in nodes, "Dangling symlink")
    return entries


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_archive(path: Path, version: str):
    original = inspect_archive(path, version)
    original_sha256 = file_sha256(path)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".normalized-", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            with gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as destination:
                    with tarfile.open(path, "r|gz") as source:
                        for member in source:
                            normalized = copy.copy(member)
                            normalized.mode = member.mode & 0o777
                            stream = source.extractfile(member) if member.isfile() else None
                            try:
                                destination.addfile(normalized, stream)
                            finally:
                                if stream is not None:
                                    stream.close()
            output.flush()
            os.fsync(output.fileno())
        require(inspect_archive(temporary, version, normalized=True) == original,
                "Bundle content or metadata changed during normalization")
        require(file_sha256(path) == original_sha256, "Source archive changed during normalization")
        os.replace(temporary, path)
        return {"entries": len(original), "originalSha256": original_sha256,
                "normalizedSha256": file_sha256(path), "bundleContentAndMetadataPreserved": True}
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("version")
    parser.add_argument("--check", action="store_true", help="Require normalized headers without changing bytes")
    args = parser.parse_args()
    result = ({"entries": len(inspect_archive(args.archive, args.version, normalized=True)), "normalized": True}
              if args.check else normalize_archive(args.archive, args.version))
    print(json.dumps(result))
