import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("sync_wiki", Path(__file__).parents[1] / "scripts/sync_wiki.py")
wiki = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wiki)
SHA = "a" * 40


def git(directory, *args):
    return subprocess.run(["git", *args], cwd=directory, text=True, capture_output=True, check=True).stdout.strip()


class WikiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.export = self.root / "export"
        self.export.mkdir()
        self.pages = {name: wiki.MARKER + "\n# Guide\n" for name in ["Home.md", "zh-CN.md", "_Sidebar.md", "_Footer.md", "en-reading.md", "zh-CN-reading.md"]}
        self.write_export()

    def write_export(self):
        for name, body in self.pages.items():
            (self.export / name).write_text(body, encoding="utf-8")
        self.manifest = {"schema": 1, "sourceSha": SHA, "pages": list(self.pages)}
        self.write_manifest()

    def write_manifest(self):
        (self.export / "wiki-export.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def repository(self):
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        clone = self.root / "checkout"
        git(self.root, "clone", str(remote), str(clone))
        git(clone, "config", "user.name", "Wiki test")
        git(clone, "config", "user.email", "wiki@example.test")
        (clone / "Home.md").write_text(wiki.MARKER + "\n", encoding="utf-8")
        git(clone, "add", "Home.md")
        git(clone, "commit", "-m", "Initialize Wiki")
        git(clone, "push", "origin", "HEAD")
        return clone, remote

    def test_valid_export_is_complete_and_bound_to_revision(self):
        self.assertEqual(wiki.load_export(self.export, SHA), self.pages)
        with self.assertRaises(ValueError):
            wiki.load_export(self.export, "b" * 40)

    def test_rejects_paths_duplicates_extra_files_and_missing_pages(self):
        for names in [["../outside.md"], self.manifest["pages"] + ["Home.md"], ["en-reading.md"]]:
            with self.subTest(names=names):
                self.manifest["pages"] = names
                self.write_manifest()
                with self.assertRaises(ValueError):
                    wiki.load_export(self.export, SHA)
        self.write_export()
        (self.export / "secret.txt").write_text("unrelated")
        with self.assertRaises(ValueError):
            wiki.load_export(self.export, SHA)

    def test_rejects_symlink_oversize_and_unmarked_payload(self):
        path = self.export / "Home.md"
        path.unlink()
        path.symlink_to(self.export / "zh-CN.md")
        with self.assertRaises(ValueError):
            wiki.load_export(self.export, SHA)
        path.unlink()
        for body in ["unowned", wiki.MARKER + "\n" + "x" * 131072]:
            path.write_text(body)
            with self.assertRaises(ValueError):
                wiki.load_export(self.export, SHA)

    def test_removes_only_obsolete_owned_pages(self):
        directory = self.root / "wiki"
        directory.mkdir()
        (directory / "en-retired.md").write_text(wiki.MARKER + "\nold")
        (directory / "My-notes.md").write_text("Keep human notes")
        (directory / "picture.png").write_bytes(b"image")
        wiki.apply_export(directory, self.pages)
        self.assertFalse((directory / "en-retired.md").exists())
        self.assertEqual((directory / "My-notes.md").read_text(), "Keep human notes")
        self.assertEqual((directory / "picture.png").read_bytes(), b"image")
        self.assertEqual((directory / "en-reading.md").read_text(), self.pages["en-reading.md"])

    def test_collision_fails_before_deleting_or_writing(self):
        directory = self.root / "wiki"
        directory.mkdir()
        (directory / "Home.md").write_text("Human home")
        (directory / "en-retired.md").write_text(wiki.MARKER + "\nold")
        with self.assertRaises(ValueError):
            wiki.apply_export(directory, self.pages)
        self.assertEqual((directory / "Home.md").read_text(), "Human home")
        self.assertTrue((directory / "en-retired.md").exists())

    def test_publish_is_idempotent_and_commits_the_exact_revision(self):
        directory, remote = self.repository()
        checks = []
        self.assertTrue(wiki.publish(directory, self.pages, SHA, os.environ.copy(), checks.append))
        head = git(directory, "rev-parse", "HEAD")
        self.assertEqual(git(remote, "rev-parse", "HEAD"), head)
        self.assertIn(SHA, git(directory, "log", "-1", "--format=%s"))
        self.assertFalse(wiki.publish(directory, self.pages, SHA, os.environ.copy(), checks.append))
        self.assertEqual(git(directory, "rev-parse", "HEAD"), head)
        self.assertEqual(checks, [SHA, SHA, SHA])

    def test_stale_website_aborts_without_a_commit(self):
        directory, remote = self.repository()
        head = git(remote, "rev-parse", "HEAD")
        def reject(_source):
            raise RuntimeError("stale website")
        with self.assertRaisesRegex(RuntimeError, "stale website"):
            wiki.publish(directory, self.pages, SHA, os.environ.copy(), reject)
        self.assertEqual(git(directory, "rev-parse", "HEAD"), head)
        self.assertEqual(git(directory, "status", "--porcelain"), "")

    def test_concurrent_edit_is_never_force_pushed(self):
        directory, remote = self.repository()
        other = self.root / "other"
        git(self.root, "clone", str(remote), str(other))
        git(other, "config", "user.name", "Human")
        git(other, "config", "user.email", "human@example.test")
        (other / "Notes.md").write_text("Concurrent work")
        git(other, "add", "Notes.md")
        git(other, "commit", "-m", "Write notes")
        git(other, "push", "origin", "HEAD")
        head = git(remote, "rev-parse", "HEAD")
        with self.assertRaisesRegex(RuntimeError, "push failed"):
            wiki.publish(directory, self.pages, SHA, os.environ.copy(), lambda _source: None)
        self.assertEqual(git(remote, "rev-parse", "HEAD"), head)

    def test_live_revision_check_rejects_mismatch_and_redirect(self):
        class Response:
            url = wiki.REVISION_URL
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                pass
            def read(self, _size):
                return json.dumps({"sourceSha": SHA}).encode()
        response = Response()
        with patch.object(wiki.urllib.request, "urlopen", return_value=response):
            wiki.verify_live(SHA)
            with self.assertRaises(RuntimeError):
                wiki.verify_live("b" * 40)
            response.url = "https://example.test/redirect"
            with self.assertRaises(RuntimeError):
                wiki.verify_live(SHA)

    def test_credentials_are_not_written_to_git_configuration(self):
        env = wiki.authenticated_environment("test-secret")
        self.assertNotIn("WIKI_TOKEN", env)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        with self.assertRaises(ValueError):
            wiki.authenticated_environment("")

    def test_workflow_publishes_only_after_success_and_scopes_token_to_wiki_jobs(self):
        source = (Path(__file__).parents[1] / ".github/workflows/deploy.yml").read_text()
        deploy = source.split("  deploy:\n", 1)[1].split("  sync-wiki:\n", 1)[0]
        sync = source.split("  sync-wiki:\n", 1)[1]
        self.assertIn("needs: wiki-access", deploy)
        self.assertIn("if: ${{ !inputs.wiki_check_only }}", deploy)
        self.assertIn("needs: deploy", sync)
        self.assertNotIn("always()", sync)
        self.assertNotIn("WIKI_TOKEN", deploy)
        self.assertLess(deploy.index("Export public Wiki"), deploy.index("- name: Deploy production"))
        self.assertIn("cancel-in-progress: false", source)
        self.assertIn("contents: read", source)
        self.assertNotIn("--force", source)


if __name__ == "__main__":
    unittest.main()
