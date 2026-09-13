"""Exercise promotion with actual fixture bytes and a stateful GitHub boundary."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from urllib.error import HTTPError
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("promote_release", ROOT / "scripts/promote_release.py")
promotion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(promotion)


def digest(content):
    return hashlib.sha256(content).hexdigest()


class FakeGitHub:
    def __init__(self):
        self.config = {"source_sha": "a" * 40, "workflow_sha": "c" * 40,
                       "preview_tag": "v1.0.0-rc.1", "version": "v1.0.0",
                       "preview_run_id": 10, "preview_release_id": 20,
                       "run_id": 30, "run_attempt": 1}
        self.bytes = {name: ("signed final bytes: " + name).encode() for name in promotion.ASSET_NAMES}
        self.provenance = {"schemaVersion": 1, "sourceCommit": "a" * 40,
                           "workflowCommit": "b" * 40, "releaseTag": "v1.0.0-rc.1",
                           "assets": [{"name": name, "sha256": digest(content), "size": len(content)}
                                      for name, content in sorted(self.bytes.items())]}
        content = json.dumps(self.provenance).encode()
        self.bytes[promotion.PROVENANCE_NAME] = content
        self.config["preview_provenance_sha256"] = digest(content)
        self.run = {"id": 10, "head_sha": "b" * 40, "run_attempt": 1,
                    "repository": {"full_name": promotion.REPO}, "head_repository": {"full_name": promotion.REPO},
                    "path": ".github/workflows/pre-release.yml", "event": "workflow_dispatch",
                    "display_title": "Preview v1.0.0-rc.1 from " + "a" * 40,
                    "status": "completed", "conclusion": "success"}
        self.jobs = [{"id": index, "run_id": 10, "head_sha": "b" * 40, "name": name,
                      "status": "completed", "conclusion": "success",
                      "steps": [{"name": step, "status": "completed", "conclusion": "success"} for step in steps]}
                     for index, (name, steps) in enumerate(promotion.JOB_STEPS.items(), 100)]
        self.preview = {"id": 20, "draft": False, "prerelease": True, "tag_name": "v1.0.0-rc.1",
                        "target_commitish": "b" * 40,
                        "body": "Private source: `lonelam/aalookup@" + "a" * 40 + "`.\nPublic workflow commit: `" + "b" * 40 + "`.",
                        "assets": [self.asset(index, name, value) for index, (name, value) in enumerate(self.bytes.items(), 200)]}
        self.enabled = True
        self.existing_stable = False
        self.draft = None
        self.mutations = []
        self.uploads = []
        self.drift_draft = False
        self.publish_immutable = True
        self.publish_conflict = False
        self.corrupt_download = False
        self.stable_after_upload = False
        self.fresh_run_attempt = False
        self.run_reads = 0

    @staticmethod
    def asset(identifier, name, value):
        return {"id": identifier, "name": name, "size": len(value), "digest": "sha256:" + digest(value), "state": "uploaded"}

    def get(self, path):
        if path.endswith("/immutable-releases"):
            return {"enabled": self.enabled}
        if "/git/ref/tags/" in path:
            if path.endswith("/v1.0.0"):
                return {"object": {"type": "commit", "sha": "c" * 40,
                                   "url": "https://api.github.com/repos/" + promotion.REPO + "/git/commits/" + "c" * 40}}
            return None
        if path.endswith("/actions/runs/10"):
            self.run_reads += 1
            value = copy.deepcopy(self.run)
            if self.fresh_run_attempt and self.run_reads > 1:
                value["run_attempt"] = 2
            return value
        if path.endswith("/releases/20"):
            return copy.deepcopy(self.preview)
        if path.endswith("/releases/40"):
            value = copy.deepcopy(self.draft)
            if self.drift_draft and value["assets"]:
                value["assets"][0]["digest"] = "sha256:" + "0" * 64
            return value
        raise AssertionError("unexpected read " + path)

    def pages(self, path, field=None):
        if path.endswith("/attempts/1/jobs") and field == "jobs":
            return copy.deepcopy(self.jobs)
        if path.endswith("/releases"):
            if self.existing_stable or (self.stable_after_upload and self.uploads):
                return [{"id": 999, "tag_name": "v1.0.0"}]
            return [copy.deepcopy(self.draft)] if self.draft else []
        if path.endswith("/releases/40/assets"):
            return copy.deepcopy(self.draft["assets"])
        raise AssertionError("unexpected listing " + path)

    def download(self, asset, destination):
        content = self.bytes[asset["name"]]
        if self.corrupt_download:
            raise ValueError("downloaded asset digest or size differs")
        destination.write_bytes(content)

    def request(self, method, path, body):
        self.mutations.append((method, path, copy.deepcopy(body)))
        if method == "POST" and path.endswith("/releases"):
            self.draft = {**body, "id": 40, "assets": [],
                          "upload_url": "https://uploads.github.com/repos/" + promotion.REPO + "/releases/40/assets{?name,label}"}
            return copy.deepcopy(self.draft)
        if method == "PATCH" and path.endswith("/releases/40"):
            if self.publish_conflict:
                raise ValueError("publication conflict")
            self.draft.update(body, immutable=self.publish_immutable)
            return copy.deepcopy(self.draft)
        raise AssertionError("unexpected mutation " + method + " " + path)

    def upload(self, url, release_id, path):
        self.uploads.append((url, release_id, path.name, path.read_bytes()))
        value = self.asset(1000 + len(self.uploads), path.name, path.read_bytes())
        self.draft["assets"].append(value)
        return copy.deepcopy(value)


class PromotionTests(unittest.TestCase):
    def run_promotion(self, api):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "promotion"
        return promotion.prepare(api, api.config, self.directory, "Reviewed release notes", "d" * 64)

    def publish_prepared(self, api):
        receipt = self.directory / "promotion-receipt.json"
        stable = json.loads((self.directory / "assets" / promotion.PROVENANCE_NAME).read_text())
        return promotion.publish(api, receipt, promotion.sha256(receipt), self.directory / "publication",
                                 lambda config: None,
                                 origin_verifier=lambda *_: (api.provenance, stable, {"id": 99}))

    def test_promotes_exact_signed_bytes_using_new_numeric_id_and_one_publish_patch(self):
        api = FakeGitHub()
        result = self.run_promotion(api)
        self.assertEqual(result["status"], "PREPARED")
        self.assertEqual([item[0] for item in api.mutations], ["POST"])
        published = self.publish_prepared(api)
        self.assertEqual(published["status"], "PUBLISHED_IMMUTABLE_CANDIDATE")
        self.assertFalse(result["approvedForDistribution"])
        self.assertEqual(result["origin"]["workflowCommit"], "b" * 40)
        self.assertEqual(result["promotionWorkflowCommit"], "c" * 40)
        self.assertEqual(len(api.uploads), 16)
        for url, release_id, name, content in api.uploads:
            self.assertEqual(release_id, 40)
            self.assertIn("/releases/40/assets", url)
            if name != promotion.PROVENANCE_NAME:
                self.assertEqual(content, api.bytes[name])
        stable_prov = json.loads((self.directory / "assets" / promotion.PROVENANCE_NAME).read_text())
        self.assertEqual(stable_prov["releaseTag"], "v1.0.0")
        self.assertEqual(stable_prov["workflowCommit"], "c" * 40)
        self.assertEqual(stable_prov["assets"], api.provenance["assets"])
        self.assertEqual(len(api.mutations), 2)
        self.assertEqual(api.mutations[0][0], "POST")
        self.assertEqual(api.mutations[0][2]["tag_name"], "v1.0.0-promotion.30.1")
        self.assertTrue(api.mutations[0][2]["draft"])
        self.assertEqual(api.mutations[1], ("PATCH", "repos/" + promotion.REPO + "/releases/40",
                         {"tag_name": "v1.0.0", "draft": False, "prerelease": True, "make_latest": "false"}))

    def test_source_provenance_draft_asset_and_run_drift_refuse_before_mutation(self):
        changes = [
            lambda api: api.config.update(source_sha="e" * 40),
            lambda api: api.config.update(preview_provenance_sha256="0" * 64),
            lambda api: api.preview.update(draft=True),
            lambda api: api.preview["assets"][0].update(digest="sha256:" + "f" * 64),
            lambda api: api.preview["assets"].pop(),
            lambda api: api.run.update(conclusion="failure"),
            lambda api: api.jobs.pop(),
            lambda api: api.jobs[1]["steps"].pop(),
            lambda api: api.run.update(head_sha="e" * 40),
            lambda api: api.preview.update(body="different source"),
            lambda api: setattr(api, "fresh_run_attempt", True),
        ]
        for change in changes:
            api = FakeGitHub()
            change(api)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.run_promotion(api)
            self.assertEqual(api.mutations, [])

    def test_existing_stable_refuses_preparation_before_download_or_write(self):
        api = FakeGitHub()
        api.existing_stable = True
        with self.assertRaises(ValueError):
            self.run_promotion(api)
        self.assertEqual(api.mutations, [])
        self.assertFalse((self.directory / "preview-provenance.json").exists())

    def test_ci_needs_no_administration_read_and_operator_refuses_disabled_immutability(self):
        api = FakeGitHub()
        api.enabled = False
        result = self.run_promotion(api)
        self.assertEqual(result["status"], "PREPARED")
        with self.assertRaisesRegex(ValueError, "immutable releases"):
            self.publish_prepared(api)
        self.assertEqual([item[0] for item in api.mutations], ["POST"])

    def test_asset_download_failure_cannot_create_draft(self):
        api = FakeGitHub()
        api.corrupt_download = True
        with self.assertRaisesRegex(ValueError, "digest"):
            self.run_promotion(api)
        self.assertEqual(api.mutations, [])

    def test_draft_digest_drift_or_late_stable_race_cannot_publish(self):
        for flag in ("drift_draft", "stable_after_upload"):
            api = FakeGitHub()
            setattr(api, flag, True)
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                self.run_promotion(api)
            self.assertEqual([item[0] for item in api.mutations], ["POST"])
            self.assertTrue(api.draft["draft"])

    def test_failed_or_nonimmutable_publication_is_not_retried_or_cleaned_up(self):
        for flag in ("publish_conflict", "publish_immutable"):
            api = FakeGitHub()
            setattr(api, flag, flag == "publish_conflict")
            self.run_promotion(api)
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                self.publish_prepared(api)
            self.assertEqual([item[0] for item in api.mutations], ["POST", "PATCH"])
            receipt = json.loads((self.directory / "publication/publication-receipt.json").read_text())
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["preparedReceipt"]["newReleaseId"], 40)
            self.assertFalse(receipt["approvedForDistribution"])

    def test_unbound_inputs_refuse_before_network(self):
        for key, value in (("preview_run_id", None), ("run_attempt", True), ("source_sha", "HEAD"),
                           ("version", "v1.0.0-rc.1"), ("preview_tag", "v1.0.1-rc.1")):
            api = FakeGitHub()
            api.config[key] = value
            with self.subTest(key=key), self.assertRaises((ValueError, TypeError)):
                self.run_promotion(api)
            self.assertEqual(api.mutations, [])

    def test_numeric_upload_endpoint_rejects_foreign_host_or_old_release_id(self):
        api = promotion.GitHub("not-a-real-token")
        for url in ("https://uploads.github.com/repos/lonelam/aalookup-hub/releases/20/assets{?name,label}",
                    "https://untrusted.example/releases/40/assets"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "numeric release ID"):
                api.upload(url, 40, Path("never-opened"))

    def test_real_upload_transport_streams_original_bytes_to_numeric_id_without_redirect(self):
        api = promotion.GitHub("test-token")
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / "signed.bin"
            file.write_bytes(b"signed executable bytes")
            response = io.BytesIO(json.dumps(FakeGitHub.asset(1, file.name, file.read_bytes())).encode())
            response.status = 201

            def opened(request, timeout):
                self.assertEqual(request.full_url, "https://uploads.github.com/repos/lonelam/aalookup-hub/releases/40/assets?name=signed.bin")
                self.assertEqual(request.method, "POST")
                self.assertEqual(request.get_header("Content-length"), str(file.stat().st_size))
                self.assertEqual(request.data.read(), file.read_bytes())
                self.assertEqual(timeout, 180)
                return response

            with patch.object(api.opener, "open", side_effect=opened):
                result = api.upload("https://uploads.github.com/repos/lonelam/aalookup-hub/releases/40/assets{?name,label}", 40, file)
            self.assertEqual(result["digest"], "sha256:" + promotion.sha256(file))

    def test_download_strips_token_on_signed_redirect_and_verifies_actual_digest(self):
        api = promotion.GitHub("test-token")
        content = b"signed artifact bytes"
        asset = FakeGitHub.asset(7, "signed.bin", content)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "signed.bin"
            response = io.BytesIO(content)
            response.status = 200
            calls = []

            def opened(request, timeout):
                calls.append(request)
                if len(calls) == 1:
                    self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
                    self.assertEqual(request.get_header("Accept"), "application/octet-stream")
                    raise HTTPError(request.full_url, 302, "redirect", {"Location": "https://release-assets.githubusercontent.com/signed?secret=ephemeral"}, None)
                self.assertIsNone(request.get_header("Authorization"))
                return response

            with patch.object(api.opener, "open", side_effect=opened):
                api.download(asset, output)
            self.assertEqual(output.read_bytes(), content)
            bad = io.BytesIO(content + b"extra")
            bad.status = 200
            with patch.object(api.opener, "open", return_value=bad), self.assertRaisesRegex(ValueError, "limit"):
                api.download(asset, Path(temporary) / "bad.bin")

    def test_actions_artifact_download_accepts_json_and_strips_token_on_signed_redirect(self):
        api = promotion.GitHub("test-token")
        content = b"receipt archive bytes"
        path = f"repos/{promotion.REPO}/actions/artifacts/99/zip"
        signed_url = "https://production.blob.core.windows.net/receipt.zip?secret=ephemeral"
        response = io.BytesIO(content)
        response.status = 200
        calls = []

        def opened(request, timeout):
            calls.append(request)
            self.assertEqual(timeout, 60)
            if len(calls) == 1:
                self.assertEqual(request.full_url, "https://api.github.com/" + path)
                self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
                self.assertEqual(request.get_header("Accept"), "application/json")
                self.assertEqual(request.get_header("X-github-api-version"), "2026-03-10")
                raise HTTPError(request.full_url, 302, "redirect", {"Location": signed_url}, None)
            self.assertEqual(request.full_url, signed_url)
            self.assertEqual(request.header_items(), [])
            return response

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "receipt.zip"
            with patch.object(api.opener, "open", side_effect=opened):
                api.download_file(path, output, len(content), "sha256:" + digest(content), artifact=True)
            self.assertEqual(len(calls), 2)
            self.assertEqual(output.read_bytes(), content)

    def test_publish_revalidates_prepared_asset_ids_and_pinned_receipt(self):
        api = FakeGitHub()
        self.run_promotion(api)
        api.draft["assets"][0]["id"] = 99999
        with self.assertRaisesRegex(ValueError, "drifted"):
            self.publish_prepared(api)
        self.assertEqual([item[0] for item in api.mutations], ["POST"])
        with self.assertRaisesRegex(ValueError, "receipt SHA differs"):
            promotion.publish(api, self.directory / "promotion-receipt.json", "0" * 64,
                              self.directory / "another-publication", lambda config: None)

    def test_receipt_must_match_the_successful_ci_artifact_bytes(self):
        api = FakeGitHub()
        receipt = self.run_promotion(api)
        original = (self.directory / "promotion-receipt.json").read_bytes()
        archive = self.directory / "fixture.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("promotion-receipt.json", original)
            zipped.writestr("preview-provenance.json", json.dumps(api.provenance).encode())
            zipped.writestr("assets/release-provenance.json", (self.directory / "assets/release-provenance.json").read_bytes())
        artifact = {"id": 99, "name": "promotion-receipt-30-1", "expired": False,
                    "size_in_bytes": archive.stat().st_size, "digest": "sha256:" + promotion.sha256(archive),
                    "workflow_run": {"id": 30, "head_sha": "c" * 40}}
        run = {**api.run, "id": 30, "head_sha": "c" * 40, "path": ".github/workflows/promote-release.yml",
               "display_title": "Promote v1.0.0-rc.1 to v1.0.0 from " + "a" * 40}
        job = {"id": 888, "run_id": 30, "name": "Prepare verified preview bytes", "status": "completed", "conclusion": "success", "head_sha": "c" * 40,
               "steps": [{"name": name, "status": "completed", "conclusion": "success"} for name in ("Copy verified bytes into isolated draft", "Preserve promotion receipt")]}

        def pages(path, field):
            return [job] if field == "jobs" else [artifact]

        def download(path, target, size, expected_digest, **kwargs):
            self.assertIn("/actions/artifacts/99/zip", path)
            self.assertTrue(kwargs["artifact"])
            self.assertEqual((size, expected_digest), (artifact["size_in_bytes"], artifact["digest"]))
            target.write_bytes(archive.read_bytes())

        with patch.object(api, "get", return_value=run), patch.object(api, "pages", side_effect=pages), \
                patch.object(api, "download_file", side_effect=download, create=True):
            folder = self.directory / "verify-receipt"
            folder.mkdir()
            values = promotion.verify_receipt_origin(api, receipt, original, folder)
            self.assertEqual(values[0], api.provenance)
            with self.assertRaisesRegex(ValueError, "does not match"):
                promotion.verify_receipt_origin(api, receipt, original + b"\n", folder)
            run["conclusion"] = "failure"
            with self.assertRaisesRegex(ValueError, "successful recorded attempt"):
                promotion.verify_receipt_origin(api, receipt, original, folder)

    def test_workflow_has_no_build_sign_or_distribution_credentials(self):
        workflow = (ROOT / ".github/workflows/promote-release.yml").read_text()
        self.assertIn("group: release-${{ inputs.version }}", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("actions: read", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("ref: ${{ github.sha }}", workflow)
        self.assertIn("ref: ${{ inputs.source_sha }}", workflow)
        self.assertEqual(workflow.count("persist-credentials: false"), 2)
        for forbidden in ("cargo ", "npm ", "signer sign", "TAURI_SIGNING", "ASC_API", "DEPLOY_", "gh release upload"):
            self.assertNotIn(forbidden, workflow)

    def test_release_notes_preserve_platform_restrictions_and_exclude_internal_entries(self):
        api = FakeGitHub()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src-tauri").mkdir()
            (root / "changelog").mkdir()
            for path in (root / "package.json", root / "src-tauri/tauri.conf.json"):
                path.write_text(json.dumps({"version": "1.0.0"}))
            (root / "src-tauri/Cargo.toml").write_text('[package]\nversion = "1.0.0"\n')
            entry = {"version": "1.0.0", "headline": {"en": "Release", "zh-CN": "发布"},
                     "changes": [{"platforms": ["macos"], "summary": {"en": "Experimental screen OCR.", "zh-CN": "实验性屏幕OCR。"}},
                                 {"summary": {"en": "All-platform change.", "zh-CN": "全平台改进。"}}],
                     "internal": [{"summary": "Private operational details"}]}
            path = root / "changelog/1.0.0.json"
            path.write_text(json.dumps(entry))
            with patch.object(promotion.subprocess, "check_output", return_value="a" * 40 + "\n"):
                notes, actual_sha = promotion.source_notes(root, api.config)
                self.assertIn("- **macOS** — Experimental screen OCR.", notes)
                self.assertIn("- **macOS** — 实验性屏幕OCR。", notes)
                self.assertIn("- All-platform change.", notes)
                self.assertNotIn("Private operational details", notes)
                self.assertEqual(actual_sha, promotion.sha256(path))
                entry["changes"][0]["platforms"] = ["unknown"]
                path.write_text(json.dumps(entry))
                with self.assertRaisesRegex(ValueError, "platform restriction"):
                    promotion.source_notes(root, api.config)


if __name__ == "__main__":
    unittest.main()
