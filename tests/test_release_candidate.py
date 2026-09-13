"""Verify the public candidate boundary and hash the actual staged test bytes."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
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

    def test_staged_windows_runtime_requires_its_own_signature(self):
        for name in ("AALookup-windows-x86_64-update.tar.gz",
                     "AALookup-windows-x86_64-update.tar.gz.sig"):
            path = self.root / name
            content = path.read_bytes()
            path.unlink()
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "exactly"):
                self.create()
            path.write_bytes(content)

    def test_prerelease_cli_binds_preview_identity_and_the_same_signed_bytes(self):
        command = [sys.executable, str(ROOT / "scripts/release_provenance.py"), "--prerelease",
                   str(self.root), "a" * 40, "b" * 40, "v1.0.0-rc.1"]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads((self.root / provenance.PROVENANCE_NAME).read_text())
        self.assertEqual(data["releaseTag"], "v1.0.0-rc.1")
        self.assertEqual(len(data["assets"]), 15)
        for item in data["assets"]:
            content = (self.root / item["name"]).read_bytes()
            self.assertEqual(item["sha256"], hashlib.sha256(content).hexdigest())

    def test_prerelease_mode_rejects_stable_and_invalid_semver_labels(self):
        for tag in ("v1.0.0", "v1.0.0-01", "v1.0.0-rc..1", "v1.0.0-rc.1+build", "1.0.0-rc.1"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                self.create(release_tag=tag, prerelease=True)


class CandidateWorkflow(unittest.TestCase):
    def test_both_workflows_use_the_same_trusted_publisher(self):
        self.assertEqual(len(provenance.ASSET_NAMES), 15)
        for name in ("release.yml", "pre-release.yml"):
            with self.subTest(workflow=name):
                workflow = (ROOT / ".github/workflows" / name).read_text()
                command = "python3 public-workflow/scripts/publish_candidate.py publish"
                self.assertIn(command, workflow)
                self.assertIn("test_publish_candidate.py", workflow)
                self.assertIn("test_release_candidate.py", workflow)
                self.assertIn("--directory dist-release --notes \"$notes\"" +
                              (" --prerelease" if name == "pre-release.yml" else "\n"), workflow)
                self.assertNotIn("gh release upload", workflow)
                self.assertNotIn("--clobber", workflow)
                self.assertNotIn("expected=(", workflow)
                self.assertIn("Private source:", workflow)
                self.assertIn("Public workflow commit:", workflow)

    def test_preflight_precedes_builds_and_does_not_block_ios_or_verify(self):
        for name in ("release.yml", "pre-release.yml"):
            workflow = (ROOT / ".github/workflows" / name).read_text()
            resolve = workflow.split("\n  macos:\n", 1)[0]
            self.assertIn("      contents: read", resolve)
            self.assertIn("run: python3 public-workflow/scripts/publish_candidate.py check", resolve)
            for step_name in ("Checkout trusted publication preflight", "Check publication target before platform builds"):
                step = re.search(r"(?ms)^      - name: " + re.escape(step_name) + r"\n(.*?)(?=^      - name: |^      # |\Z)", resolve).group(1)
                self.assertEqual("if: ${{ inputs.operation == 'release' }}" in step, name == "release.yml")
            preflight = resolve.split("      - name: Check publication target before platform builds", 1)[1]
            self.assertIn("GH_TOKEN: ${{ github.token }}", preflight)
            self.assertIn("VERSION: ${{ steps.source.outputs.version }}", preflight)
            self.assertIn("PRIVATE_SOURCE_SHA: ${{ steps.source.outputs.source_sha }}", preflight)
            self.assertIn("PUBLIC_WORKFLOW_SHA: ${{ github.sha }}", preflight)
            self.assertNotIn("AALOOKUP_SOURCE_TOKEN", preflight)

    def test_publish_requires_all_platforms_and_keeps_distribution_separate(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text()
        job = workflow.split("\n  release:\n", 1)[1]
        needs = re.search(r"(?ms)^    needs:\n(.*?)^    if:", job).group(1)
        self.assertEqual(re.findall(r"- (\w+)", needs), ["resolve", "macos", "windows", "android", "ios"])
        preview = (ROOT / ".github/workflows/pre-release.yml").read_text()
        self.assertIn("needs: [resolve, macos, windows, android, ios]", preview.split("\n  publish:\n", 1)[1])
        self.assertNotIn("always()", job)
        for source in (workflow, preview):
            self.assertNotIn("make_latest=true", source)
            self.assertNotIn("releases/refresh", source)
            self.assertNotIn("AALOOKUP_RELEASE_REFRESH_TOKEN", source)
            self.assertNotIn("workflow run deploy", source)
            self.assertIn("bash scripts/release-ios.sh", source)
            self.assertNotIn("predates", source)
            self.assertNotIn("building without an error-reporting DSN", source)
        source_input = workflow.split("      source_sha:\n", 1)[1].split("      version:\n", 1)[0]
        self.assertIn("required: true", source_input)


if __name__ == "__main__":
    unittest.main()
