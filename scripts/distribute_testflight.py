#!/usr/bin/env python3
"""Distribute one exact uploaded iOS build to AALookup's existing public group."""

import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

APP_ID = "6803544073"
BUNDLE_ID = "com.aalookup.app"
GROUP_ID = "de5fef9d-5f92-4921-88f0-07ea4884ae5e"
PUBLIC_LINK = "https://testflight.apple.com/join/ckB7WYFu"
ORIGIN = "https://api.appstoreconnect.apple.com"
PENDING = {"WAITING_FOR_BETA_REVIEW", "IN_BETA_REVIEW"}
READY = {"READY_FOR_BETA_SUBMISSION", "READY_FOR_BETA_TESTING", "BETA_APPROVED", "IN_BETA_TESTING"} | PENDING
NOTES = """请测试 iPhone/iPad 启动、前后台切换、查词和词典图片显示。
全新安装并联网：直接使用 → 查询 apple。应用会尝试自动准备 AALookup Learner’s English。
覆盖安装或没有结果：欢迎页出现时先点“直接使用”，然后进入 Settings → Dictionaries → Search & download → AALookup Learner’s English，下载并启用，再查询 apple。

Please test iPhone/iPad launch, returning from the background, word lookup and dictionary images.
Fresh install with internet: Start now → look up apple. The app attempts to prepare AALookup Learner’s English automatically.
Existing install or no result: tap Start now if the welcome screen appears, then Settings → Dictionaries → Search & download → AALookup Learner’s English; download and enable it, then look up apple.
"""


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def jwt_signature(der):
    # OpenSSL returns ASN.1 integers; ES256 JWTs require two fixed 32-byte integers.
    require(len(der) >= 8 and der[0] == 0x30 and der[1] == len(der) - 2, "Invalid ES256 signature")
    parts, offset = [], 2
    for _ in range(2):
        require(offset + 2 <= len(der) and der[offset] == 2, "Invalid ES256 integer")
        size = der[offset + 1]
        value = der[offset + 2:offset + 2 + size]
        require(1 <= size <= 33 and len(value) == size and value[0] < 128, "Invalid ES256 integer size")
        number = int.from_bytes(value, "big")
        require(0 < number < 2**256, "Invalid ES256 integer value")
        parts.append(number.to_bytes(32, "big"))
        offset += size + 2
    require(offset == len(der), "Invalid ES256 trailing bytes")
    return b"".join(parts)


def token(key_path, key_id, issuer):
    now = int(time.time())
    header = b64(json.dumps({"alg": "ES256", "kid": key_id, "typ": "JWT"}).encode())
    claims = b64(json.dumps({"iss": issuer, "iat": now - 5, "exp": now + 600, "aud": "appstoreconnect-v1"}).encode())
    message = f"{header}.{claims}"
    signed = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key_path)],
                            input=message.encode(), capture_output=True, check=True).stdout
    return f"{message}.{b64(jwt_signature(signed))}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("App Store Connect unexpectedly redirected a request")


class Client:
    def __init__(self, key_path, key_id, issuer):
        self.credentials = (key_path, key_id, issuer)
        self.opener = urllib.request.build_opener(NoRedirect)

    def call(self, method, path, body=None, query=None):
        require(path.startswith("/v1/") and "?" not in path and "#" not in path, "Invalid API path")
        url = ORIGIN + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + token(*self.credentials),
                     "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=45) as response:
                raw = response.read(2_000_001)
            require(len(raw) <= 2_000_000, "Unexpectedly large API response")
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            # Do not log remote bodies, review credentials, or authorization headers.
            raise RuntimeError(f"App Store Connect {method} {path}: HTTP {error.code}; inspect in App Store Connect before rerunning") from None

    def rows(self, path, query=None):
        response = self.call("GET", path, query={"limit": "200", **(query or {})})
        require(not response.get("links", {}).get("next"), "Unexpected pagination; inspect exact build/group manually")
        return response["data"]


def relationship(resource, name):
    return resource["relationships"][name]["data"]["id"]


def validate_group(client):
    app = client.call("GET", f"/v1/apps/{APP_ID}")["data"]
    require(app["attributes"]["bundleId"] == BUNDLE_ID, "App bundle identity mismatch")
    group = client.call("GET", f"/v1/betaGroups/{GROUP_ID}")["data"]
    owner = client.call("GET", f"/v1/betaGroups/{GROUP_ID}/app")["data"]
    attrs = group["attributes"]
    require(owner["id"] == APP_ID and group["id"] == GROUP_ID, "Public group identity mismatch")
    require(attrs.get("isInternalGroup") is False and attrs.get("publicLinkEnabled") is True
            and attrs.get("publicLink") == PUBLIC_LINK, "Existing public TestFlight link is not enabled; inspect manually")


def find_build(client, version, number):
    versions = client.rows("/v1/preReleaseVersions", {"filter[app]": APP_ID,
                           "filter[version]": version, "filter[platform]": "IOS"})
    require(len(versions) <= 1, "Ambiguous app version")
    if not versions:
        return None
    train = versions[0]
    require(train["attributes"]["version"] == version and train["attributes"]["platform"] == "IOS", "Version identity mismatch")
    builds = client.rows("/v1/builds", {"filter[app]": APP_ID, "filter[preReleaseVersion]": train["id"],
                        "filter[version]": number, "include": "app,preReleaseVersion"})
    require(len(builds) <= 1, "Ambiguous exact build")
    if not builds:
        return None
    build = builds[0]
    require(build["attributes"]["version"] == number and relationship(build, "app") == APP_ID
            and relationship(build, "preReleaseVersion") == train["id"], "Build identity mismatch")
    require(not build["attributes"].get("expired"), "Build is expired")
    require(build["attributes"].get("buildAudienceType") != "INTERNAL_ONLY", "Build is internal-only")
    require(build["attributes"]["processingState"] not in {"FAILED", "INVALID"}, "Build processing failed")
    return build


