"""Exercise the static-only packaging trust boundary without credentials."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("package_review_pages", ROOT / "scripts" / "package_review_pages.py")
packager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packager)
SOURCE_SHA = "a" * 40


class ReviewPackagingContract(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.candidate = self.root / "candidate"
        page = self.candidate / "website" / "terms" / "index.html"
        page.parent.mkdir(parents=True)
        page.write_bytes(b"<h1>Terms</h1>")
        self.manifest = {"schemaVersion": 1, "kind": "aalookup-public-review-pages", "sourceSha": SOURCE_SHA,
                         "files": [{"path": "terms/index.html", "sha256": packager.digest(page), "baseSha256": None}]}
        self.manifest_path = self.candidate / "manifest.json"

    def build(self, **kwargs):
        self.manifest_path.write_text(json.dumps(self.manifest))
        with patch("builtins.print"):
            return packager.package(self.candidate, kwargs.get("source_sha", SOURCE_SHA),
                                    kwargs.get("manifest_sha", packager.digest(self.manifest_path)), self.root)

    def test_archive_contains_only_manifest_and_reviewed_files(self):
        (self.candidate / "review.json").write_bytes(b"local review metadata excluded")
        (self.candidate / "website" / "checkout.js").write_bytes(b"not approved")
        archive = self.build()
        with tarfile.open(archive) as payload:
            self.assertEqual(payload.getnames(), ["manifest.json", "website/terms/index.html"])
            for entry in payload.getmembers():
                self.assertTrue(entry.isfile())
                self.assertEqual(entry.mode, 0o644)
                self.assertEqual(entry.uid, 0)
                self.assertEqual(payload.extractfile(entry).read(), (self.candidate / entry.name).read_bytes())
        with self.assertRaises(FileExistsError):
            self.build()

    def test_manifest_or_source_identity_mismatch_refuses_before_archive_creation(self):
        for kwargs in ({"manifest_sha": "f" * 64}, {"source_sha": "b" * 40}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.build(**kwargs)
        self.assertEqual(list(self.root.glob("*.tar.gz")), [])

    def test_payload_drift_and_unsafe_or_duplicate_paths_are_refused(self):
        original = dict(self.manifest["files"][0])
        for entry in (dict(original, sha256="0" * 64), dict(original, path="../server.env"),
                      dict(original, path="account/index.html"), dict(original, baseSha256="invalid")):
            self.manifest["files"] = [entry]
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                self.build()
        self.manifest["files"] = [original, original]
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual(list(self.root.glob("*.tar.gz")), [])

    def test_symlinked_file_or_parent_cannot_redirect_packaging(self):
        page = self.candidate / "website" / "terms" / "index.html"
        outside = self.root / "outside.html"
        outside.write_bytes(page.read_bytes())
        page.unlink()
        page.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "regular single-link"):
            self.build()
        page.unlink()
        page.parent.rmdir()
        outside_directory = self.root / "outside"
        outside_directory.mkdir()
        (outside_directory / "index.html").write_bytes(outside.read_bytes())
        page.parent.symlink_to(outside_directory, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "parent is a symlink"):
            self.build()


if __name__ == "__main__":
    unittest.main()
