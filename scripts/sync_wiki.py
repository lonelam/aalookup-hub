"""Publish a bounded, generated Wiki artifact after its website revision is live."""

import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request

MARKER = "<!-- AALookup generated user guide. -->"
REMOTE = "https://github.com/lonelam/aalookup-hub.wiki.git"
REVISION_URL = "https://aalookup.com/wiki/revision.json"
PAGE = re.compile(r"(?:Home|zh-CN|_Sidebar|_Footer|(?:en|zh-CN)-[a-z0-9]+(?:-[a-z0-9]+)*)\.md\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")


def git(args, directory, env=None, allowed=(0,)):
    result = subprocess.run(["git", *args], cwd=directory, env=env, text=True, capture_output=True, check=False)
    if result.returncode not in allowed:
        # Git diagnostics may contain credentials or transport configuration.
        raise RuntimeError(f"Wiki git {args[0]} failed (exit {result.returncode}); check Wiki access and branch state")
    return result


def authenticated_environment(token):
    if not token or "\n" in token or "\r" in token:
        raise ValueError("Configure AALOOKUP_WIKI_TOKEN before deploying; it must be able to push the Hub Wiki")
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GIT_CONFIG_") or key in {"GIT_DIR", "GIT_WORK_TREE", "GIT_ASKPASS", "WIKI_TOKEN"}:
            del env[key]
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {encoded}",
        "GIT_CONFIG_KEY_1": "credential.helper",
        "GIT_CONFIG_VALUE_1": "",
    })
    return env


def load_export(directory, source_sha):
    if not SHA.fullmatch(source_sha):
        raise ValueError("Expected an exact source SHA")
    manifest_path = directory / "wiki-export.json"
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > 32768:
        raise ValueError("Missing or invalid Wiki manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if set(manifest) != {"schema", "sourceSha", "pages"} or manifest["schema"] != 1 or manifest["sourceSha"] != source_sha:
        raise ValueError("Wiki artifact does not match the deployed source")
    names = manifest["pages"]
    if not isinstance(names, list) or not 4 <= len(names) <= 204 or not all(isinstance(name, str) and PAGE.fullmatch(name) for name in names):
        raise ValueError("Invalid Wiki page list")
    if len(set(names)) != len(names) or not {"Home.md", "zh-CN.md", "_Sidebar.md", "_Footer.md"} <= set(names):
        raise ValueError("Missing or duplicate Wiki navigation")
    if {path.name for path in directory.iterdir()} != set(names) | {"wiki-export.json"}:
        raise ValueError("Wiki artifact contains unexpected files")
    pages = {}
    size = 0
    for name in names:
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 131072:
            raise ValueError("Wiki pages must be bounded regular files")
        body = path.read_text(encoding="utf-8")
        if not body.startswith(MARKER + "\n"):
            raise ValueError("Wiki page has no ownership marker")
        size += path.stat().st_size
        if size > 2097152:
            raise ValueError("Wiki artifact exceeds 2 MiB")
        pages[name] = body
    return pages


def apply_export(directory, pages):
    # Only files carrying our ownership marker can be replaced or removed.
    # Human-authored Wiki pages and attachments remain untouched.
    owned = set()
    for path in directory.glob("*.md"):
        if path.is_symlink():
            raise ValueError("Refusing Wiki symlinks")
        if PAGE.fullmatch(path.name):
            with path.open(encoding="utf-8") as source:
                if source.readline().rstrip("\r\n") == MARKER:
                    owned.add(path.name)
    for name in pages:
        path = directory / name
        if path.exists() and name not in owned:
            raise ValueError(f"Generated Wiki page conflicts with an unowned page: {name}")
    for name in owned - pages.keys():
        (directory / name).unlink()
    for name, body in pages.items():
        (directory / name).write_text(body, encoding="utf-8")
    return sorted(owned | pages.keys())


def verify_live(source_sha):
    request = urllib.request.Request(REVISION_URL, headers={"Cache-Control": "no-cache", "User-Agent": "AALookup-Wiki-Sync"})
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.url != REVISION_URL:
            raise RuntimeError("Unexpected website revision redirect")
        body = response.read(4097)
    if len(body) > 4096 or json.loads(body).get("sourceSha") != source_sha:
        raise RuntimeError("Website revision differs; refusing to publish a stale Wiki artifact")


def publish(directory, pages, source_sha, env, live_check=verify_live):
    live_check(source_sha)
    paths = apply_export(directory, pages)
    git(["add", "--", *paths], directory, env)
    if git(["diff", "--cached", "--quiet"], directory, env, allowed=(0, 1)).returncode == 0:
        return False
    git(["-c", "user.name=AALookup Wiki", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com", "commit", "-m", f"Sync Wiki From {source_sha}"], directory, env)
    live_check(source_sha)
    # Never force-push or overwrite a concurrent edit. A failed push is visible
    # and can be retried from a fresh clone after the conflict is resolved.
    git(["push", "origin", "HEAD"], directory, env)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--export", type=Path)
    parser.add_argument("--source-sha")
    args = parser.parse_args()
    env = authenticated_environment(os.environ.get("WIKI_TOKEN", ""))
    pages = None if args.check else load_export(args.export, args.source_sha or "")
    with tempfile.TemporaryDirectory(prefix="aalookup-wiki-") as temporary:
        directory = Path(temporary) / "wiki"
        git(["clone", "--quiet", REMOTE, str(directory)], Path(temporary), env)
        git(["push", "--dry-run", "origin", "HEAD"], directory, env)
        if args.check:
            print("Wiki exists and push access is ready.")
            return
        changed = publish(directory, pages, args.source_sha, env)
        print("Wiki synchronized." if changed else "Wiki already matches this source.")


if __name__ == "__main__":
    main()