def detail(client, build_id):
    return client.call("GET", f"/v1/builds/{build_id}/buildBetaDetail")["data"]


def build_body(resource_type, build_id, attributes=None):
    data = {"type": resource_type, "relationships": {"build": {"data": {"type": "builds", "id": build_id}}}}
    if attributes is not None:
        data["attributes"] = attributes
    return {"data": data}


def distribute(client, version, number, timeout=1200, sleep=time.sleep, clock=time.monotonic):
    require(re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version), "Expected exact marketing version")
    require(re.fullmatch(r"[1-9]\d*", number), "Expected exact positive build number")
    validate_group(client)
    deadline = clock() + timeout
    while True:
        build = find_build(client, version, number)
        if build and build["attributes"]["processingState"] == "VALID":
            beta = detail(client, build["id"])
            state = beta["attributes"].get("externalBuildState")
            if state != "PROCESSING":
                break
        require(clock() < deadline, "Uploaded build is not ready; rerun this distribution workflow later without rebuilding")
        print(f"Waiting for Apple processing of {version} ({number})", flush=True)
        sleep(min(30, max(0, deadline - clock())))
    require(state in READY, f"External testing needs operator attention: {state}")
    build_id = build["id"]
    if state == "READY_FOR_BETA_SUBMISSION":
        localizations = client.rows(f"/v1/builds/{build_id}/betaBuildLocalizations")
        if not any(row["attributes"].get("whatsNew", "").strip() for row in localizations):
            existing = next((row for row in localizations if row["attributes"].get("locale") == "zh-Hans"), None)
            if existing:
                client.call("PATCH", f"/v1/betaBuildLocalizations/{existing['id']}", {"data": {
                    "type": "betaBuildLocalizations", "id": existing["id"], "attributes": {"whatsNew": NOTES}}})
            else:
                client.call("POST", "/v1/betaBuildLocalizations", build_body("betaBuildLocalizations", build_id,
                            {"locale": "zh-Hans", "whatsNew": NOTES}))
    if beta["attributes"].get("autoNotifyEnabled") is not True:
        client.call("PATCH", f"/v1/buildBetaDetails/{beta['id']}", {"data": {
            "type": "buildBetaDetails", "id": beta["id"], "attributes": {"autoNotifyEnabled": True}}})
    groups = client.rows(f"/v1/builds/{build_id}/betaGroups")
    if not any(row["id"] == GROUP_ID for row in groups):
        client.call("POST", f"/v1/betaGroups/{GROUP_ID}/relationships/builds",
                    {"data": [{"type": "builds", "id": build_id}]})
    # Adding a group may itself advance review state. Re-read before submitting.
    state = detail(client, build_id)["attributes"]["externalBuildState"]
    if state == "READY_FOR_BETA_SUBMISSION":
        client.call("POST", "/v1/betaAppReviewSubmissions", build_body("betaAppReviewSubmissions", build_id))
    elif state in {"READY_FOR_BETA_TESTING", "BETA_APPROVED"}:
        client.call("POST", "/v1/buildBetaNotifications", build_body("buildBetaNotifications", build_id))
    else:
        require(state in PENDING | {"IN_BETA_TESTING"}, f"Unexpected external testing state: {state}")
    # Only bounded GET retries absorb Apple's read-after-write delay; mutations are never retried.
    for attempt in range(7):
        final = detail(client, build_id)["attributes"]
        groups = client.rows(f"/v1/builds/{build_id}/betaGroups")
        state = final.get("externalBuildState")
        if (state in PENDING | {"IN_BETA_TESTING"} and final.get("autoNotifyEnabled") is True
                and any(row["id"] == GROUP_ID for row in groups)):
            return {"appId": APP_ID, "version": version, "buildNumber": number, "buildId": build_id,
                    "groupId": GROUP_ID, "publicLink": PUBLIC_LINK, "externalBuildState": state,
                    "autoNotifyEnabled": True, "availableToPublicTesters": state == "IN_BETA_TESTING"}
        require(attempt < 6, "Distribution has not settled; inspect App Store Connect before rerunning")
        sleep(10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build-number", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    key_id, issuer, key = (os.environ.get(name, "").strip() for name in
                           ("ASC_API_KEY_ID", "ASC_API_ISSUER_ID", "ASC_API_KEY"))
    require(key_id and issuer and key, "Existing ASC_API_KEY_ID, ASC_API_ISSUER_ID and ASC_API_KEY are required")
    with tempfile.TemporaryDirectory(prefix="aalookup-asc-") as directory:
        key_path = Path(directory) / "key.p8"
        key_path.touch(mode=0o600)
        key_path.write_text(key)
        result = distribute(Client(key_path, key_id, issuer), args.version.removeprefix("v"), args.build_number)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as output:
            output.write(f"## Public TestFlight\n\n{result['version']} ({result['buildNumber']}): **{result['externalBuildState']}**.\n\n"
                         f"[Public TestFlight]({PUBLIC_LINK}). Apple approval is required before a waiting build is available.\n")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"TestFlight distribution incomplete: {error}") from None
