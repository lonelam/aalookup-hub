#!/usr/bin/env python3
"""Check or publish an exact candidate without replacing existing release bytes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat

from promote_release import GitHub, IDENTIFIER, REPO, SHA, SOURCE_REPO, STABLE, asset_manifest, positive, require, sha256
from release_provenance import ASSET_NAMES, PROVENANCE_NAME, write_provenance

ALL_NAMES = (*ASSET_NAMES, PROVENANCE_NAME)
RELEASES = f"repos/{REPO}/releases"


def validate(config, prerelease=None):
    for key in ("source_sha", "workflow_sha"):
        require(isinstance(config.get(key), str) and re.fullmatch(SHA, config[key]), f"invalid {key}")
    preview = STABLE + rf"-{IDENTIFIER}(?:\.{IDENTIFIER})*"
    pattern = f"(?:{STABLE}|{preview})" if prerelease is None else preview if prerelease else STABLE
    require(isinstance(config.get("version"), str) and re.fullmatch(pattern, config["version"]), "invalid candidate version or prerelease mode")


def markers(config):
    return (f"Private source: `{SOURCE_REPO}@{config['source_sha']}`.",
            f"Public workflow commit: `{config['workflow_sha']}`.")


def verify_markers(body, config):
    require(isinstance(body, str), "release body missing")
    for prefix, expected in zip(("Private source:", "Public workflow commit:"), markers(config)):
        require([line for line in body.splitlines() if line.startswith(prefix)] == [expected], "release source or workflow marker differs")


def release_body(notes, config):
    require(isinstance(notes, str) and notes.strip() and len(notes.encode()) <= 256 * 1024, "release notes missing or too large")
    body = notes
    for prefix, expected in zip(("Private source:", "Public workflow commit:"), markers(config)):
        if not any(line.startswith(prefix) for line in body.splitlines()):
            body = body.rstrip() + "\n\n" + expected + "\n"
    verify_markers(body, config)
    return body


def verify_release(release, config, *, published=False, release_id=None, body=None):
    require(isinstance(release, dict) and positive(release.get("id")), "release identity missing")
    require(release_id is None or release["id"] == release_id, "numeric release identity changed")
    require(release.get("tag_name") == config["version"] and release.get("target_commitish") == config["workflow_sha"], "release tag or target differs")
    require(release.get("draft") is (not published) and release.get("prerelease") is True, "release is already published or not a prerelease draft")
    verify_markers(release.get("body"), config)
    require(body is None or release["body"] == body, "release notes changed")
    if published:
        require(release.get("immutable") is True, "published release is not immutable")


def check_tag(api, config):
    tag = api.get(f"repos/{REPO}/git/ref/tags/{config['version']}")
    if tag is not None:
        require(isinstance(tag, dict) and tag.get("object", {}).get("type") == "commit"
                and tag["object"].get("sha") == config["workflow_sha"], "public tag is not owned by this workflow")
    return tag


def check(api, config):
    """Fail before builds for a published tag or a draft owned by other inputs."""
    validate(config)
    matches = [release for release in api.pages(RELEASES) if release.get("tag_name") == config["version"]]
    require(len(matches) <= 1, "multiple releases use the candidate tag")
    tag = check_tag(api, config)
    if not matches:
        return None
    release = matches[0]
    verify_release(release, config)
    require(tag is not None, "existing draft lost its public tag")
    return release


def local_manifest(directory, config, prerelease):
    validate(config, prerelease)
    require(not directory.is_symlink() and directory.is_dir(), "artifact directory must be regular")
    provenance = directory / PROVENANCE_NAME
    if not provenance.exists():
        write_provenance(directory, config["source_sha"], config["workflow_sha"], config["version"], prerelease=prerelease)
    require({path.name for path in directory.iterdir()} == set(ALL_NAMES), "local artifact set differs")
    expected = {}
    for name in sorted(ALL_NAMES):
        path = directory / name
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 < info.st_size <= 3 * 1024**3, "local artifact is not a bounded regular single-link file")
        expected[name] = {"name": name, "size": info.st_size, "digest": "sha256:" + sha256(path)}
    require(sum(item["size"] for item in expected.values()) <= 6 * 1024**3, "total artifact size exceeded")
    require(expected[PROVENANCE_NAME]["size"] <= 1024 * 1024, "provenance is too large")
    value = {"schemaVersion": 1, "sourceCommit": config["source_sha"], "workflowCommit": config["workflow_sha"],
             "releaseTag": config["version"], "assets": [{"name": name, "sha256": expected[name]["digest"][7:], "size": expected[name]["size"]} for name in sorted(ASSET_NAMES)]}
    # A retained local provenance is reusable only if it is the writer's exact output.
    require(provenance.read_bytes() == (json.dumps(value, indent=2) + "\n").encode(), "local provenance differs from current artifact bytes or identity")
    return expected


def matching_assets(assets, expected, *, complete=False):
    require(isinstance(assets, list) and all(isinstance(item, dict) for item in assets), "invalid asset listing")
    names = [item.get("name") for item in assets]
    require(len(names) == len(set(names)) and set(names) <= set(expected), "unexpected or duplicate existing asset")
    actual = asset_manifest(assets, expected if complete else names)
    for name, item in actual.items():
        require({key: item[key] for key in ("name", "size", "digest")} == expected[name], "existing or uploaded asset bytes differ")
    return actual


def publish(api, config, directory, notes, *, prerelease=False):
    validate(config, prerelease)
    check(api, config)
    body = release_body(notes, config)
    expected = local_manifest(directory, config, prerelease)
    release = check(api, config)
    if release is None:
        if check_tag(api, config) is None:
            api.request("POST", f"repos/{REPO}/git/refs", {"ref": f"refs/tags/{config['version']}", "sha": config["workflow_sha"]})
            require(check_tag(api, config) is not None, "created public tag missing")
        # A conflicting concurrent creator must be inspected, never overwritten.
        require(check(api, config) is None, "candidate draft appeared before creation")
        release = api.request("POST", RELEASES, {"tag_name": config["version"], "target_commitish": config["workflow_sha"],
            "name": config["version"], "body": body, "draft": True, "prerelease": True, "make_latest": "false"})
        verify_release(release, config, body=body)
        require(release.get("assets") == [], "new draft unexpectedly contains assets")
    verify_release(release, config, body=body)
    release_id = release["id"]
    path = f"{RELEASES}/{release_id}"
    uploaded = matching_assets(api.pages(path + "/assets"), expected)
    for name in sorted(expected):
        if name not in uploaded:
            item = api.upload(release["upload_url"], release_id, directory / name)
            uploaded.update(matching_assets([item], {name: expected[name]}))
    require(len({item["id"] for item in uploaded.values()}) == len(expected), "uploaded asset IDs repeat")
    fresh = check(api, config)
    verify_release(fresh, config, release_id=release_id, body=body)
    verify_release(api.get(path), config, release_id=release_id, body=body)
    require(matching_assets(api.pages(path + "/assets"), expected, complete=True) == uploaded, "draft asset IDs changed before publication")
    require(check_tag(api, config) is not None, "public tag disappeared before publication")
    # The sole publication PATCH. There is no automatic retry or failed-draft cleanup.
    published = api.request("PATCH", path, {"draft": False, "prerelease": True, "make_latest": "false"})
    verify_release(published, config, published=True, release_id=release_id, body=body)
    require(matching_assets(published.get("assets"), expected, complete=True) == uploaded, "publication response assets differ")
    fresh = api.get(path)
    verify_release(fresh, config, published=True, release_id=release_id, body=body)
    require(matching_assets(fresh.get("assets"), expected, complete=True) == uploaded, "published assets differ")
    require(matching_assets(api.pages(path + "/assets"), expected, complete=True) == uploaded, "published asset listing differs")
    require(check_tag(api, config) is not None, "published tag disappeared")
    return published


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Read-only publication target preflight")
    publication = commands.add_parser("publish", help="Publish exact local bytes as an immutable acceptance candidate")
    publication.add_argument("--directory", type=Path, required=True)
    publication.add_argument("--notes", type=Path, required=True)
    publication.add_argument("--prerelease", action="store_true")
    args = parser.parse_args()
    config = {"version": os.environ.get("VERSION", ""), "source_sha": os.environ.get("PRIVATE_SOURCE_SHA", ""),
              "workflow_sha": os.environ.get("PUBLIC_WORKFLOW_SHA", "")}
    validate(config)
    token = os.environ.get("GH_TOKEN", "")
    require(bool(token.strip()), "GH_TOKEN is required")
    api = GitHub(token)
    if args.command == "check":
        check(api, config)
        print(f"Publication target {config['version']} is available or a matching unpublished draft.")
    else:
        info = args.notes.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 < info.st_size <= 256 * 1024, "notes must be a bounded regular single-link file")
        result = publish(api, config, args.directory, args.notes.read_text(), prerelease=args.prerelease)
        print(f"Published immutable candidate {config['version']} (release {result['id']}); distribution approval remains separate.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit(f"Candidate refused: {error}") from None
