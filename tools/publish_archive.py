#!/usr/bin/env python3
"""Publish a verified export without checking out or changing a code branch."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

from repo_dump import verify_archive


ARCHIVE_REF = "refs/heads/repository-archive"
MARKER_PATH = ".repository-archive.json"
MARKER = {"format": 1, "managed_by": "tools/publish_archive.py"}
MAX_FILE_BYTES = 40 * 1024 * 1024
# Leave room below 200 MiB for tree/commit objects and pack framing.
MAX_BATCH_BYTES = 190 * 1024 * 1024


class PublishError(Exception):
    """The export or destination is unsafe to publish."""


class Git:
    def __init__(self, repository: Path, index: Path):
        self.repository = repository
        self.env = os.environ.copy()
        self.env["GIT_INDEX_FILE"] = str(index)
        self.env["GIT_AUTHOR_NAME"] = "github-actions[bot]"
        self.env["GIT_AUTHOR_EMAIL"] = "41898282+github-actions[bot]@users.noreply.github.com"
        self.env["GIT_COMMITTER_NAME"] = self.env["GIT_AUTHOR_NAME"]
        self.env["GIT_COMMITTER_EMAIL"] = self.env["GIT_AUTHOR_EMAIL"]

    def run(self, *args: str, input: str | None = None) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repository), *args],
            input=input, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env,
        )
        if result.returncode:
            # Do not echo remote URLs or git diagnostics: a local remote may
            # contain credentials, and the caller already knows the operation.
            raise PublishError(f"git {args[0]} failed (exit {result.returncode})")
        return result.stdout.strip()

    def remote_tip(self, ref: str) -> str | None:
        result = self.run("ls-remote", "--refs", "origin", ref)
        if not result:
            return None
        rows = [row.split() for row in result.splitlines()]
        if len(rows) != 1 or rows[0][1] != ref:
            raise PublishError("The remote returned an unexpected branch reference")
        return rows[0][0]

    def stage_blob(self, name: str, oid: str) -> None:
        self.run("update-index", "--add", "--cacheinfo", "100644", oid, name)


def archive_files(archive: Path, names: set[str]) -> list[Path]:
    if not archive.is_dir() or archive.is_symlink():
        raise PublishError("Archive must be a regular directory, not a symlink")
    if any(not isinstance(name, str) for name in names):
        raise PublishError("Manifest file paths must be strings")
    files = []
    for name in sorted(names):
        if not isinstance(name, str) or "\\" in name or "\0" in name:
            raise PublishError("Manifest contains an unsafe file path")
        parts = name.split("/")
        if any(part in ("", ".", "..") or ":" in part or part.casefold() == ".git" for part in parts):
            raise PublishError("Manifest contains an unsafe relative file path")
        path = archive
        for part in parts:
            path = path / part
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise PublishError("Manifest file path contains a symlink or junction")
        if not path.is_file():
            raise PublishError(f"A manifest file is missing or not a regular file: {name}")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise PublishError(f"File exceeds the 40 MiB storage limit: {name}")
        files.append(path)
    return sorted(files, key=lambda path: path.relative_to(archive).as_posix())


def verify_export(archive: Path) -> list[Path]:
    try:
        manifest = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PublishError("A readable archive/manifest.json is required") from exc
    if not isinstance(manifest, dict):
        raise PublishError("Archive manifest must be a JSON object")
    if manifest.get("status") != "complete" or manifest.get("failures"):
        raise PublishError("Only a complete export with no recorded failures can be published")
    # A local rerun may retain cached assets that no longer occur in this
    # snapshot. Publish only files referenced by the verified manifest.
    names = {"manifest.json", "README.md"}
    try:
        names.update(entry["path"] for entry in manifest.get("metadata", []))
        names.update(
            part["path"] for asset in manifest.get("assets", []) for part in asset["parts"]
        )
    except (KeyError, TypeError) as exc:
        raise PublishError("Manifest contains malformed file entries") from exc
    files = archive_files(archive, names)
    failures = verify_archive(archive)
    if failures:
        raise PublishError(f"Archive verification found {len(failures)} failure(s)")
    return files


def publish(archive: Path, repository: Path, *, push: bool = False) -> str:
    """Build in an isolated Git index, upload bounded batches, then publish.

    All temporary commits are made with commit-tree. They never check out,
    update, or commit the caller's working tree, index, or local branches.
    """
    archive = archive.absolute()
    repository = repository.resolve()
    files = verify_export(archive)
    with tempfile.TemporaryDirectory(prefix="repository-archive-") as directory:
        git = Git(repository, Path(directory) / "index")
        git.run("rev-parse", "--git-dir")
        current_ref = git.run("rev-parse", "--abbrev-ref", "HEAD")
        if current_ref == "repository-archive":
            raise PublishError("Run the publisher from the code branch, not repository-archive")

        old_tip = git.remote_tip(ARCHIVE_REF)
        if old_tip:
            git.run("fetch", "--no-tags", "--no-write-fetch-head", "origin", ARCHIVE_REF)
            try:
                marker = json.loads(git.run("show", f"{old_tip}:{MARKER_PATH}"))
            except (ValueError, PublishError) as exc:
                raise PublishError("Existing archive branch is not managed by this publisher") from exc
            if marker != MARKER:
                raise PublishError("Existing archive branch has an unexpected ownership marker")
        git.run("read-tree", "--empty")
        marker_oid = git.run("hash-object", "-w", "--stdin", input=json.dumps(MARKER) + "\n")
        git.stage_blob(MARKER_PATH, marker_oid)
        readme = (
            "# Repository archive\n\n"
            "The latest complete export is in [archive/](archive/). "
            "See [manifest.json](archive/manifest.json) for provenance, counts, "
            "attachment parts and checksums.\n\n"
            "This branch is managed by tools/publish_archive.py on the code branch. "
            "Intermediate upload commits may contain only part of an export.\n"
        )
        git.stage_blob("README.md", git.run("hash-object", "-w", "--stdin", input=readme))

        staging_ref = f"refs/heads/repository-archive-upload-{uuid.uuid4().hex}"
        parent = old_tip
        batch_bytes = len(readme.encode()) + len(json.dumps(MARKER).encode()) + 1
        batch_number = 0
        staging_created = False
        completed = False
        if push:
            print(f"Temporary upload branch: {staging_ref}", flush=True)

        def commit_batch() -> None:
            nonlocal parent, batch_number, staging_created
            tree = git.run("write-tree")
            args = ["commit-tree", tree]
            if parent:
                args += ["-p", parent]
            batch_number += 1
            parent = git.run(*args, input=f"Archive upload batch {batch_number}\n")
            if push:
                # A push per batch, not just a commit per batch, bounds the
                # new uncompressed objects sent in each transfer to 200 MiB.
                staging_created = True  # cleanup also covers uncertain pushes
                git.run("push", "origin", f"{parent}:{staging_ref}")
            print(f"Prepared archive batch {batch_number} ({batch_bytes} bytes)")

        try:
            for path in files:
                size = path.stat().st_size
                if batch_bytes + size > MAX_BATCH_BYTES:
                    commit_batch()
                    batch_bytes = 0
                oid = git.run("hash-object", "-w", "--no-filters", "--", str(path))
                git.stage_blob("archive/" + path.relative_to(archive).as_posix(), oid)
                batch_bytes += size
            final_tree = git.run("write-tree")
            if old_tip and final_tree == git.run("rev-parse", f"{old_tip}^{{tree}}"):
                print("Archive is unchanged; nothing to publish")
                return old_tip
            commit_batch()
            assert parent is not None
            if push:
                # Never force: another writer wins rather than losing updates.
                # All data is already at the remote, so this final push is tiny.
                if git.remote_tip(ARCHIVE_REF) != old_tip:
                    raise PublishError("Archive branch changed during upload; rerun the workflow")
                git.run("push", "origin", f"{parent}:{ARCHIVE_REF}")
                if git.remote_tip(ARCHIVE_REF) != parent:
                    raise PublishError("Remote archive commit could not be confirmed")
                completed = True
                print(f"Published and confirmed repository-archive at {parent}")
            else:
                print("Dry run complete; pass --push to publish repository-archive")
            return parent
        finally:
            if staging_created:
                try:
                    git.run("push", "origin", "--delete", staging_ref)
                except PublishError:
                    state = "after publication" if completed else "after an interrupted upload"
                    print(
                        f"Warning: temporary branch {staging_ref} remains {state}; "
                        "it can be deleted from the repository's branches page.",
                        file=sys.stderr,
                    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path("archive"))
    parser.add_argument("--repo-dir", type=Path, default=Path("."))
    parser.add_argument("--push", action="store_true", help="Publish to origin/repository-archive")
    args = parser.parse_args()
    try:
        publish(args.archive, args.repo_dir, push=args.push)
    except (PublishError, OSError, ValueError) as exc:
        print(f"Archive was not published: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
