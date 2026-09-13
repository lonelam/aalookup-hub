"""Exercise the trusted caller and actual workflow packaging without networking."""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import textwrap
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("deploy_production", ROOT / "scripts" / "deploy_production.py")
caller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(caller)
BINARIES = (
    "aalookup-server", "aalookup-backup", "aalookup-database",
    "aalookup-membership-transition", "aalookup-event-log",
)
SOURCE_SHA = "e" * 40


class PackagingContract(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.outputs = self.root / "target" / "x86_64-unknown-linux-musl" / "release"
        self.outputs.mkdir(parents=True)
        for name in BINARIES:
            (self.outputs / name).write_bytes(f"fixture:{name}|debug".encode())
        website = self.root / "website" / "dist"
        website.mkdir(parents=True)
        (website / "index.html").write_text("<h1>AALookup</h1>")
        self.commands = self.root / "mock-bin"
        self.commands.mkdir()
        # Mock ELF inspection and debug removal for portable contract tests.
        # The workflow's file checks,
        # copy, permissions, hashing and tar creation execute on real files.
        file_probe = self.commands / "file"
        file_probe.write_text(
            '#!/bin/sh\ncase "$1" in\n'
            '  *"${TEST_DYNAMIC_BINARY:-__none__}") echo "dynamically linked";;\n'
            '  *) echo "statically linked";;\nesac\n'
        )
        file_probe.chmod(0o755)
        stripper = self.commands / "strip"
        stripper.write_text(
            '#!/usr/bin/env python3\nimport os, pathlib, sys\n'
            'assert sys.argv[1] == "--strip-debug"\n'
            'if os.environ.get("TEST_STRIP_FAILURE"): sys.exit(1)\n'
            'p = pathlib.Path(sys.argv[2])\n'
            'p.write_bytes(p.read_bytes().removesuffix(b"|debug"))\n'
        )
        stripper.chmod(0o755)
        # macOS has shasum but does not necessarily have sha256sum.
        checksum = self.commands / "sha256sum"
        checksum.write_text(
            '#!/usr/bin/env python3\nimport hashlib, pathlib, sys\n'
            'for name in sys.argv[1:]:\n'
            ' print(hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest(), name)\n'
        )
        checksum.chmod(0o755)

    def package(self, archive_limit=None, expanded_limit=None, **extra_env):
        workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
        step = re.search(
            r"(?ms)^      - name: Package deployment\n(.*?)(?=^      - name: |\Z)",
            workflow,
        )
        self.assertIsNotNone(step)
        command = textwrap.dedent(step.group(1).split("        run: |\n", 1)[1])
        if archive_limit is not None:
            command = command.replace("packed > 104857600", f"packed > {archive_limit}")
        if expanded_limit is not None:
            command = command.replace("unpacked > 536870912", f"unpacked > {expanded_limit}")
        return subprocess.run(
            ["bash", "-c", command], cwd=self.root, capture_output=True, text=True,
            env={"PATH": f"{self.commands}{os.pathsep}{os.environ['PATH']}", "COPYFILE_DISABLE": "1",
                 "SOURCE_SHA": SOURCE_SHA, **extra_env},
        )

    def test_build_and_package_select_the_same_five_production_binaries(self):
        source = (ROOT / ".github/workflows/deploy.yml").read_text()
        build = source.split("- name: Build server\n", 1)[1].split("- name:", 1)[0]
        package = source.split("- name: Package deployment\n", 1)[1].split("- name:", 1)[0]
        self.assertIn("cargo build --locked --release -p aalookup-server", build)
        self.assertIn("-p aalookup-event-log", build)
        self.assertIn("--target x86_64-unknown-linux-musl", build)
        self.assertEqual(tuple(re.findall(r"--bin ([a-z-]+)", build)), BINARIES)
        self.assertEqual(tuple(re.search(r"binaries=\(([^)]+)\)", package).group(1).split()), BINARIES)
        self.assertNotIn("--bins", build)
        self.assertNotIn("aalookup-billing", build + package)
        self.assertIn("run: npm run server:deploy:test\n", source)

    def test_archive_contains_every_operator_from_the_same_build_output(self):
        # Other local Cargo outputs must not widen the production payload.
        for name in ("aalookup-billing", "aalookup-billing-catalog"):
            (self.outputs / name).write_bytes(b"excluded tool")
        result = self.package()
        self.assertEqual(result.returncode, 0, result.stderr)
        archive = self.root / f"aalookup-{SOURCE_SHA}.tar.gz"
        with tarfile.open(archive) as payload:
            members = {entry.name.removeprefix("./"): entry for entry in payload if entry.isfile()}
            self.assertEqual(set(members), {*BINARIES, "website/index.html"})
            for name in BINARIES:
                shipped = f"fixture:{name}".encode()
                self.assertEqual(payload.extractfile(members[name]).read(), shipped)
                self.assertEqual((self.outputs / name).read_bytes(), shipped + b"|debug")
                self.assertEqual(members[name].mode & 0o777, 0o755)
                self.assertRegex(result.stdout, hashlib.sha256(shipped).hexdigest() + rf"\s+deploy/{name}")
        self.assertIn(archive.name, result.stdout)

    def test_debug_removal_failure_stops_packaging_without_changing_build_output(self):
        result = self.package(TEST_STRIP_FAILURE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / f"aalookup-{SOURCE_SHA}.tar.gz").exists())
        for name in BINARIES:
            self.assertTrue((self.outputs / name).read_bytes().endswith(b"|debug"))

    def test_archive_and_expanded_size_limits_fail_before_upload(self):
        for limits in ({"archive_limit": 1}, {"expanded_limit": 1}):
            with self.subTest(limits=limits):
                result = self.package(**limits)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Deployment exceeds the host's", result.stderr)

    def test_missing_empty_or_linked_operator_cannot_publish_an_archive(self):
        for name in BINARIES:
            binary = self.outputs / name
            for kind in ("missing", "empty", "symlink"):
                with self.subTest(name=name, kind=kind):
                    binary.unlink()
                    if kind == "empty":
                        binary.touch()
                    elif kind == "symlink":
                        binary.symlink_to(self.outputs / "aalookup-server")
                    result = self.package()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(name, result.stderr)
                    self.assertFalse((self.root / f"aalookup-{SOURCE_SHA}.tar.gz").exists())
                    binary.unlink(missing_ok=True)
                    binary.write_bytes(f"fixture:{name}".encode())

    def test_dynamically_linked_operator_cannot_publish_an_archive(self):
        for name in ("aalookup-membership-transition", "aalookup-event-log"):
            with self.subTest(name=name):
                result = self.package(TEST_DYNAMIC_BINARY=name)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / f"aalookup-{SOURCE_SHA}.tar.gz").exists())


