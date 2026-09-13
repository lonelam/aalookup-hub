#!/usr/bin/env python3
"""Promote verified preview bytes through an isolated, immutable candidate draft."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import tomllib
import zipfile
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from release_provenance import ASSET_NAMES, PROVENANCE_NAME, write_provenance

REPO = "lonelam/aalookup-hub"
SOURCE_REPO = "lonelam/aalookup"
STABLE = r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
IDENTIFIER = r"(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
SHA = r"[0-9a-f]{40}"
DIGEST = r"[0-9a-f]{64}"
JOB_STEPS = {
    "Resolve private source": {"Checkout exact private source", "Verify source identity and resolve preview version"},
    "macOS (arm64 + x64 + universal)": {"Checkout exact private source", "Build and collect macOS artifacts", "Normalize staged macOS update", "Sign normalized macOS update", "Verify signatures, notarization, and updater archive", "Upload workflow artifacts"},
    "Windows (x64)": {"Checkout exact private source", "Build and collect Windows artifacts", "Upload workflow artifacts"},
    "Android (arm64 APK)": {"Checkout exact private source", "Build signed APK and updater artifacts", "Upload Android artifacts"},
    "iOS (IPA + TestFlight)": {"Checkout exact private source", "Build the signed IPA", "Upload to TestFlight", "Upload iOS artifacts"},
    "Publish GitHub prerelease": {"Verify trusted candidate packaging", "Stage and publish immutable GitHub prerelease"},
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def positive(value):
    return type(value) is int and value > 0


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_inputs(config):
    for key in ("source_sha", "workflow_sha"):
        require(re.fullmatch(SHA, config[key]), f"invalid {key}")
    require(re.fullmatch(DIGEST, config["preview_provenance_sha256"]), "invalid preview provenance SHA-256")
    require(re.fullmatch(STABLE, config["version"]), "version must be stable v-prefixed SemVer")
    require(re.fullmatch(STABLE + rf"-{IDENTIFIER}(?:\.{IDENTIFIER})*", config["preview_tag"]), "preview tag must be explicit prerelease SemVer")
    require(config["preview_tag"].split("-", 1)[0] == config["version"], "preview base version differs")
    for key in ("preview_run_id", "preview_release_id", "run_id", "run_attempt"):
        require(positive(config[key]), f"invalid {key}")
    require(config["run_id"] != config["preview_run_id"], "promotion must be a separate run")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    """No mutation retries, and no credentials forwarded to signed download URLs."""
    def __init__(self, token):
        self.token = token
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, body=None):
        require(path.startswith("repos/") and ".." not in path, "invalid API path")
        data = None if body is None else json.dumps(body).encode()
        request = Request("https://api.github.com/" + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
            "Content-Type": "application/json", "X-GitHub-Api-Version": "2026-03-10",
        })
        try:
            with self.opener.open(request, timeout=60) as response:
                payload = response.read(8 * 1024 * 1024 + 1)
        except HTTPError as error:
            if method == "GET" and error.code == 404:
                return None
            raise ValueError(f"GitHub {method} {path.split('?')[0]} failed (HTTP {error.code})") from None
        require(len(payload) <= 8 * 1024 * 1024, "API response too large")
        return json.loads(payload) if payload else None

    def get(self, path):
        return self.request("GET", path)

    def pages(self, path, field=None):
        result = []
        for page in range(1, 101):
            payload = self.get(f"{path}?per_page=100&page={page}")
            require(payload is not None, "paginated API result missing")
            batch = payload[field] if field else payload
            require(isinstance(batch, list), "invalid API page")
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise ValueError("pagination limit exceeded")

    def download(self, asset, destination):
        self.download_file(f"repos/{REPO}/releases/assets/{asset['id']}", destination,
                           asset["size"], asset["digest"])

    def download_file(self, path, destination, expected_size, expected_digest, *, artifact=False):
        url = "https://api.github.com/" + path
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json" if artifact else "application/octet-stream",
                   "X-GitHub-Api-Version": "2026-03-10"}
        try:
            response = self.opener.open(Request(url, headers=headers), timeout=60)
        except HTTPError as error:
            status = error.code
            location = error.headers.get("Location", "")
            error.close()
            require(status in (301, 302, 303, 307, 308), "asset download API failed")
            parsed = urlsplit(location)
            host = parsed.hostname or ""
            allowed = host in ("release-assets.githubusercontent.com", "objects.githubusercontent.com")
            if artifact:
                allowed = allowed or host.endswith(".blob.core.windows.net")
            require(parsed.scheme == "https" and allowed and not parsed.username and not parsed.password, "unexpected asset redirect")
            # A signed URL is transient and stays in memory; the GitHub token stays on api.github.com.
            response = self.opener.open(Request(location), timeout=60)
        size = 0
        deadline = time.monotonic() + 600
        with response, destination.open("xb") as stream:
            require(response.status == 200, "asset download status differs")
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                require(size <= expected_size and time.monotonic() < deadline, "asset download limit exceeded")
                stream.write(chunk)
        require(size == expected_size and "sha256:" + sha256(destination) == expected_digest, "downloaded asset digest or size differs")

    def upload(self, upload_url, release_id, path):
        expected = f"https://uploads.github.com/repos/{REPO}/releases/{release_id}/assets"
        require(upload_url.split("{", 1)[0] == expected, "upload URL is not the new numeric release ID")
        # urllib streams a file object with an explicit length; no shell, tag lookup or clobber.
        with path.open("rb") as stream:
            request = Request(expected + "?name=" + quote(path.name, safe=""), data=stream, method="POST", headers={
                "Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                "Content-Type": "application/octet-stream", "Content-Length": str(path.stat().st_size),
                "X-GitHub-Api-Version": "2026-03-10",
            })
            try:
                with self.opener.open(request, timeout=180) as response:
                    require(response.status == 201, "asset upload did not create a new asset")
                    return json.loads(response.read(1024 * 1024))
            except HTTPError as error:
                raise ValueError(f"asset upload failed (HTTP {error.code}); draft retained") from None


def assert_run(run, jobs, config, build_sha):
    require(isinstance(run, dict) and run.get("id") == config["preview_run_id"], "preview run ID differs")
    require(run.get("repository", {}).get("full_name") == REPO and run.get("head_repository", {}).get("full_name") == REPO, "preview run repository differs")
    require(run.get("path") == ".github/workflows/pre-release.yml" and run.get("event") == "workflow_dispatch", "preview workflow differs")
    require(run.get("display_title") == f"Preview {config['preview_tag']} from {config['source_sha']}", "preview run source/tag inputs differ")
    require(run.get("status") == "completed" and run.get("conclusion") == "success", "preview run is not successful")
    require(run.get("head_sha") == build_sha and positive(run.get("run_attempt")), "preview build workflow differs")
    require(len(jobs) == 6 and {job.get("name") for job in jobs} == set(JOB_STEPS), "preview must have all six expected jobs")
    require(len({job.get("id") for job in jobs}) == 6, "duplicate preview job")
    for job in jobs:
        require(positive(job.get("id")) and job.get("run_id") == run["id"] and job.get("head_sha") == build_sha, "preview job identity differs")
        require(job.get("status") == "completed" and job.get("conclusion") == "success", "preview job failed")
        steps = {step["name"] for step in job.get("steps", []) if step.get("status") == "completed" and step.get("conclusion") == "success"}
        require(JOB_STEPS[job["name"]] <= steps, "preview required step did not succeed")


def asset_manifest(assets, names):
    require(isinstance(assets, list) and len(assets) == len(names), "asset count differs")
    require({asset.get("name") for asset in assets} == set(names), "asset names differ")
    require(len({asset.get("id") for asset in assets}) == len(names), "duplicate asset ID")
    result = {}
    for asset in assets:
        require(positive(asset.get("id")) and positive(asset.get("size")) and asset["size"] <= 3 * 1024**3, "invalid asset ID or size")
        require(asset.get("state") == "uploaded" and re.fullmatch(r"sha256:" + DIGEST, asset.get("digest", "")), "asset digest/state unavailable")
        result[asset["name"]] = {key: asset[key] for key in ("id", "name", "size", "digest")}
    require(sum(item["size"] for item in result.values()) <= 6 * 1024**3, "total asset limit exceeded")
    return result


def assert_preview(release, provenance, config, build_sha):
    require(release.get("id") == config["preview_release_id"] and release.get("tag_name") == config["preview_tag"], "preview release identity differs")
    require(release.get("draft") is False and release.get("prerelease") is True, "preview release is not published prerelease")
    require(release.get("target_commitish") == build_sha, "preview release target differs")
    require(f"Private source: `lonelam/aalookup@{config['source_sha']}`." in release.get("body", ""), "preview release source marker differs")
    require(f"Public workflow commit: `{build_sha}`." in release.get("body", ""), "preview workflow marker differs")
    require(set(provenance) == {"schemaVersion", "sourceCommit", "workflowCommit", "releaseTag", "assets"}, "provenance contract differs")
    require(type(provenance["schemaVersion"]) is int and provenance["schemaVersion"] == 1, "provenance schema differs")
    require((provenance["sourceCommit"], provenance["workflowCommit"], provenance["releaseTag"]) == (config["source_sha"], build_sha, config["preview_tag"]), "provenance source/workflow/tag differs")
    manifest = asset_manifest(release["assets"], (*ASSET_NAMES, PROVENANCE_NAME))
    rows = provenance["assets"]
    require(isinstance(rows, list) and len(rows) == 15 and [row.get("name") for row in rows] == sorted(ASSET_NAMES), "provenance artifact set differs")
    for row in rows:
        require(set(row) == {"name", "sha256", "size"} and positive(row["size"]) and re.fullmatch(DIGEST, row["sha256"]), "invalid provenance asset")
        actual = manifest[row["name"]]
        require((actual["size"], actual["digest"]) == (row["size"], "sha256:" + row["sha256"]), "provenance/API asset drift")
    return manifest


def private_tag(api, version, source):
    value = api.get(f"repos/{SOURCE_REPO}/git/ref/tags/{version}")
    require(value is not None, "private stable tag missing")
    value = value["object"]
    for _ in range(5):
        if value.get("type") == "commit":
            break
        require(value.get("type") == "tag" and re.fullmatch(SHA, value.get("sha", "")), "invalid private tag object")
        value = api.get(f"repos/{SOURCE_REPO}/git/tags/{value['sha']}")["object"]
    require(value.get("type") == "commit" and value.get("sha") == source, "private stable tag does not resolve to source")


def source_notes(directory, config):
    head = subprocess.check_output(["git", "-C", str(directory), "rev-parse", "HEAD"], text=True).strip()
    require(head == config["source_sha"], "private checkout differs")
    version = config["version"][1:]
    for name in ("package.json", "src-tauri/tauri.conf.json"):
        require(json.loads((directory / name).read_text())["version"] == version, "source package version differs")
    require(tomllib.loads((directory / "src-tauri/Cargo.toml").read_text())["package"]["version"] == version, "source Cargo version differs")
    changelog = directory / "changelog" / f"{version}.json"
    data = json.loads(changelog.read_text())
    require(data["version"] == version and isinstance(data["changes"], list) and data["changes"], "source changelog differs")
    lines = []
    for locale in ("en", "zh-CN"):
        require(isinstance(data["headline"][locale], str), "changelog headline missing")
        lines.extend([data["headline"][locale], ""])
        for change in data["changes"]:
            summary = change["summary"][locale]
            require(isinstance(summary, str) and summary.strip(), "changelog summary missing")
            platforms = change.get("platforms")
            prefix = ""
            if platforms is not None:
                labels = {"macos": "macOS", "windows": "Windows"}
                require(isinstance(platforms, list) and platforms and all(platform in labels for platform in platforms) and len(set(platforms)) == len(platforms), "invalid changelog platform restriction")
                prefix = "**" + " / ".join(labels[platform] for platform in platforms) + "** — "
            lines.append("- " + prefix + summary)
        lines.append("")
    return "\n".join(lines), sha256(changelog)


def preconditions(api, config, *, publication=False):
    if publication:
        # This administration-read endpoint is available to the operator, not GITHUB_TOKEN.
        immutable = api.get(f"repos/{REPO}/immutable-releases")
        require(immutable is not None and immutable.get("enabled") is True, "immutable releases must be enabled")
    tag = api.get(f"repos/{REPO}/git/ref/tags/{config['version']}")
    require(tag is not None and tag.get("object", {}).get("type") == "commit" and tag["object"].get("sha") == config["workflow_sha"], "stable public tag must point to this promotion workflow")
    require(not any(item.get("tag_name") == config["version"] for item in api.pages(f"repos/{REPO}/releases")), "stable release already exists")


def prepare(api, config, directory, notes, changelog_sha, recheck_source=lambda: None):
    validate_inputs(config)
    require(not directory.exists(), "promotion directory must be new")
    directory.mkdir(mode=0o700, parents=True)
    receipt_path = directory / "promotion-receipt.json"
    receipt = {"schemaVersion": 1, "status": "verifying", "sourceCommit": config["source_sha"],
               "promotionWorkflowCommit": config["workflow_sha"], "promotionRunId": config["run_id"],
               "promotionRunAttempt": config["run_attempt"], "releaseTag": config["version"],
               "changelogSha256": changelog_sha, "origin": {"runId": config["preview_run_id"],
               "releaseId": config["preview_release_id"], "tag": config["preview_tag"],
               "provenanceSha256": config["preview_provenance_sha256"]}, "approvedForDistribution": False}

    def save():
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")

    save()
    try:
        preconditions(api, config)
        recheck_source()
        run_path = f"repos/{REPO}/actions/runs/{config['preview_run_id']}"
        run = api.get(run_path)
        require(run is not None and re.fullmatch(SHA, run.get("head_sha", "")) and positive(run.get("run_attempt")), "preview run unavailable")
        build_sha = run["head_sha"]
        jobs = api.pages(run_path + f"/attempts/{run['run_attempt']}/jobs", "jobs")
        assert_run(run, jobs, config, build_sha)
        receipt["origin"].update(workflowCommit=build_sha, runAttempt=run["run_attempt"], jobs=[{"id": job["id"], "name": job["name"]} for job in jobs])
        preview_path = f"repos/{REPO}/releases/{config['preview_release_id']}"
        preview = api.get(preview_path)
        require(isinstance(preview, dict), "preview release unavailable")
        initial = asset_manifest(preview.get("assets"), (*ASSET_NAMES, PROVENANCE_NAME))
        origin_prov = directory / "preview-provenance.json"
        require(initial[PROVENANCE_NAME]["size"] <= 1024 * 1024, "preview provenance too large")
        require(initial[PROVENANCE_NAME]["digest"] == "sha256:" + config["preview_provenance_sha256"], "frozen preview provenance SHA differs")
        api.download(initial[PROVENANCE_NAME], origin_prov)
        require(sha256(origin_prov) == config["preview_provenance_sha256"], "frozen preview provenance SHA differs")
        provenance = json.loads(origin_prov.read_text())
        manifest = assert_preview(preview, provenance, config, build_sha)
        payloads = directory / "assets"
        payloads.mkdir(mode=0o700)
        for name in sorted(ASSET_NAMES):
            api.download(manifest[name], payloads / name)
        write_provenance(payloads, config["source_sha"], config["workflow_sha"], config["version"])
        expected = {path.name: {"name": path.name, "size": path.stat().st_size, "digest": "sha256:" + sha256(path)} for path in payloads.iterdir()}
        require(set(expected) == {*ASSET_NAMES, PROVENANCE_NAME}, "promotion payload set differs")
        receipt["origin"]["assets"] = list(manifest.values())
        receipt["assets"] = list(expected.values())
        receipt["status"] = "verified-preview"
        save()
        # No remote mutation occurs until every input byte and precondition has passed.
        require(assert_preview(api.get(preview_path), provenance, config, build_sha) == manifest, "preview changed before staging")
        fresh_run = api.get(run_path)
        require(fresh_run["run_attempt"] == run["run_attempt"], "preview run attempt changed")
        assert_run(fresh_run, jobs, config, build_sha)
        preconditions(api, config)
        recheck_source()
        temporary_tag = f"{config['version']}-promotion.{config['run_id']}.{config['run_attempt']}"
        require(api.get(f"repos/{REPO}/git/ref/tags/{temporary_tag}") is None, "temporary tag already exists")
        require(not any(item.get("tag_name") == temporary_tag for item in api.pages(f"repos/{REPO}/releases")), "temporary draft already exists")
        body = (notes + f"\nPrivate source: `lonelam/aalookup@{config['source_sha']}`.\n"
                f"Public workflow commit: `{config['workflow_sha']}`.\n\n"
                f"Original build workflow commit: `{build_sha}`. Original preview run: `{config['preview_run_id']}` "
                f"(attempt {run['run_attempt']}); preview release: `{config['preview_release_id']}` / `{config['preview_tag']}`.\n"
                f"Original preview provenance SHA-256: `{config['preview_provenance_sha256']}`.\n"
                "All 15 platform artifacts are copied byte-for-byte from that tested preview. "
                "The public workflow above performs promotion and does not rebuild or re-sign them.\n\n"
                "This is an acceptance candidate. Website and updater distribution still require the existing release approval.\n")
        draft = api.request("POST", f"repos/{REPO}/releases", {"tag_name": temporary_tag,
            "target_commitish": config["workflow_sha"], "name": config["version"], "body": body,
            "draft": True, "prerelease": True, "make_latest": "false"})
        require(positive(draft.get("id")) and draft["id"] != config["preview_release_id"], "new draft ID invalid")
        new_id = draft["id"]
        receipt["newReleaseId"] = new_id
        receipt["temporaryTag"] = temporary_tag
        receipt["status"] = "draft-created"
        save()
        require(draft.get("draft") is True and draft.get("prerelease") is True and draft.get("target_commitish") == config["workflow_sha"] and draft.get("tag_name") == temporary_tag and not draft.get("assets"), "new draft differs")
        uploaded = {}
        for name in sorted(expected):
            item = api.upload(draft["upload_url"], new_id, payloads / name)
            parsed = asset_manifest([item], [name])[name]
            require({key: parsed[key] for key in ("name", "size", "digest")} == expected[name], "uploaded asset differs")
            uploaded[name] = parsed
            receipt["uploadedAssets"] = list(uploaded.values())
            save()
        new_path = f"repos/{REPO}/releases/{new_id}"

        receipt["body"] = body
        receipt["assets"] = list(uploaded.values())
        verify_new(api.get(new_path), receipt, published=False)
        require(asset_manifest(api.pages(new_path + "/assets"), expected) == uploaded, "uploaded asset listing drifted")
        require(assert_preview(api.get(preview_path), provenance, config, build_sha) == manifest, "preview changed before publication")
        preconditions(api, config)
        recheck_source()
        receipt["status"] = "PREPARED"
        save()
        return receipt
    except Exception:
        receipt["failedStage"] = receipt["status"]
        receipt["status"] = "failed"
        save()
        raise


def verify_new(value, receipt, *, published):
    require(isinstance(value, dict) and value.get("id") == receipt["newReleaseId"] and value.get("draft") is (not published) and value.get("prerelease") is True, "new release state differs")
    require(value.get("tag_name") == (receipt["releaseTag"] if published else receipt["temporaryTag"]), "new release tag differs")
    require(value.get("target_commitish") == receipt["promotionWorkflowCommit"] and value.get("body") == receipt["body"], "new release ownership differs")
    expected = {item["name"]: item for item in receipt["assets"]}
    require(asset_manifest(value.get("assets"), (*ASSET_NAMES, PROVENANCE_NAME)) == expected, "new release asset IDs/digests drifted")
    if published:
        require(value.get("immutable") is True, "published release is not immutable")


def receipt_config(receipt):
    require(receipt.get("schemaVersion") == 1 and receipt.get("status") == "PREPARED" and receipt.get("approvedForDistribution") is False, "receipt is not an unapproved prepared candidate")
    origin = receipt["origin"]
    config = {"source_sha": receipt["sourceCommit"], "workflow_sha": receipt["promotionWorkflowCommit"],
              "version": receipt["releaseTag"], "run_id": receipt["promotionRunId"],
              "run_attempt": receipt["promotionRunAttempt"], "preview_run_id": origin["runId"],
              "preview_release_id": origin["releaseId"], "preview_tag": origin["tag"],
              "preview_provenance_sha256": origin["provenanceSha256"]}
    validate_inputs(config)
    require(positive(receipt["newReleaseId"]) and receipt["newReleaseId"] != origin["releaseId"], "prepared release ID differs")
    require(receipt["temporaryTag"] == f"{config['version']}-promotion.{config['run_id']}.{config['run_attempt']}", "prepared temporary tag differs")
    return config


def verify_receipt_origin(api, receipt, original_bytes, directory):
    """Authenticate the receipt as bytes from the successful preparation CI artifact."""
    config = receipt_config(receipt)
    run_path = f"repos/{REPO}/actions/runs/{config['run_id']}"
    run = api.get(run_path)
    require(run.get("id") == config["run_id"] and run.get("repository", {}).get("full_name") == REPO and run.get("head_repository", {}).get("full_name") == REPO, "preparation CI repository differs")
    require(run.get("path") == ".github/workflows/promote-release.yml" and run.get("event") == "workflow_dispatch" and run.get("head_sha") == config["workflow_sha"], "preparation CI workflow differs")
    require(run.get("display_title") == f"Promote {config['preview_tag']} to {config['version']} from {config['source_sha']}", "preparation CI inputs differ")
    require(run.get("status") == "completed" and run.get("conclusion") == "success" and run.get("run_attempt") == config["run_attempt"], "preparation CI is not the successful recorded attempt")
    jobs = api.pages(run_path + f"/attempts/{config['run_attempt']}/jobs", "jobs")
    require(len(jobs) == 1 and positive(jobs[0].get("id")) and jobs[0].get("run_id") == config["run_id"] and jobs[0].get("name") == "Prepare verified preview bytes" and jobs[0].get("status") == "completed" and jobs[0].get("conclusion") == "success" and jobs[0].get("head_sha") == config["workflow_sha"], "preparation CI job differs")
    steps = {step["name"] for step in jobs[0].get("steps", []) if step.get("status") == "completed" and step.get("conclusion") == "success"}
    require({"Copy verified bytes into isolated draft", "Preserve promotion receipt"} <= steps, "preparation CI steps incomplete")
    artifacts = api.pages(run_path + "/artifacts", "artifacts")
    name = f"promotion-receipt-{config['run_id']}-{config['run_attempt']}"
    matches = [item for item in artifacts if item.get("name") == name]
    require(len(matches) == 1, "preparation receipt artifact missing or ambiguous")
    artifact = matches[0]
    require(positive(artifact.get("id")) and artifact.get("expired") is False and positive(artifact.get("size_in_bytes")) and artifact["size_in_bytes"] <= 4 * 1024 * 1024 and re.fullmatch(r"sha256:" + DIGEST, artifact.get("digest", "")), "preparation artifact metadata invalid")
    require(artifact.get("workflow_run", {}).get("id") == config["run_id"] and artifact["workflow_run"].get("head_sha") == config["workflow_sha"], "preparation artifact run differs")
    archive = directory / "ci-receipt.zip"
    api.download_file(f"repos/{REPO}/actions/artifacts/{artifact['id']}/zip", archive,
                      artifact["size_in_bytes"], artifact["digest"], artifact=True)
    with zipfile.ZipFile(archive) as zipped:
        names = {"promotion-receipt.json", "preview-provenance.json", "assets/release-provenance.json"}
        require(len(zipped.infolist()) == 3 and set(zipped.namelist()) == names, "receipt artifact members differ")
        require(all(item.file_size <= 1024 * 1024 and not item.is_dir() for item in zipped.infolist()), "receipt artifact member limit exceeded")
        require(zipped.testzip() is None, "receipt artifact CRC failure")
        require(zipped.read("promotion-receipt.json") == original_bytes, "local receipt does not match successful CI artifact")
        origin_prov = zipped.read("preview-provenance.json")
        require(hashlib.sha256(origin_prov).hexdigest() == config["preview_provenance_sha256"], "CI preview provenance differs")
        stable_prov = zipped.read("assets/release-provenance.json")
        expected = {item["name"]: item for item in receipt["assets"]}
        require("sha256:" + hashlib.sha256(stable_prov).hexdigest() == expected[PROVENANCE_NAME]["digest"], "CI stable provenance differs")
        return json.loads(origin_prov), json.loads(stable_prov), {key: artifact[key] for key in ("id", "name", "size_in_bytes", "digest")}


def publish(api, receipt_path, receipt_sha256, directory, recheck_source, *, origin_verifier=verify_receipt_origin):
    require(re.fullmatch(DIGEST, receipt_sha256), "receipt SHA-256 must be pinned")
    original_bytes = receipt_path.read_bytes()
    require(hashlib.sha256(original_bytes).hexdigest() == receipt_sha256, "prepared receipt SHA differs")
    receipt = json.loads(original_bytes)
    config = receipt_config(receipt)
    require(not directory.exists(), "publication evidence directory must be new")
    directory.mkdir(mode=0o700, parents=True)
    result = {"status": "verifying-prepared-receipt", "preparedReceiptSha256": receipt_sha256,
              "preparedReceipt": receipt, "approvedForDistribution": False}
    evidence = directory / "publication-receipt.json"

    def save():
        evidence.write_text(json.dumps(result, indent=2) + "\n")

    save()
    try:
        preconditions(api, config, publication=True)
        recheck_source(config)
        original_prov, stable_prov, artifact = origin_verifier(api, receipt, original_bytes, directory)
        result["preparationArtifact"] = artifact
        preview = api.get(f"repos/{REPO}/releases/{config['preview_release_id']}")
        build_sha = receipt["origin"]["workflowCommit"]
        origin_manifest = assert_preview(preview, original_prov, config, build_sha)
        require(origin_manifest == {item["name"]: item for item in receipt["origin"]["assets"]}, "preview changed after preparation")
        require(stable_prov == {**original_prov, "workflowCommit": config["workflow_sha"], "releaseTag": config["version"]}, "stable provenance must change only promotion identity")
        expected = {item["name"]: item for item in receipt["assets"]}
        require(len(receipt["assets"]) == 16 and set(expected) == {*ASSET_NAMES, PROVENANCE_NAME}, "prepared artifact set differs")
        for name in ASSET_NAMES:
            require((expected[name]["size"], expected[name]["digest"]) == (origin_manifest[name]["size"], origin_manifest[name]["digest"]), "prepared payload differs from preview")
        run_path = f"repos/{REPO}/actions/runs/{config['preview_run_id']}"
        preview_run = api.get(run_path)
        require(preview_run["run_attempt"] == receipt["origin"]["runAttempt"], "preview attempt changed")
        assert_run(preview_run, api.pages(run_path + f"/attempts/{preview_run['run_attempt']}/jobs", "jobs"), config, build_sha)
        path = f"repos/{REPO}/releases/{receipt['newReleaseId']}"
        verify_new(api.get(path), receipt, published=False)
        require(asset_manifest(api.pages(path + "/assets"), expected) == expected, "prepared assets changed")
        preconditions(api, config, publication=True)
        recheck_source(config)
        result["status"] = "publication-requested"
        save()
        # The only publication mutation: never expose the stable tag as a draft.
        published = api.request("PATCH", path, {"tag_name": config["version"], "draft": False,
                               "prerelease": True, "make_latest": "false"})
        verify_new(published, receipt, published=True)
        verify_new(api.get(path), receipt, published=True)
        tag = api.get(f"repos/{REPO}/git/ref/tags/{config['version']}")
        require(tag["object"]["type"] == "commit" and tag["object"]["sha"] == config["workflow_sha"], "published tag drifted")
        require(asset_manifest(api.pages(path + "/assets"), expected) == expected, "published asset listing drifted")
        result["status"] = "PUBLISHED_IMMUTABLE_CANDIDATE"
        result["releaseId"] = receipt["newReleaseId"]
        save()
        return result
    except Exception:
        result["failedStage"] = result["status"]
        result["status"] = "failed"
        save()
        raise


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("prepare", help="CI: verify and upload an isolated draft only")
    publication = commands.add_parser("publish", help="Operator: verify successful CI receipt and publish once")
    publication.add_argument("--receipt", type=Path, required=True)
    publication.add_argument("--receipt-sha256", required=True)
    publication.add_argument("--source-directory", type=Path, required=True)
    publication.add_argument("--directory", type=Path, required=True, help="New private evidence directory")
    args = parser.parse_args()
    if args.operation == "publish":
        # Capture the existing local gh identity in memory. Never write a credential file.
        token = subprocess.check_output(["gh", "auth", "token"], stderr=subprocess.PIPE, text=True).strip()
        require(bool(token), "GitHub operator authentication unavailable")
        api = GitHub(token)

        def check(config):
            assert_tooling(config["workflow_sha"])
            private_tag(api, config["version"], config["source_sha"])
            _, changelog_sha = source_notes(args.source_directory, config)
            require(changelog_sha == json.loads(args.receipt.read_text())["changelogSha256"], "source changelog changed after preparation")

        result = publish(api, args.receipt, args.receipt_sha256, args.directory, check)
        print(f"Immutable acceptance candidate {result['releaseId']} published; distribution remains unapproved.")
        return
    config = {key: os.environ[key.upper()] for key in ("source_sha", "preview_tag", "preview_provenance_sha256", "version")}
    config.update(workflow_sha=os.environ["GITHUB_SHA"])
    for key, env in (("preview_run_id", "PREVIEW_RUN_ID"), ("preview_release_id", "PREVIEW_RELEASE_ID"), ("run_id", "GITHUB_RUN_ID"), ("run_attempt", "GITHUB_RUN_ATTEMPT")):
        raw = os.environ[env]
        require(re.fullmatch(r"[1-9]\d*", raw), f"invalid {env}")
        config[key] = int(raw)
    validate_inputs(config)
    assert_tooling(config["workflow_sha"])
    require(os.environ["GITHUB_REPOSITORY"] == REPO, "promotion repository differs")
    source_api = GitHub(os.environ["AALOOKUP_SOURCE_TOKEN"])
    recheck = lambda: private_tag(source_api, config["version"], config["source_sha"])
    recheck()
    notes, changelog_sha = source_notes(Path(os.environ["SOURCE_DIRECTORY"]), config)
    receipt = prepare(GitHub(os.environ["GH_TOKEN"]), config, Path(os.environ["PROMOTION_DIRECTORY"]), notes, changelog_sha, recheck)
    print(f"PREPARED isolated draft {receipt['newReleaseId']}; no stable candidate has been published.")


def assert_tooling(expected_sha):
    root = Path(__file__).resolve().parents[1]
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    require(head == expected_sha, "local promotion tooling must be the exact recorded Hub checkout")
    result = subprocess.run(["git", "-C", str(root), "diff", "--quiet", "HEAD", "--",
                             "scripts/promote_release.py", "scripts/release_provenance.py", ".github/workflows/promote-release.yml"], check=False)
    require(result.returncode == 0, "local promotion tooling has uncommitted changes")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Network exceptions can include signed URLs; never print their raw text.
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        raise SystemExit("Promotion failed: " + message)
