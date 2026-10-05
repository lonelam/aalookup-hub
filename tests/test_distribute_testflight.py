import base64
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("distribution", ROOT / "scripts/distribute_testflight.py")
distribution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(distribution)


class Apple:
    def __init__(self, state="READY_FOR_BETA_SUBMISSION", grouped=False, automatic=False):
        self.state, self.grouped, self.automatic = state, grouped, automatic
        self.writes = []
        self.notes = []
        self.processing = "VALID"
        self.expired = False
        self.audience = "APP_STORE_ELIGIBLE"
        self.bundle = distribution.BUNDLE_ID
        self.build_number = "72"
        self.available = True
        self.link_enabled = True
        self.add_submits = False
        self.fail_write = False
        self.version = {"id": "train", "attributes": {"version": "1.0.15", "platform": "IOS"}}

    def call(self, method, path, body=None, query=None):
        if method != "GET":
            self.writes.append((method, path, body))
            if self.fail_write:
                raise RuntimeError("uncertain response")
            if "buildBetaDetails/" in path:
                self.automatic = body["data"]["attributes"]["autoNotifyEnabled"]
            elif path.endswith("/relationships/builds"):
                self.grouped = True
                if self.add_submits:
                    self.state = "WAITING_FOR_BETA_REVIEW"
            elif path == "/v1/betaAppReviewSubmissions":
                self.state = "WAITING_FOR_BETA_REVIEW"
            elif path == "/v1/buildBetaNotifications":
                self.state = "IN_BETA_TESTING"
            return {}
        if path == f"/v1/apps/{distribution.APP_ID}":
            return {"data": {"attributes": {"bundleId": self.bundle}}}
        if path == f"/v1/betaGroups/{distribution.GROUP_ID}":
            return {"data": {"id": distribution.GROUP_ID, "attributes": {
                "isInternalGroup": False, "publicLinkEnabled": self.link_enabled, "publicLink": distribution.PUBLIC_LINK}}}
        if path.endswith("/app"):
            return {"data": {"id": distribution.APP_ID}}
        if path.endswith("/buildBetaDetail"):
            return {"data": {"id": "detail", "attributes": {
                "externalBuildState": self.state, "autoNotifyEnabled": self.automatic}}}
        raise AssertionError((method, path))

    def rows(self, path, query=None):
        if path == "/v1/preReleaseVersions":
            assert query == {"filter[app]": distribution.APP_ID, "filter[version]": "1.0.15", "filter[platform]": "IOS"}
            return [copy.deepcopy(self.version)]
        if path == "/v1/builds":
            assert query["filter[version]"] == "72" and query["filter[preReleaseVersion]"] == "train"
            if not self.available:
                return []
            return [{"id": "build", "attributes": {"version": self.build_number, "expired": self.expired,
                "buildAudienceType": self.audience, "processingState": self.processing}, "relationships": {
                "app": {"data": {"id": distribution.APP_ID}}, "preReleaseVersion": {"data": {"id": "train"}}}}]
        if path.endswith("/betaBuildLocalizations"):
            return self.notes
        if path.endswith("/betaGroups"):
            return [{"id": distribution.GROUP_ID}] if self.grouped else []
        raise AssertionError(path)


