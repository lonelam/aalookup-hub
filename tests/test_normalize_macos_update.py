"""Exercise the actual Tauri header shape and reject unsafe repairs."""
from __future__ import annotations

import gzip
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("normalize_macos_update", ROOT / "scripts/normalize_macos_update.py")
normalizer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(normalizer)


def patch_modes(path, replacements):
    # TarInfo.tobuf masks Unix type bits itself, so ordinary addfile fixtures
    # cannot reproduce Tauri's full st_mode headers. Patch real header bytes.
    data = bytearray(gzip.decompress(path.read_bytes()))
    offset = 0
    while data[offset:offset + 100].strip(b"\0"):
        header = data[offset:offset + 512]
        name = header[:100].split(b"\0")[0].decode().rstrip("/")
        if name in replacements:
            header[100:108] = f"{replacements[name]:07o}\0".encode()
            header[148:156] = b" " * 8
            header[148:156] = f"{sum(header):06o}\0 ".encode()
            data[offset:offset + 512] = header
        size = int(header[124:136].strip(b"\0 ") or b"0", 8)
        offset += 512 + (size + 511) // 512 * 512
    path.write_bytes(gzip.compress(data))


class MacosArchiveNormalization(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.archive = self.directory / "update.tar.gz"

    def fixture(self, extras=(), *, metadata=None):
        metadata = {"format": 1, "version": "1.0.0", "platform": "macos"} if metadata is None else metadata
        entries = [
            ("AALookup.app", tarfile.DIRTYPE, 0o755, b"", ""),
            ("AALookup.app/Contents/MacOS/aalookup", tarfile.REGTYPE, 0o755, b"app-binary\x00\xff", ""),
            ("AALookup.app/Contents/Helpers/aalookup-update-helper", tarfile.REGTYPE, 0o755, b"helper-binary", ""),
            ("AALookup.app/Contents/Resources/aalookup-update.json", tarfile.REGTYPE, 0o644, json.dumps(metadata).encode(), ""),
            ("AALookup.app/Contents/_CodeSignature/CodeResources", tarfile.REGTYPE, 0o644, b"opaque-resource-seal", ""),
            *extras,
        ]
        with tarfile.open(self.archive, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            for name, kind, mode, data, link in entries:
                entry = tarfile.TarInfo(name)
                entry.type, entry.mode, entry.linkname = kind, mode, link
                entry.size = len(data)
                entry.uid, entry.gid, entry.uname, entry.gname, entry.mtime = 501, 20, "builder", "staff", 123456
                archive.addfile(entry, io.BytesIO(data) if data else None)
        return self.archive

    def test_real_tauri_modes_normalize_without_changing_bundle_bytes_or_metadata(self):
        self.fixture([("AALookup.app/Contents/current", tarfile.SYMTYPE, 0o777, b"", "MacOS/aalookup")])
        patch_modes(self.archive, {"AALookup.app": 0o40755,
                    "AALookup.app/Contents/MacOS/aalookup": 0o100755,
                    "AALookup.app/Contents/_CodeSignature/CodeResources": 0o100644,
                    "AALookup.app/Contents/current": 0o120777})
        with tarfile.open(self.archive) as archive:
            self.assertEqual(archive.getmember("AALookup.app").mode, 0o40755)
            self.assertEqual(archive.getmember("AALookup.app/Contents/MacOS/aalookup").mode, 0o100755)
        original = normalizer.inspect_archive(self.archive, "1.0.0")
        with self.assertRaisesRegex(ValueError, "mode"):
            normalizer.inspect_archive(self.archive, "1.0.0", normalized=True)
        result = normalizer.normalize_archive(self.archive, "1.0.0")
        self.assertNotEqual(result["originalSha256"], result["normalizedSha256"])
        self.assertEqual(normalizer.inspect_archive(self.archive, "1.0.0", normalized=True), original)
        self.assertEqual(set(self.directory.iterdir()), {self.archive})
        second = normalizer.normalize_archive(self.archive, "1.0.0")
        self.assertEqual(second["originalSha256"], second["normalizedSha256"])

    def test_privileged_mismatched_and_unknown_mode_bits_never_get_repaired(self):
        for mode in (0o4755, 0o2755, 0o1755, 0o104755, 0o120755, 0o40755, 0o200755):
            with self.subTest(mode=oct(mode)):
                self.fixture()
                patch_modes(self.archive, {"AALookup.app/Contents/MacOS/aalookup": mode})
                before = self.archive.read_bytes()
                with self.assertRaisesRegex(ValueError, "mode"):
                    normalizer.normalize_archive(self.archive, "1.0.0")
                self.assertEqual(self.archive.read_bytes(), before)
                self.assertEqual(set(self.directory.iterdir()), {self.archive})

    def test_unsupported_entry_types_are_not_treated_as_regular_files(self):
        for kind in (tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE,
                     tarfile.CONTTYPE, tarfile.GNUTYPE_SPARSE):
            with self.subTest(kind=kind):
                self.fixture([("AALookup.app/extra", kind, 0o644, b"", "")])
                with self.assertRaises(ValueError):
                    normalizer.normalize_archive(self.archive, "1.0.0")

    def test_paths_and_links_cannot_alias_escape_or_shadow_files(self):
        invalid = [
            [("Other.app/file", tarfile.REGTYPE, 0o644, b"x", "")],
            [("AALookup.app/../escape", tarfile.REGTYPE, 0o644, b"x", "")],
            [("AALookup.app/Contents/macos/aalookup", tarfile.REGTYPE, 0o644, b"x", "")],
            [("AALookup.app/Contents/MacOS/aalookup/child", tarfile.REGTYPE, 0o644, b"x", "")],
            [("AALookup.app/Contents/link", tarfile.SYMTYPE, 0o777, b"", "../../escape")],
            [("AALookup.app/Contents/link", tarfile.SYMTYPE, 0o777, b"", "/tmp/outside")],
            [("AALookup.app/Contents/link", tarfile.SYMTYPE, 0o777, b"", "missing")],
            [("AALookup.app/Contents/link", tarfile.SYMTYPE, 0o777, b"", "link")],
            [("AALookup.app/Contents/link", tarfile.SYMTYPE, 0o777, b"", "other"),
             ("AALookup.app/Contents/other", tarfile.SYMTYPE, 0o777, b"", "link")],
            [("AALookup.app/Contents/deep/up", tarfile.SYMTYPE, 0o777, b"", "../.."),
             ("AALookup.app/Contents/link", tarfile.SYMTYPE, 0o777, b"", "deep/up/../../outside"),
             ("AALookup.app/Contents/outside", tarfile.REGTYPE, 0o644, b"x", "")],
            [("AALookup.app/Contents/link", tarfile.SYMTYPE, 0o777, b"", "bad*/../MacOS/aalookup")],
            [("AALookup.app/Contents/link", tarfile.SYMTYPE, 0o777, b"", "MacOS/aalookup/../aalookup")],
            [("AALookup.app/Contents/AΣ", tarfile.REGTYPE, 0o644, b"x", ""),
             ("AALookup.app/Contents/Aσ", tarfile.REGTYPE, 0o644, b"y", "")],
            [("AALookup.app/Contents/e\u0301", tarfile.REGTYPE, 0o644, b"x", ""),
             ("AALookup.app/Contents/é", tarfile.REGTYPE, 0o644, b"y", "")],
        ]
        for entries in invalid:
            with self.subTest(entries=entries):
                self.fixture(entries)
                with self.assertRaises(ValueError):
                    normalizer.normalize_archive(self.archive, "1.0.0")

    def test_safe_pax_metadata_survives_while_mode_overrides_are_rejected(self):
        for headers in ({"mtime": "123456.5", "SCHILY.xattr.com.apple.example": "opaque"},
                        {"SCHILY.mode": "0104755"}):
            self.fixture()
            original = self.directory / "original.tar.gz"
            self.archive.rename(original)
            with tarfile.open(original) as source, tarfile.open(self.archive, "w:gz", format=tarfile.PAX_FORMAT) as target:
                for member in source:
                    if member.name == "AALookup.app/Contents/MacOS/aalookup":
                        member.pax_headers = headers
                    target.addfile(member, source.extractfile(member) if member.isfile() else None)
            if "SCHILY.mode" in headers:
                with self.assertRaisesRegex(ValueError, "PAX"):
                    normalizer.normalize_archive(self.archive, "1.0.0")
            else:
                before = normalizer.inspect_archive(self.archive, "1.0.0")
                normalizer.normalize_archive(self.archive, "1.0.0")
                self.assertEqual(normalizer.inspect_archive(self.archive, "1.0.0", normalized=True), before)
            original.unlink()

    def test_wrong_manifest_and_resource_limits_fail_before_replacement(self):
        for metadata in ({"format": True, "version": "1.0.0", "platform": "macos"},
                         {"format": 1, "version": "1.0.1", "platform": "macos"},
                         {"format": 1, "version": "1.0.0", "platform": "windows"}):
            self.fixture(metadata=metadata)
            with self.assertRaisesRegex(ValueError, "metadata"):
                normalizer.normalize_archive(self.archive, "1.0.0")
        self.fixture()
        for constant in ("MAX_ENTRIES", "MAX_BYTES", "MAX_FILE_BYTES"):
            with self.subTest(constant=constant), mock.patch.object(normalizer, constant, 1):
                with self.assertRaises(ValueError):
                    normalizer.normalize_archive(self.archive, "1.0.0")

    def test_failed_output_validation_retains_original_and_cleans_temporary(self):
        self.fixture()
        before = self.archive.read_bytes()
        inspect = normalizer.inspect_archive
        def fail_output(path, *args, **kwargs):
            if kwargs.get("normalized"):
                raise ValueError("output mismatch")
            return inspect(path, *args, **kwargs)
        with mock.patch.object(normalizer, "inspect_archive", side_effect=fail_output):
            with self.assertRaisesRegex(ValueError, "output mismatch"):
                normalizer.normalize_archive(self.archive, "1.0.0")
        self.assertEqual(self.archive.read_bytes(), before)
        self.assertEqual(set(self.directory.iterdir()), {self.archive})

    def test_workflows_normalize_and_resign_before_apple_verification_and_upload(self):
        for workflow in ("release.yml", "pre-release.yml"):
            text = (ROOT / ".github/workflows" / workflow).read_text()
            job = text.split("\n  macos:\n", 1)[1].split("\n  windows:\n", 1)[0]
            self.assertIn("ref: ${{ github.sha }}\n          path: public-workflow", job)
            normalize = job.index('python3 public-workflow/scripts/normalize_macos_update.py "$archive" "$version"')
            sign = job.index('node node_modules/@tauri-apps/cli/tauri.js signer sign "$archive"')
            apple = job.index('codesign --verify --deep --strict --verbose=2 "$updater_app"')
            self.assertLess(job.index("bash scripts/release-macos.sh"), normalize)
            self.assertLess(normalize, job.index('rm -- "${archive}.sig"'))
            self.assertLess(job.index('rm -- "${archive}.sig"'), sign)
            self.assertLess(sign, apple)
            self.assertLess(apple, job.index("- name: Upload workflow artifacts"))
            self.assertIn("test_normalize_macos_update.py", job)


if __name__ == "__main__":
    unittest.main()
