"""Verify the public candidate boundary and hash the actual staged test bytes."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_provenance", ROOT / "scripts" / "release_provenance.py")
provenance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provenance)


class ReleaseProvenance(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in provenance.ASSET_NAMES:
            (self.root / name).write_bytes(f"signed-fixture:{name}".encode())

    def create(self, **changes):
        args = {"source_commit": "a" * 40, "workflow_commit": "b" * 40, "release_tag": "v0.7.8", **changes}
        return provenance.write_provenance(self.root, **args)

    def test_provenance_binds_source_workflow_tag_and_exact_artifact_bytes(self):
        result = json.loads(self.create().read_text())
        self.assertEqual(set(result), {"schemaVersion", "sourceCommit", "workflowCommit", "releaseTag", "assets"})
        self.assertEqual(result["schemaVersion"], 1)
        self.assertEqual(result["sourceCommit"], "a" * 40)
        self.assertEqual(result["workflowCommit"], "b" * 40)
        self.assertEqual(result["releaseTag"], "v0.7.8")
        self.assertEqual([item["name"] for item in result["assets"]], sorted(provenance.ASSET_NAMES))
        for item in result["assets"]:
            content = (self.root / item["name"]).read_bytes()
            self.assertEqual(item["size"], len(content))
            self.assertEqual(item["sha256"], hashlib.sha256(content).hexdigest())
        self.assertNotIn(provenance.PROVENANCE_NAME, [item["name"] for item in result["assets"]])
        with self.assertRaises(ValueError):
            self.create()

    def test_wrong_identity_or_unexpected_file_cannot_enter_provenance(self):
        for changed in ({"source_commit": "HEAD"}, {"workflow_commit": "A" * 40}, {"release_tag": "v0.7.8-rc.1"}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.create(**changed)
        (self.root / "private-source.tar.gz").write_bytes(b"must never publish")
        with self.assertRaisesRegex(ValueError, "exactly"):
            self.create()
        self.assertFalse((self.root / provenance.PROVENANCE_NAME).exists())

    def test_missing_empty_or_linked_assets_are_rejected(self):
        name = provenance.ASSET_NAMES[0]
        path = self.root / name
        for kind in ("missing", "empty", "symlink", "directory"):
            path.unlink(missing_ok=True)
            if kind == "empty":
                path.touch()
            elif kind == "symlink":
                path.symlink_to(self.root / provenance.ASSET_NAMES[1])
            elif kind == "directory":
                path.mkdir()
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.create()
        path.rmdir()


class CandidateWorkflow(unittest.TestCase):
    def test_publish_requires_all_platforms_and_keeps_candidate_out_of_latest(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text()
        job = workflow.split("\n  release:\n", 1)[1]
        needs = re.search(r"(?ms)^    needs:\n(.*?)^    if:", job).group(1)
        self.assertEqual(re.findall(r"- (\w+)", needs), ["resolve", "macos", "windows", "android", "ios"])
        self.assertNotIn("always()", job)
        self.assertNotIn("make_latest=true", workflow)
        self.assertNotIn("releases/refresh", workflow)
        self.assertNotIn("AALOOKUP_RELEASE_REFRESH_TOKEN", workflow)
        self.assertNotIn("\n  refresh:", workflow)
        self.assertIn("-F draft=false \\\n            -F prerelease=true \\\n            --raw-field make_latest=false", job)
        self.assertLess(job.index("release_provenance.py"), job.index('gh release upload "$VERSION"'))
        self.assertIn("expected+=(release-provenance.json)", job)
        self.assertIn("Private source:", job)
        self.assertIn("Public workflow commit:", job)
        self.assertIn('"$actual_manifest" != "$expected_manifest"', job)
        self.assertIn("already published and cannot be replaced", job)
        self.assertNotIn("workflow run deploy", workflow)


if __name__ == "__main__":
    unittest.main()
