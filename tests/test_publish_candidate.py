"""Exercise exact-byte candidate publication with a stateful GitHub boundary."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publish_candidate as candidate


class FakeGitHub:
    def __init__(self):
        self.config = {"version": "v1.0.1", "source_sha": "a" * 40, "workflow_sha": "b" * 40}
        self.tag = None
        self.release = None
        self.mutations = []
        self.uploads = []
        self.immutable = True
        self.fail_upload = None
        self.fail_patch = False
        self.fail_read = False
        self.drift = None
        self.duplicate = False

    @staticmethod
    def asset(identifier, name, data):
        return {"id": identifier, "name": name, "size": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest(), "state": "uploaded"}

    def create_draft(self, notes="Reviewed notes"):
        self.tag = {"object": {"type": "commit", "sha": self.config["workflow_sha"]}}
        self.release = {"id": 42, "tag_name": self.config["version"], "target_commitish": self.config["workflow_sha"],
                        "body": candidate.release_body(notes, self.config), "draft": True, "prerelease": True,
                        "assets": [], "upload_url": f"https://uploads.github.com/repos/{candidate.REPO}/releases/42/assets{{?name,label}}"}
        return self.release

    def get(self, path):
        if self.fail_read:
            raise ValueError("API read failed")
        if "/git/ref/tags/" in path:
            return copy.deepcopy(self.tag)
        if path == candidate.RELEASES + "/42":
            result = copy.deepcopy(self.release)
            if self.drift == "fresh-immutable" and result["draft"] is False:
                result["immutable"] = False
            return result
        raise AssertionError(path)

    def pages(self, path):
        if self.fail_read:
            raise ValueError("API read failed")
        if path == candidate.RELEASES:
            return copy.deepcopy([self.release] * (2 if self.duplicate else 1) if self.release else [])
        if path == candidate.RELEASES + "/42/assets":
            return copy.deepcopy(self.release["assets"])
        raise AssertionError(path)

    def request(self, method, path, body):
        self.mutations.append((method, path, copy.deepcopy(body)))
        if method == "POST" and path.endswith("/git/refs"):
            self.tag = {"object": {"type": "commit", "sha": body["sha"]}}
            return copy.deepcopy(self.tag)
        if method == "POST" and path == candidate.RELEASES:
            self.create_draft()
            self.release.update(body)
            return copy.deepcopy(self.release)
        if method == "PATCH" and path == candidate.RELEASES + "/42":
            if self.fail_patch:
                raise ValueError("publication API failed")
            self.release.update(body, immutable=self.immutable)
            return copy.deepcopy(self.release)
        raise AssertionError((method, path))

    def upload(self, url, identifier, path):
        assert url == self.release["upload_url"] and identifier == 42
        self.uploads.append(path.name)
        if path.name == self.fail_upload:
            raise ValueError("upload API failed")
        result = self.asset(100 + len(self.release["assets"]), path.name, path.read_bytes())
        self.release["assets"].append(result)
        if len(self.release["assets"]) == 16:
            if self.drift == "source":
                self.release["body"] = self.release["body"].replace("a" * 40, "c" * 40)
            elif self.drift == "asset-id":
                self.release["assets"][0]["id"] = 9999
            elif self.drift == "tag":
                self.tag["object"]["sha"] = "c" * 40
        return copy.deepcopy(result)


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        for name in candidate.ASSET_NAMES:
            (self.directory / name).write_bytes(("actual fixture bytes: " + name).encode())
        self.api = FakeGitHub()

    def publish(self, **kwargs):
        return candidate.publish(self.api, self.api.config, self.directory, "Reviewed notes", **kwargs)

    def test_preflight_is_read_only_for_absent_target_and_matching_draft(self):
        self.assertIsNone(candidate.check(self.api, self.api.config))
        self.api.create_draft()
        self.assertEqual(candidate.check(self.api, self.api.config)["id"], 42)
        self.assertEqual(self.api.mutations, [])
        self.assertEqual(self.api.uploads, [])

    def test_published_wrong_owner_target_tag_or_ambiguous_draft_refuses(self):
        changes = [lambda api: api.release.update(draft=False),
                   lambda api: api.release.update(prerelease=False),
                   lambda api: api.release.update(target_commitish="c" * 40),
                   lambda api: api.release.update(body=api.release["body"].replace("a" * 40, "c" * 40)),
                   lambda api: api.release.update(body=api.release["body"].replace("b" * 40, "c" * 40)),
                   lambda api: api.tag["object"].update(sha="c" * 40),
                   lambda api: api.tag["object"].update(type="tag"),
                   lambda api: setattr(api, "tag", None),
                   lambda api: setattr(api, "duplicate", True)]
        for change in changes:
            with self.subTest(change=change):
                api = FakeGitHub(); api.create_draft(); change(api)
                with self.assertRaises(ValueError):
                    candidate.check(api, api.config)
                self.assertEqual(api.mutations, [])

    def test_publish_uses_numeric_uploads_exact_provenance_and_one_final_patch(self):
        result = self.publish()
        self.assertTrue(result["immutable"])
        self.assertFalse(result["draft"])
        self.assertTrue(result["prerelease"])
        self.assertEqual(result["make_latest"], "false")
        self.assertEqual(len(self.api.uploads), 16)
        patches = [item for item in self.api.mutations if item[0] == "PATCH"]
        self.assertEqual(patches, [("PATCH", candidate.RELEASES + "/42", {"draft": False, "prerelease": True, "make_latest": "false"})])
        data = json.loads((self.directory / candidate.PROVENANCE_NAME).read_text())
        self.assertEqual(data["sourceCommit"], self.api.config["source_sha"])
        self.assertEqual(data["workflowCommit"], self.api.config["workflow_sha"])
        self.assertEqual(len(data["assets"]), 15)
        for row in data["assets"]:
            self.assertEqual(row["sha256"], hashlib.sha256((self.directory / row["name"]).read_bytes()).hexdigest())

    def test_preview_requires_explicit_prerelease_mode(self):
        self.api.config["version"] = "v1.0.1-rc.1"
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.api.mutations, [])
        self.assertTrue(self.publish(prerelease=True)["immutable"])

    def test_partial_upload_can_resume_only_identical_bytes_and_local_provenance(self):
        self.api.fail_upload = sorted(candidate.ALL_NAMES)[3]
        with self.assertRaisesRegex(ValueError, "upload API"):
            self.publish()
        retained = copy.deepcopy(self.api.release["assets"])
        self.assertEqual(len(retained), 3)
        self.assertEqual(sum(item[0] == "PATCH" for item in self.api.mutations), 0)
        self.api.fail_upload = None
        self.api.uploads.clear()
        self.publish()
        self.assertEqual(self.api.release["assets"][:3], retained)
        self.assertEqual(len(self.api.uploads), 13)
        self.assertEqual(sum(item[0] == "POST" and item[1] == candidate.RELEASES for item in self.api.mutations), 1)

    def test_same_name_wrong_digest_size_or_state_refuses_before_any_upload(self):
        name = candidate.ASSET_NAMES[0]
        for changes in ({"digest": "sha256:" + "0" * 64}, {"size": 1}, {"state": "new"}):
            with self.subTest(changes=changes):
                self.api = FakeGitHub(); self.api.create_draft()
                asset = self.api.asset(90, name, (self.directory / name).read_bytes())
                self.api.release["assets"] = [{**asset, **changes}]
                with self.assertRaises(ValueError):
                    self.publish()
                self.assertEqual(self.api.uploads, [])
                self.assertEqual(self.api.mutations, [])

    def test_notes_or_retained_local_provenance_drift_refuses_without_mutation(self):
        self.api.create_draft("Other notes")
        with self.assertRaisesRegex(ValueError, "notes changed"):
            self.publish()
        self.assertEqual(self.api.mutations, [])
        self.api.create_draft()
        (self.directory / candidate.ASSET_NAMES[0]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "provenance differs"):
            self.publish()
        self.assertEqual(self.api.uploads, [])

    def test_invalid_local_set_never_creates_public_tag(self):
        (self.directory / "unreviewed.txt").write_text("not an artifact")
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.api.mutations, [])

    def test_identity_asset_id_and_tag_drift_prevent_publication(self):
        for drift in ("source", "asset-id", "tag"):
            with self.subTest(drift=drift):
                self.api = FakeGitHub(); self.api.drift = drift
                with self.assertRaises(ValueError):
                    self.publish()
                self.assertFalse(any(method == "PATCH" for method, _, _ in self.api.mutations))

    def test_read_and_publication_api_errors_are_not_retried(self):
        self.api.fail_read = True
        with self.assertRaisesRegex(ValueError, "read failed"):
            self.publish()
        self.assertEqual(self.api.mutations, [])
        self.api.fail_read = False; self.api.fail_patch = True
        with self.assertRaisesRegex(ValueError, "publication API"):
            self.publish()
        self.assertEqual(sum(method == "PATCH" for method, _, _ in self.api.mutations), 1)
        self.assertTrue(self.api.release["draft"])

    def test_response_and_fresh_read_must_both_confirm_immutability(self):
        for drift in ("response", "fresh-immutable"):
            with self.subTest(drift=drift):
                self.api = FakeGitHub()
                if drift == "response": self.api.immutable = False
                else: self.api.drift = drift
                with self.assertRaisesRegex(ValueError, "not immutable"):
                    self.publish()
                self.assertEqual(sum(method == "PATCH" for method, _, _ in self.api.mutations), 1)

    def test_invalid_inputs_or_duplicate_ownership_markers_refuse(self):
        for version in ("1.0.1", "v1.0.1-01", "v1.0.1-rc..1", "v1.0.1+build", "v1.0.1\n"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                candidate.check(self.api, {**self.api.config, "version": version})
        for key in ("source_sha", "workflow_sha"):
            with self.assertRaises(ValueError):
                candidate.check(self.api, {**self.api.config, key: "HEAD"})
        body = candidate.release_body("notes", self.api.config)
        with self.assertRaises(ValueError):
            candidate.release_body(body + candidate.markers(self.api.config)[0], self.api.config)
        self.assertEqual(self.api.mutations, [])


if __name__ == "__main__":
    unittest.main()
