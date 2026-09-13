"""An associated rollout needs an exact approved source, ordinary ops do not."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_client_release", ROOT / "scripts" / "check_client_release.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
SHA = "a" * 40


class ClientReleaseGate(unittest.TestCase):
    def test_only_the_current_approved_version_and_exact_source_pass(self):
        for version in ("v0.7.8", "0.7.8"):
            gate.assert_approved_manifest({"version": version, "sourceCommit": SHA}, "v0.7.8", SHA)
        for manifest in (None, {}, {"version": "v0.7.8"}, {"version": "v0.7.8", "sourceCommit": None},
                         {"version": "v0.7.7", "sourceCommit": SHA}, {"version": "v0.7.8", "sourceCommit": "b" * 40}):
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                gate.assert_approved_manifest(manifest, "v0.7.8", SHA)
        for tag, sha in (("v0.7.8-rc.1", SHA), ("0.7.8", SHA), ("v0.7.8", "HEAD")):
            with self.assertRaises(ValueError):
                gate.assert_approved_manifest({"version": tag, "sourceCommit": sha}, tag, sha)

    def test_probe_is_bounded_and_reads_only_the_public_approved_manifest(self):
        response = io.BytesIO(json.dumps({"version": "v0.7.8", "sourceCommit": SHA}).encode())
        response.status = 200
        with patch.object(gate, "urlopen", return_value=response) as opener:
            gate.check_approved_release("https://example.test/", "v0.7.8", SHA)
            request = opener.call_args.args[0]
            self.assertEqual(request.full_url, "https://example.test/api/v1/releases/latest")
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.headers["Cache-control"], "no-cache")
            self.assertEqual(opener.call_args.kwargs["timeout"], 20)
        for origin in ("http://example.test", "https://secret@example.test", "https://example.test/path", "https://example.test?key=x"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                gate.check_approved_release(origin, "v0.7.8", SHA)
        for content in (b"<html>SPA fallback</html>", b"x" * (1024 * 1024 + 1)):
            response = io.BytesIO(content)
            response.status = 200
            with patch.object(gate, "urlopen", return_value=response), self.assertRaises(ValueError):
                gate.check_approved_release("https://example.test", "v0.7.8", SHA)

    def test_only_explicit_client_association_enables_the_workflow_gate(self):
        workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
        gate_step = workflow.split("      - name: Check approval for a client-associated website rollout\n", 1)[1].split("      - name:", 1)[0]
        self.assertIn("if: ${{ inputs.client_release_tag != '' }}", gate_step)
        self.assertIn("CLIENT_RELEASE_TAG: ${{ inputs.client_release_tag }}", gate_step)
        self.assertIn("SOURCE_SHA: ${{ inputs.source_sha }}", gate_step)
        self.assertIn("run: python3 .aalookup-hub/scripts/check_client_release.py", gate_step)
        self.assertLess(workflow.index("check_client_release.py"), workflow.index("deploy_production.py"))
        self.assertIn("client_release_tag:", workflow)
        self.assertIn("required: false", workflow.split("client_release_tag:", 1)[1].split("request_id:", 1)[0])


if __name__ == "__main__":
    unittest.main()
