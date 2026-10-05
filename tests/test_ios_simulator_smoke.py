import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/ios-simulator-smoke.yml").read_text()


class IosSimulatorSmokeTests(unittest.TestCase):
    def test_exact_source_is_validated_before_checkout_and_checked_afterwards(self):
        self.assertLess(WORKFLOW.index("Validate exact source revision"), WORKFLOW.index("Checkout exact private source"))
        self.assertIn("^[0-9a-f]{40}$", WORKFLOW)
        self.assertIn('test "$(git rev-parse HEAD)" = "$SOURCE_SHA"', WORKFLOW)
        self.assertIn("ref: ${{ inputs.source_sha }}", WORKFLOW)
        self.assertIn("persist-credentials: false", WORKFLOW)

    def test_native_acceptance_uses_ios27_and_never_publishes(self):
        self.assertIn("runs-on: xcode-27", WORKFLOW)
        self.assertIn("node scripts/ios-simulator-smoke.mjs", WORKFLOW)
        self.assertIn("workflow_dispatch:", WORKFLOW)
        self.assertNotRegex(WORKFLOW, r"(?m)^  (?:push|pull_request|schedule):")
        self.assertNotIn("contents: write", WORKFLOW)
        self.assertNotRegex(WORKFLOW, r"ASC_API|APPLE_API|upload-app|release-ios|publish_candidate|dispatch-deploy")
        self.assertEqual(re.findall(r"secrets\.([A-Z_]+)", WORKFLOW), ["AALOOKUP_SOURCE_TOKEN"])

    def test_actions_are_pinned_and_failure_evidence_is_scoped(self):
        actions = re.findall(r"uses: ([^\s#]+)", WORKFLOW)
        self.assertTrue(actions)
        for action in actions:
            self.assertRegex(action, r"@[0-9a-f]{40}$")
        self.assertIn("if: always()", WORKFLOW)
        self.assertIn("retention-days: 7", WORKFLOW)
        paths = [path.strip() for path in WORKFLOW.split("          path: |", 1)[1].splitlines() if path.strip()]
        expected = {
            ".dev-data/ios-simulator-smoke-*/" + name for name in (
                "environment.json", "simulators.json", "bundle-verification.json", "App-Info.plist",
                "application.log", "commands.log", "installation.log", "simulator-host.log",
                "simulator-final-state.json", "final-screen.png", "screenshots/**", "Smoke.xcresult/**",
            )
        }
        self.assertEqual(set(paths), expected)
        self.assertEqual(len(paths), len(expected))


if __name__ == "__main__":
    unittest.main()