class CallerContract(unittest.TestCase):
    def run_caller(self, deploy_status=0, check_status=0, **environment):
        with tempfile.TemporaryDirectory() as directory:
            inputs = {
                "source_sha": SOURCE_SHA, "deploy_user": "www", "deploy_host": "example.test",
                "deploy_port": "2222", "deploy_url": "https://example.test",
                "private_key": "TEST-ONLY-NOT-A-KEY", "known_hosts": "TEST-ONLY-HOST",
            }
            calls = []
            def run(args, **_kwargs):
                calls.append(args)
                if args[0] == "ssh" and "--check" in args[-1]:
                    if check_status:
                        raise caller.Fail("host protocol check failed")
                    return subprocess.CompletedProcess(args, 0)
                result = deploy_status if args[0] == "ssh" and "--protocol" in args[-1] else 0
                return subprocess.CompletedProcess(args, result)
            with patch.object(caller, "validated_inputs", return_value=inputs), \
                    patch.object(caller.Path, "is_file", return_value=True), \
                    patch.dict(os.environ, {"RUNNER_TEMP": directory, **environment}, clear=True), \
                    patch.object(caller.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as key_probe, \
                    patch.object(caller, "run", side_effect=run):
                try:
                    result = caller.main()
                except caller.Fail as error:
                    result = error
            key_probe.assert_called_once()
            self.assertEqual(list(Path(directory).iterdir()), [], "temporary credential files must be removed")
            return result, calls

    def test_caller_checks_protocol_ten_before_upload_with_strict_host_identity(self):
        result, calls = self.run_caller()
        self.assertEqual(caller.PROTOCOL, "10")
        self.assertEqual(result, 0)
        self.assertEqual([args[0] for args in calls], ["ssh", "scp", "ssh"])
        self.assertEqual(calls[0][-1], "sudo -n -- /usr/local/sbin/aalookup-deploy --check --protocol 10")
        self.assertEqual(calls[2][-1], f"sudo -n -- /usr/local/sbin/aalookup-deploy --protocol 10 {SOURCE_SHA}")
        for args in calls:
            self.assertIn("StrictHostKeyChecking=yes", args)
            self.assertIn("IdentitiesOnly=yes", args)

    def test_protocol_mismatch_never_uploads_or_deploys(self):
        for operation in ("release", "review-pages-plan", "review-pages-apply"):
            with self.subTest(operation=operation):
                result, calls = self.run_caller(check_status=1, DEPLOY_OPERATION=operation,
                                                REVIEW_MANIFEST_SHA256="d" * 64)
                self.assertIsInstance(result, caller.Fail)
                self.assertEqual(str(result), "host protocol check failed")
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][-1], "sudo -n -- /usr/local/sbin/aalookup-deploy --check --protocol 10")

    def test_failed_deployment_cleans_archive_without_a_weaker_protocol_retry(self):
        result, calls = self.run_caller(deploy_status=1)
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[-1][-1], f"rm -f -- /home/www/deploy/aalookup-{SOURCE_SHA}.tar.gz")
        self.assertEqual(sum("--protocol" in args[-1] for args in calls), 2)

    def test_review_plan_and_apply_name_the_same_pinned_manifest_without_release_fallback(self):
        manifest = "d" * 64
        for operation in ("review-pages-plan", "review-pages-apply"):
            with self.subTest(operation=operation):
                result, calls = self.run_caller(DEPLOY_OPERATION=operation, REVIEW_MANIFEST_SHA256=manifest)
                self.assertEqual(result, 0)
                self.assertEqual(len(calls), 3)
                self.assertEqual(calls[0][-1], "sudo -n -- /usr/local/sbin/aalookup-deploy --check --protocol 10")
                self.assertEqual(calls[2][-1],
                                 f"sudo -n -- /usr/local/sbin/aalookup-deploy --{operation} --protocol 10 {SOURCE_SHA} {manifest}")
                self.assertEqual(calls[1][-1],
                                 f"www@example.test:/home/www/deploy/aalookup-review-pages-{SOURCE_SHA}-{manifest}.tar.gz")

    def test_unknown_operation_or_unpinned_review_is_rejected_before_network(self):
        for environment in ({"DEPLOY_OPERATION": "website"}, {"DEPLOY_OPERATION": "review-pages-apply"},
                            {"DEPLOY_OPERATION": "review-pages-apply", "REVIEW_MANIFEST_SHA256": "bad"}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True), \
                    self.assertRaises(caller.Fail):
                caller.deployment_target(SOURCE_SHA)



if __name__ == "__main__":
    unittest.main()
