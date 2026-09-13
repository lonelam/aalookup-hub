#!/usr/bin/env python3
"""Gate only a website rollout explicitly associated with a client release."""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def assert_approved_manifest(manifest: object, release_tag: str, source_sha: str) -> None:
    if not re.fullmatch(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", release_tag):
        raise ValueError("client release must be a stable v-prefixed version")
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("client release source must be a full lowercase commit SHA")
    if not isinstance(manifest, dict):
        raise ValueError("approved release manifest is not an object")
    if manifest.get("version") not in (release_tag, release_tag[1:]) or manifest.get("sourceCommit") != source_sha:
        raise ValueError("this client version and exact source are not the currently approved public release")


def check_approved_release(origin: str, release_tag: str, source_sha: str) -> None:
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("DEPLOY_URL must be an HTTPS origin without credentials, path, query or fragment")
    request = Request(origin.rstrip("/") + "/api/v1/releases/latest", headers={"Accept": "application/json", "Cache-Control": "no-cache"})
    with urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise ValueError("approved release manifest is unavailable")
        payload = response.read(1024 * 1024 + 1)
    if len(payload) > 1024 * 1024:
        raise ValueError("approved release manifest is too large")
    assert_approved_manifest(json.loads(payload), release_tag, source_sha)


if __name__ == "__main__":
    check_approved_release(os.environ["DEPLOY_URL"], os.environ["CLIENT_RELEASE_TAG"], os.environ["SOURCE_SHA"])
    print("Client-associated website rollout matches the approved version and exact source.")
