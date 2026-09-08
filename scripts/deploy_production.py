#!/usr/bin/env python3
"""Ship a built deployment archive to the production host and ask the
root-owned helper there to install it.

This is the caller; `server/aalookup-deploy` in lonelam/aalookup is the helper.
Everything that touches production state happens on the far side of the SSH
connection — this script's whole job is to validate its inputs, put the archive
where the helper expects it, and name a protocol version the helper recognises.

The pair used to be two bash scripts. The helper was rewritten in Python
because its rollback logic is exactly the kind of thing `set -e` cannot express
safely; this side follows so the deployment path is one language, and so the
argument validation below can be unit tested rather than reasoned about.

Environment (all required unless noted):
    DEPLOY_SSH_PRIVATE_KEY  private key with access to the deployment account
    DEPLOY_KNOWN_HOSTS      pinned host keys; verified out of band
    DEPLOY_HOST             production hostname
    DEPLOY_USER             deployment account name
    DEPLOY_PORT             SSH port (default 22)
    DEPLOY_URL              public origin, used only for validation here
    SOURCE_SHA              exact 40-character commit SHA being deployed
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

PROTOCOL = "3"

# \A…\Z, not ^…$: Python's `$` also matches before a trailing newline.
COMMIT_SHA = re.compile(r"\A[0-9a-f]{40}\Z")
DEPLOY_ACCOUNT = re.compile(r"\A[a-z_][a-z0-9_-]*\Z")
PORT_NUMBER = re.compile(r"\A[0-9]+\Z")


class Fail(Exception):
    """A refusal with a message, as opposed to a bug with a traceback."""


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise Fail(f"Set the production {name} secret")
    return value


def validated_inputs() -> dict[str, str]:
    source_sha = required("SOURCE_SHA")
    if COMMIT_SHA.match(source_sha) is None:
        raise Fail("SOURCE_SHA must be a full 40-character lowercase commit SHA")

    deploy_user = required("DEPLOY_USER")
    if DEPLOY_ACCOUNT.match(deploy_user) is None:
        raise Fail("DEPLOY_USER is invalid")

    port = os.environ.get("DEPLOY_PORT", "22").strip() or "22"
    if PORT_NUMBER.match(port) is None or not 1 <= int(port) <= 65535:
        raise Fail("DEPLOY_PORT must be between 1 and 65535")

    deploy_url = required("DEPLOY_URL").rstrip("/")
    if not deploy_url.startswith(("http://", "https://")):
        raise Fail("DEPLOY_URL must be an HTTP(S) origin")

    return {
        "source_sha": source_sha,
        "deploy_user": deploy_user,
        "deploy_host": required("DEPLOY_HOST"),
        "deploy_port": port,
        "deploy_url": deploy_url,
        "private_key": required("DEPLOY_SSH_PRIVATE_KEY"),
        "known_hosts": required("DEPLOY_KNOWN_HOSTS"),
    }


def write_private(path: Path, contents: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(contents.replace("\r", ""))
        if not contents.endswith("\n"):
            handle.write("\n")


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, check=False, stdin=subprocess.DEVNULL)
    if check and result.returncode != 0:
        raise Fail(f"command failed ({result.returncode}): {args[0]}")
    return result


def main() -> int:
    inputs = validated_inputs()
    source_sha = inputs["source_sha"]

    archive = Path(f"aalookup-{source_sha}.tar.gz")
    if not archive.is_file():
        raise Fail(f"Missing deployment archive {archive}")

    runner_temp = os.environ.get("RUNNER_TEMP")
    if not runner_temp:
        raise Fail("RUNNER_TEMP is required")
    ssh_dir = Path(tempfile.mkdtemp(prefix="aalookup-ssh-", dir=runner_temp))
    ssh_dir.chmod(stat.S_IRWXU)

    try:
        key_path = ssh_dir / "deploy_key"
        hosts_path = ssh_dir / "known_hosts"
        write_private(key_path, inputs["private_key"])
        write_private(hosts_path, inputs["known_hosts"])

        # A passphrase-protected key would hang the non-interactive SSH below
        # rather than fail it, so it is rejected here where the message is
        # readable.
        probe = subprocess.run(
            ["ssh-keygen", "-y", "-P", "", "-f", str(key_path)],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )
        if probe.returncode != 0:
            sys.stderr.write(probe.stderr.decode("utf-8", "replace"))
            raise Fail(
                "DEPLOY_SSH_PRIVATE_KEY could not be read as an unencrypted private key"
            )

        destination = f"{inputs['deploy_user']}@{inputs['deploy_host']}"
        remote_archive = f"/home/{inputs['deploy_user']}/deploy/{archive.name}"

        common = [
            "-i", str(key_path),
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={hosts_path}",
        ]
        ssh_options = [*common, "-p", inputs["deploy_port"]]
        scp_options = [*common, "-P", inputs["deploy_port"]]

        run(["scp", *scp_options, str(archive), f"{destination}:{remote_archive}"])

        # The remote shell receives one string, so the only interpolated value
        # is quoted here. The protocol and path are literals.
        deploy_command = (
            f"sudo -n -- /usr/local/sbin/aalookup-deploy "
            f"--protocol {PROTOCOL} {shlex.quote(source_sha)}"
        )
        deployed = run(["ssh", *ssh_options, destination, deploy_command], check=False)
        if deployed.returncode != 0:
            cleanup = f"rm -f -- {shlex.quote(remote_archive)}"
            run(["ssh", *ssh_options, destination, cleanup], check=False)
            return 1
    finally:
        shutil.rmtree(ssh_dir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
