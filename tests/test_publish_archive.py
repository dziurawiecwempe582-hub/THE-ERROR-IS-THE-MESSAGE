"""Exercise publication through real Git repositories and a local bare remote.

These tests never contact a network remote. Only the batch size is reduced;
object creation, commits, pushes, and remote-tree inspection use real Git.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1] / "tools"
SPEC = importlib.util.spec_from_file_location("publish_archive", TOOLS / "publish_archive.py")
publisher = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(TOOLS))
try:
    SPEC.loader.exec_module(publisher)
finally:
    sys.path.pop(0)


def checksum(data):
    return hashlib.sha256(data).hexdigest()


@unittest.skipUnless(shutil.which("git"), "Git is required for local publication integration tests")
class PublishArchiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="archive-publish-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repository = self.root / "working repository"
        self.remote = self.root / "remote.git"
        self.archive = self.repository / "archive"

        # User Git settings, hooks and an inherited index must not affect the
        # fixture or allow its local pushes to invoke outside configuration.
        environment = os.environ.copy()
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"):
            environment.pop(name, None)
        environment.update({
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Archive integration test",
            "GIT_AUTHOR_EMAIL": "archive-test@example.invalid",
            "GIT_COMMITTER_NAME": "Archive integration test",
            "GIT_COMMITTER_EMAIL": "archive-test@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        })
        self.environment = mock.patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

        self.git("init", "--bare", str(self.remote))
        self.git("init", "--initial-branch=main", str(self.repository))
        (self.repository / "code.txt").write_bytes(b"committed code\n")
        self.work_git("add", "code.txt")
        self.work_git("commit", "-m", "Initial code")
        self.work_git("remote", "add", "origin", str(self.remote))
        self.work_git("push", "origin", "main")
        self.initial_main = self.work_git("rev-parse", "HEAD")

        # Preserve both a staged version and a different unstaged version.
        (self.repository / "code.txt").write_bytes(b"staged code\n")
        (self.repository / "staged.txt").write_bytes(b"staged addition\n")
        self.work_git("add", "code.txt", "staged.txt")
        (self.repository / "code.txt").write_bytes(b"unstaged code\n")
        (self.repository / "private.env").write_bytes(b"UNRELATED_SECRET=never-publish\n")
        self.expected_files, self.manifest = self.create_archive()

    def git(self, *arguments):
        result = subprocess.run(
            ["git", *map(str, arguments)], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )
        if result.returncode:
            self.fail(f"Local git {arguments[0]} failed: {result.stderr.decode('utf-8', errors='replace')}")
        return result.stdout

    def work_git(self, *arguments):
        return self.git("-C", self.repository, *arguments)

    def remote_git(self, *arguments):
        return self.git("--git-dir", self.remote, *arguments)

    def create_archive(self):
        files = {"README.md": b"# Complete fixture archive\n"}
        metadata = b'{"full_name":"fixture/project"}\n'
        files["metadata/repository.json"] = metadata
        manifest = {
            "format_version": 1, "repository": "fixture/project",
            "status": "complete", "failures": [], "counts": {"assets": 3},
            "metadata": [{"path": "metadata/repository.json", "size": len(metadata), "sha256": checksum(metadata)}],
            "assets": [],
        }
        for index in range(3):
            # Distinct binary data prevents accidental Git object deduplication
            # from hiding whether separate batches reached the remote.
            data = bytes([index, 0, 255]) * 701
            name = f"assets/attachment-{index}.bin"
            files[name] = data
            manifest["assets"].append({
                "url": f"https://github.com/user-attachments/files/{index}/fixture.bin",
                "size": len(data), "sha256": checksum(data),
                "parts": [{"path": name, "size": len(data), "sha256": checksum(data)}],
            })
        files["manifest.json"] = json.dumps(manifest, indent=2).encode() + b"\n"
        for name, data in files.items():
            path = self.archive / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        # Neither unrelated files nor stale cache files belong to the export.
        (self.archive / ".env").write_bytes(b"ARCHIVE_SECRET=never-publish\n")
        (self.archive / "assets" / "stale-cache.bin").write_bytes(b"NOT_IN_MANIFEST\n")
        return files, manifest

    def working_state(self):
        return {
            "head": self.work_git("rev-parse", "HEAD"),
            "branch": self.work_git("symbolic-ref", "HEAD"),
            "local_branches": self.work_git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"),
            "status": self.work_git("status", "--porcelain=v1", "-z"),
            "index": (self.repository / ".git" / "index").read_bytes(),
            "code": (self.repository / "code.txt").read_bytes(),
            "staged_file": (self.repository / "staged.txt").read_bytes(),
        }

    def test_batches_reach_remote_and_final_export_preserves_private_work(self):
        before = self.working_state()
        observed_uploads = []
        real_run = publisher.Git.run

        def observe_real_git(instance, *arguments, **kwargs):
            result = real_run(instance, *arguments, **kwargs)
            if arguments[0] == "push" and len(arguments) == 3:
                commit, separator, ref = arguments[2].partition(":")
                if separator and ref.startswith("refs/heads/repository-archive-upload-"):
                    # Inspect immediately after each actual push, before the
                    # publisher prepares or uploads the following batch.
                    remote_tip = self.remote_git("rev-parse", ref).decode().strip()
                    names = set(self.remote_git("ls-tree", "-r", "--name-only", ref).decode().splitlines())
                    self.assertEqual(remote_tip, commit)
                    self.assertEqual(self.remote_git("for-each-ref", "--format=%(refname)", "refs/heads/repository-archive"), b"")
                    self.assertNotIn("archive/.env", names)
                    self.assertNotIn("private.env", names)
                    self.assertNotIn("archive/assets/stale-cache.bin", names)
                    observed_uploads.append((commit, names))
            return result

        with mock.patch.object(publisher, "MAX_BATCH_BYTES", 3000), mock.patch.object(publisher.Git, "run", observe_real_git), contextlib.redirect_stdout(io.StringIO()):
            final_commit = publisher.publish(self.archive, self.repository, push=True)

        self.assertGreaterEqual(len(observed_uploads), 3)
        self.assertGreater(len(observed_uploads[-1][1]), len(observed_uploads[0][1]))
        self.assertEqual(self.remote_git("rev-parse", "refs/heads/repository-archive").decode().strip(), final_commit)
        expected_names = {"README.md", ".repository-archive.json"} | {"archive/" + name for name in self.expected_files}
        actual_names = set(self.remote_git("ls-tree", "-r", "--name-only", final_commit).decode().splitlines())
        self.assertEqual(actual_names, expected_names)
        for name, data in self.expected_files.items():
            self.assertEqual(self.remote_git("show", f"{final_commit}:archive/{name}"), data)
        remote_refs = self.remote_git("for-each-ref", "--format=%(refname)", "refs/heads").decode().splitlines()
        self.assertEqual(set(remote_refs), {"refs/heads/main", "refs/heads/repository-archive"})
        self.assertEqual(self.remote_git("rev-parse", "refs/heads/main"), self.initial_main)
        self.assertEqual(self.working_state(), before)

    def test_partial_and_corrupt_exports_are_rejected_before_any_remote_change(self):
        for defect in ("partial", "corrupt"):
            with self.subTest(defect=defect):
                for name, data in self.expected_files.items():
                    (self.archive / name).write_bytes(data)
                if defect == "partial":
                    bad_manifest = dict(self.manifest, status="partial", failures=[{"kind": "api", "error": "Unavailable review thread"}])
                    (self.archive / "manifest.json").write_text(json.dumps(bad_manifest), encoding="utf-8")
                else:
                    (self.archive / "assets" / "attachment-0.bin").write_bytes(b"corrupt attachment")
                before = self.working_state()
                remote_before = self.remote_git("show-ref")
                with self.assertRaises(publisher.PublishError), contextlib.redirect_stdout(io.StringIO()):
                    publisher.publish(self.archive, self.repository, push=True)
                self.assertEqual(self.remote_git("show-ref"), remote_before)
                self.assertEqual(self.working_state(), before)


if __name__ == "__main__":
    unittest.main()