class DistributionTests(unittest.TestCase):
    def run_distribution(self, api, **kwargs):
        return distribution.distribute(api, "1.0.15", "72", sleep=lambda _: None, **kwargs)

    def test_first_submission_sets_notes_group_and_automatic_release(self):
        api = Apple()
        result = self.run_distribution(api)
        self.assertEqual(result["externalBuildState"], "WAITING_FOR_BETA_REVIEW")
        self.assertFalse(result["availableToPublicTesters"])
        paths = [path for _, path, _ in api.writes]
        self.assertEqual(paths, ["/v1/betaBuildLocalizations", "/v1/buildBetaDetails/detail",
            f"/v1/betaGroups/{distribution.GROUP_ID}/relationships/builds", "/v1/betaAppReviewSubmissions"])
        self.assertNotRegex(distribution.NOTES, r"(?i)mdx|mdd|mdict")
        self.assertIn("Settings → Dictionaries → Search & download", distribution.NOTES)

    def test_pending_or_public_build_is_idempotent(self):
        for state in ("WAITING_FOR_BETA_REVIEW", "IN_BETA_REVIEW", "IN_BETA_TESTING"):
            with self.subTest(state=state):
                api = Apple(state, grouped=True, automatic=True)
                result = self.run_distribution(api)
                self.assertEqual(api.writes, [])
                self.assertEqual(result["availableToPublicTesters"], state == "IN_BETA_TESTING")

    def test_group_addition_that_submits_does_not_double_submit(self):
        api = Apple()
        api.add_submits = True
        self.run_distribution(api)
        self.assertFalse(any(path == "/v1/betaAppReviewSubmissions" for _, path, _ in api.writes))

    def test_approved_build_starts_testing_without_repeating_review(self):
        for state in ("READY_FOR_BETA_TESTING", "BETA_APPROVED"):
            api = Apple(state, grouped=True, automatic=True)
            self.assertTrue(self.run_distribution(api)["availableToPublicTesters"])
            self.assertEqual([path for _, path, _ in api.writes], ["/v1/buildBetaNotifications"])

    def test_operator_notes_are_preserved_and_empty_localization_is_updated(self):
        api = Apple()
        api.notes = [{"id": "notes", "attributes": {"locale": "zh-Hans", "whatsNew": "Specific release notes"}}]
        self.run_distribution(api)
        self.assertFalse(any("Localizations" in path for _, path, _ in api.writes))
        api = Apple()
        api.notes = [{"id": "notes", "attributes": {"locale": "zh-Hans", "whatsNew": ""}}]
        self.run_distribution(api)
        self.assertEqual(api.writes[0][:2], ("PATCH", "/v1/betaBuildLocalizations/notes"))

    def test_rejected_compliance_unknown_expired_and_wrong_identity_never_mutate(self):
        cases = [("state", "BETA_REJECTED"), ("state", "MISSING_EXPORT_COMPLIANCE"),
                 ("state", "NEW_UNKNOWN_STATE"), ("expired", True), ("bundle", "other.app"),
                 ("build_number", "73"), ("audience", "INTERNAL_ONLY"), ("link_enabled", False),
                 ("processing", "INVALID")]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                api = Apple()
                setattr(api, key, value)
                with self.assertRaises(RuntimeError):
                    self.run_distribution(api)
                self.assertEqual(api.writes, [])

    def test_missing_or_processing_build_times_out_without_latest_fallback(self):
        for key, value in (("available", False), ("processing", "PROCESSING"), ("state", "PROCESSING")):
            api = Apple()
            setattr(api, key, value)
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                self.run_distribution(api, timeout=0)
            self.assertEqual(api.writes, [])

    def test_uncertain_mutation_is_not_retried(self):
        api = Apple()
        api.fail_write = True
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            self.run_distribution(api)
        self.assertEqual(len(api.writes), 1)

    def test_invalid_inputs_never_contact_apple(self):
        for version, number in (("latest", "72"), ("1.0.15", ""), ("1.0.15", "72; echo secret")):
            with self.assertRaises(RuntimeError):
                distribution.distribute(None, version, number)

    def test_real_openssl_jwt_signature_and_claims(self):
        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder) / "key.pem"
            subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(key)], check=True, capture_output=True)
            encoded = distribution.token(key, "TESTKEY", "test-issuer")
            header, claims, signature = encoded.split(".")
            decode = lambda part: base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
            self.assertEqual(json.loads(decode(header))["alg"], "ES256")
            self.assertEqual(json.loads(decode(claims))["aud"], "appstoreconnect-v1")
            raw = decode(signature)
            self.assertEqual(len(raw), 64)
            integers = []
            for value in (raw[:32], raw[32:]):
                value = value.lstrip(b"\0")
                if value[0] >= 128:
                    value = b"\0" + value
                integers.append(b"\x02" + bytes([len(value)]) + value)
            content = b"".join(integers)
            sig = Path(folder) / "signature"
            sig.write_bytes(b"\x30" + bytes([len(content)]) + content)
            public = Path(folder) / "public.pem"
            subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(public)], check=True, capture_output=True)
            subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(sig)],
                           input=f"{header}.{claims}".encode(), check=True, capture_output=True)

    def test_workflows_bind_the_uploaded_number_and_do_not_gate_other_platforms(self):
        for name, version_output, publisher in (("release.yml", "version", "release"), ("pre-release.yml", "source_version", "publish")):
            source = (ROOT / ".github/workflows" / name).read_text()
            job = source.split("  ios:")[1].split(f"  {publisher}:")[0]
            self.assertIn("continue-on-error: true", job)
            self.assertIn("steps.public-testflight.outcome == 'failure'", job)
            self.assertIn("## Public TestFlight needs attention", job)
            self.assertIn("ref: ${{ github.sha }}", job)
            self.assertIn(f"needs.resolve.outputs.{version_output}", job)
            self.assertIn("BUILD_NUMBER: ${{ github.run_number }}", job)
            self.assertLess(job.index("name: aalookup-ios"), job.index("id: public-testflight"))
            self.assertIn('python3 testflight-workflow/scripts/distribute_testflight.py --version "$VERSION"', job)
            self.assertNotIn("testflight-public", source.split(f"  {publisher}:")[1])
        source = (ROOT / ".github/workflows/testflight-public.yml").read_text()
        self.assertIn("ref: ${{ github.sha }}", source)
        self.assertIn("cancel-in-progress: false", source)
        self.assertNotIn("secrets.AALOOKUP_SOURCE_TOKEN", source)
        self.assertNotIn("contents: write", source)


if __name__ == "__main__":
    unittest.main()
