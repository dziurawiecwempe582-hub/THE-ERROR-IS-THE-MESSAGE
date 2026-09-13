"""Offline acceptance tests for archive completeness and safe retrieval.

Fixtures deliberately cover pagination and failure modes that a tiny repository
cannot reveal. No GitHub credentials or network access are required.
"""

import hashlib
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request


SOURCE = Path(__file__).resolve().parents[1] / "tools" / "repo_dump.py"
SPEC = importlib.util.spec_from_file_location("repo_dump", SOURCE)
dump = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dump
SPEC.loader.exec_module(dump)


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Response(io.BytesIO):
    def __init__(self, data, headers=None, url="https://api.github.com/example"):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        super().__init__(data)
        self.headers = headers or {}
        self.url = url
        self.status = 200

    def geturl(self):
        return self.url


class AttachmentExtractionTests(unittest.TestCase):
    def test_example_asset_paths_are_not_uploaded_files(self):
        valid = "https://github.com/user-attachments/assets/11111111-aaaa-bbbb-cccc-111111111111"
        value = f"Example: `https://github.com/user-attachments/assets/xxxx`. Actual: {valid}"
        self.assertEqual(dump.extract_attachment_urls(value), {valid})

    def test_nested_json_and_common_github_attachment_forms(self):
        urls = {
            "https://github.com/user-attachments/assets/11111111-aaaa-bbbb-cccc-111111111111",
            "https://github.com/user-attachments/files/12345/notes.pdf",
            "https://user-images.githubusercontent.com/42/example.png",
        }
        value = {
            "body": f'<img alt="diagram" src="{sorted(urls)[0]}" />',
            "comments": [
                {"body": f"[specification]({sorted(urls)[1]})"},
                {"body": f"Plain attachment: {sorted(urls)[2]}\n"},
            ],
            "unrelated": [None, True, 9, "https://github.com/owner/repo/issues/12"],
        }
        self.assertEqual(dump.extract_attachment_urls(value), urls)

    def test_duplicate_urls_are_not_downloaded_twice(self):
        url = "https://github.com/user-attachments/files/12345/spec.pdf"
        self.assertEqual(dump.extract_attachment_urls([url, {"body": f"[copy]({url})"}]), {url})

    def test_download_allowlist_rejects_lookalikes_and_insecure_urls(self):
        for url in (
            "http://github.com/user-attachments/files/1/a.txt",
            "https://github.com.evil.example/user-attachments/files/1/a.txt",
            "https://github.com@evil.example/user-attachments/files/1/a.txt",
            "file:///etc/passwd",
            "https://127.0.0.1/private",
            "https://github.com:notaport/file.bin",
            "https://github.com:99999/file.bin",
            "https://[broken/file.bin",
        ):
            with self.subTest(url=url):
                self.assertFalse(dump.is_allowed_download(url))


class QueueOpener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, *args, **kwargs):
        self.requests.append(request)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    __call__ = open


class PaginationTests(unittest.TestCase):
    def test_more_than_one_hundred_records_and_short_intermediate_page(self):
        second = "https://api.github.com/repos/owner/project/issues?state=all&page=2&per_page=100"
        third = "https://api.github.com/repos/owner/project/issues?state=all&page=3&per_page=100"
        first_values = [{"id": n} for n in range(100)]
        transport = QueueOpener([
            Response(first_values, {"Link": f'<{second}>; rel="next", <{third}>; rel="last"'}),
            Response([{"id": 100}], {"Link": f'<{third}>; rel="next"'}),
            Response([{"id": 101}]),
        ])
        client = dump.GitHubClient(token=None, opener=transport)
        values = list(client.json_pages("/repos/owner/project/issues?state=all&per_page=100"))
        self.assertEqual([row["id"] for row in values], list(range(102)))
        self.assertEqual(len(transport.requests), 3)

    def test_untrusted_pagination_link_is_not_followed_with_token(self):
        transport = QueueOpener([
            Response([{"id": 1}], {"Link": '<https://attacker.example/collect>; rel="next"'}),
            Response([]),
        ])
        client = dump.GitHubClient(token="fake-test-token", opener=transport)
        with self.assertRaises(Exception):
            list(client.json_pages("/repos/owner/project/issues"))
        self.assertEqual(len(transport.requests), 1)


class RedirectTests(unittest.TestCase):
    def test_release_source_redirect_is_allowed_without_forwarding_token(self):
        request = Request(
            "https://api.github.com/repos/owner/project/zipball/v1",
            headers={"Authorization": "Bearer fake-test-token"},
        )
        target = "https://codeload.github.com/owner/project/legacy.zip/refs/tags/v1"
        redirected = dump.SafeRedirectHandler().redirect_request(request, None, 302, "Found", {}, target)
        self.assertEqual(redirected.full_url, target)
        self.assertNotIn("authorization", {key.lower() for key, _ in redirected.header_items()})
        self.assertFalse(dump.is_allowed_download(target.replace("codeload.github.com", "codeload.github.com.evil.example")))
        self.assertFalse(dump.is_allowed_download(target.replace("https://", "http://")))

    def test_github_s3_attachment_redirect_is_allowed_without_credentials(self):
        request = Request("https://github.com/user-attachments/assets/example",
                          headers={"Authorization": "Bearer fake-test-token"})
        target = "https://github-production-user-asset-6210df.s3.amazonaws.com/123/file.mp4"
        redirected = dump.SafeRedirectHandler().redirect_request(request, None, 302, "Found", {}, target)
        self.assertEqual(redirected.full_url, target)
        self.assertNotIn("authorization", {key.lower() for key, _ in redirected.header_items()})
        self.assertFalse(dump.is_allowed_download(target.replace(".com/", ".com.attacker.example/")))

    def test_token_is_only_sent_to_github_api(self):
        transport = QueueOpener([Response(b"file", url="https://github.com/user-attachments/files/1/a.bin")])
        client = dump.GitHubClient(token="fake-test-token", opener=transport)
        with client._open("https://github.com/user-attachments/files/1/a.bin", accept="application/octet-stream") as response:
            self.assertEqual(response.read(), b"file")
        self.assertNotIn("authorization", {key.lower() for key, _ in transport.requests[0].header_items()})

    def test_cross_domain_download_redirect_drops_authorization(self):
        request = Request(
            "https://api.github.com/repos/owner/project/releases/assets/1",
            headers={"Authorization": "Bearer fake-test-token", "Accept": "application/octet-stream"},
        )
        request.add_unredirected_header("Authorization", "Bearer fake-test-token")
        redirected = dump.SafeRedirectHandler().redirect_request(
            request, None, 302, "Found", {},
            "https://release-assets.githubusercontent.com/github-production-release-asset/1/file.bin",
        )
        self.assertIsNotNone(redirected)
        self.assertNotIn("authorization", {key.lower() for key, _ in redirected.header_items()})

    def test_redirect_to_untrusted_host_or_http_is_rejected(self):
        for destination in (
            "https://attacker.example/file.bin", "http://github.com/file.bin",
            "https://github.com.attacker.example/file.bin",
        ):
            request = Request("https://github.com/user-attachments/files/1/a.bin")
            with self.subTest(destination=destination), self.assertRaises(Exception):
                dump.SafeRedirectHandler().redirect_request(request, None, 302, "Found", {}, destination)


class ArchiveIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "archive"
        (self.output / "assets").mkdir(parents=True)

    def fixture(self):
        chunks = [b"first part\x00binary\xff", b"second part"]
        parts = []
        for index, data in enumerate(chunks):
            relative = f"assets/sample.part{index:04d}"
            (self.output / relative).write_bytes(data)
            parts.append({"path": relative, "sha256": digest(data), "size": len(data)})
        manifest = {
            "status": "complete",
            "repo": "owner/project",
            "counts": {},
            "metadata": [{"path": "metadata.json", "sha256": digest(b"{}"), "size": 2}],
            "assets": [{
                "url": "https://github.com/user-attachments/files/1/sample.bin",
                "sha256": digest(b"".join(chunks)),
                "size": sum(map(len, chunks)),
                "parts": parts,
            }],
            "failures": [],
        }
        (self.output / "metadata.json").write_bytes(b"{}")
        self.write_manifest(manifest)
        return manifest

    def write_manifest(self, manifest):
        (self.output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_split_asset_is_verified_as_one_reconstructable_file(self):
        self.fixture()
        self.assertEqual(dump.verify_archive(self.output), [])

    def test_restore_recovers_original_and_never_overwrites_existing_file(self):
        manifest = self.fixture()
        target = self.output.parent / "restored.bin"
        asset = manifest["assets"][0]
        dump.restore_asset(self.output, asset["sha256"], target)
        self.assertEqual(target.read_bytes(), b"first part\x00binary\xffsecond part")
        with self.assertRaises(FileExistsError):
            dump.restore_asset(self.output, asset["sha256"], target)
        self.assertEqual(digest(target.read_bytes()), asset["sha256"])

    def test_corrupt_asset_is_refused_before_creating_restored_file(self):
        manifest = self.fixture()
        asset = manifest["assets"][0]
        (self.output / asset["parts"][0]["path"]).write_bytes(b"bad")
        target = self.output.parent / "restored.bin"
        with self.assertRaises(ValueError):
            dump.restore_asset(self.output, asset["sha256"], target)
        self.assertFalse(target.exists())

    def test_corrupt_and_missing_parts_are_reported(self):
        manifest = self.fixture()
        (self.output / manifest["assets"][0]["parts"][0]["path"]).write_bytes(b"corrupted")
        self.assertTrue(dump.verify_archive(self.output))
        (self.output / manifest["assets"][0]["parts"][1]["path"]).unlink()
        self.assertTrue(dump.verify_archive(self.output))

    def test_part_order_and_whole_file_digest_are_verified(self):
        manifest = self.fixture()
        manifest["assets"][0]["parts"].reverse()
        self.write_manifest(manifest)
        self.assertTrue(dump.verify_archive(self.output))

    def test_manifest_cannot_refer_to_a_file_outside_archive(self):
        manifest = self.fixture()
        outside = self.output.parent / "private.txt"
        outside.write_bytes(b"private data")
        manifest["assets"][0].update({
            "sha256": digest(b"private data"), "size": len(b"private data"),
            "parts": [{"path": "../private.txt", "sha256": digest(b"private data"), "size": len(b"private data")}],
        })
        self.write_manifest(manifest)
        self.assertTrue(dump.verify_archive(self.output))

    def test_windows_absolute_and_parent_paths_are_rejected(self):
        for unsafe in ("..\\private.txt", "C:\\private.txt", "/private.txt"):
            with self.subTest(path=unsafe):
                manifest = self.fixture()
                manifest["assets"][0]["parts"][0]["path"] = unsafe
                self.write_manifest(manifest)
                self.assertTrue(dump.verify_archive(self.output))

    def test_symlink_to_outside_archive_is_rejected(self):
        outside = self.output.parent / "outside"
        outside.mkdir()
        (outside / "private.txt").write_text("private", encoding="utf-8")
        try:
            (self.output / "escape").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Creating symbolic links is unavailable: {exc}")
        with self.assertRaises(ValueError):
            dump.safe_path(self.output, "escape/private.txt")

    @unittest.skipUnless(os.name == "nt", "Windows path prefix regression")
    def test_windows_resolve_prefix_does_not_change_containment(self):
        root = self.output.resolve()
        inside = Path("\\\\?\\" + str(root / "assets" / "file.bin"))
        with mock.patch.object(Path, "resolve", side_effect=[root, inside]):
            self.assertEqual(dump.safe_path(root, "assets/file.bin"), inside)
        outside = Path("\\\\?\\" + str(root.parent / "private.txt"))
        with mock.patch.object(Path, "resolve", side_effect=[root, outside]):
            with self.assertRaises(ValueError):
                dump.safe_path(root, "assets/file.bin")

    def test_metadata_corruption_cannot_pass_integrity_check(self):
        manifest = self.fixture()
        data = b'{"issues": []}'
        (self.output / "issues.json").write_bytes(data)
        manifest["metadata"] = [{"path": "issues.json", "sha256": digest(data), "size": len(data)}]
        self.write_manifest(manifest)
        self.assertEqual(dump.verify_archive(self.output), [])
        (self.output / "issues.json").write_bytes(b"truncated")
        self.assertTrue(dump.verify_archive(self.output))


class FixtureClient:
    """Exact public endpoint responses; no permissive unknown-URL fallback."""

    prefix = "/repos/owner/project"

    def __init__(self, populated=True):
        self.calls = []
        self.downloads = []
        self.api_failures = set()
        self.asset_failures = set()
        self.review_attachment = "https://github.com/user-attachments/files/11/review.txt"
        self.inline_attachment = "https://github.com/user-attachments/files/12/inline.txt"
        self.release_attachment = "https://github.com/owner/project/releases/download/v1/binary.bin"
        self.comment_attachment = "https://github.com/user-attachments/files/13/comment.txt"
        self.file_data = b"\x00payload\xffanother-block"
        self.single = {self.prefix: {"full_name": "owner/project"}}
        self.pages = {
            self.prefix + "/issues": [], self.prefix + "/pulls": [],
            self.prefix + "/releases": [], self.prefix + "/tags": [],
        }
        if populated:
            self.pages.update({
                self.prefix + "/issues": [
                    {"number": 1, "title": "Closed issue", "state": "closed", "body": "issue", "comments": 1},
                    {"number": 2, "title": "Closed pull", "state": "closed", "body": "pull", "comments": 1,
                     "pull_request": {"url": "https://api.github.com/repos/owner/project/pulls/2"}},
                ],
                self.prefix + "/issues/1/comments": [{"id": 11, "body": "ordinary discussion"}],
                self.prefix + "/issues/2/comments": [{"id": 12, "body": self.comment_attachment}],
                self.prefix + "/pulls": [{"number": 2, "state": "closed", "title": "Closed pull"}],
                self.prefix + "/pulls/2/reviews": [{"id": 21, "body": f"[review file]({self.review_attachment})"}],
                self.prefix + "/pulls/2/comments": [{"id": 22, "body": f'<img src="{self.inline_attachment}" />'}],
                self.prefix + "/pulls/2/commits": [{"sha": "a" * 40}],
                self.prefix + "/commits/" + "a" * 40 + "/comments": [{"id": 31, "body": "commit discussion"}],
                self.prefix + "/releases": [{"id": 7, "tag_name": "v1", "body": "release notes"}],
                self.prefix + "/releases/7/assets": [{"id": 71, "browser_download_url": self.release_attachment}],
                self.prefix + "/tags": [{"name": "v1", "commit": {"sha": "a" * 40}}],
            })
            self.single[self.prefix + "/pulls/2"] = {"number": 2, "body": "Full pull request", "state": "closed"}

    def lookup(self, path, source):
        self.calls.append(path)
        normalized = urlsplit(path).path
        if normalized in self.api_failures:
            raise RuntimeError("Fixture API unavailable")
        if normalized not in source:
            raise AssertionError(f"Unexpected endpoint: {path}")
        return source[normalized]

    def json(self, path):
        return self.lookup(path, self.single)

    def json_pages(self, path):
        return self.lookup(path, self.pages)

    def _open(self, url, accept="application/vnd.github+json"):
        self.downloads.append(url)
        if url in self.asset_failures:
            raise RuntimeError("Fixture attachment unavailable")
        return Response(self.file_data, {"Content-Type": "application/octet-stream", "Content-Length": str(len(self.file_data))}, url)


class ArchiveEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "archive"

    def run_archive(self, client):
        with mock.patch("builtins.print"):
            return dump.Archive("owner/project", self.output, client).run()

    def test_closed_issues_prs_reviews_and_release_assets_are_all_preserved(self):
        client = FixtureClient()
        with mock.patch.object(dump, "ASSET_PART_SIZE", 8):
            manifest = self.run_archive(client)
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["counts"]["issues"], 1)
        self.assertEqual(manifest["counts"]["pull_requests"], 1)
        self.assertEqual(manifest["counts"]["issue_comments"], 1)
        self.assertEqual(manifest["counts"]["pr_comments"], 1)
        self.assertEqual(manifest["counts"]["reviews"], 1)
        self.assertEqual(manifest["counts"]["review_comments"], 1)
        self.assertEqual(manifest["counts"]["commit_comments"], 1)
        self.assertEqual(manifest["counts"]["releases"], 1)
        self.assertEqual({asset["url"] for asset in manifest["assets"]}, {
            client.review_attachment, client.inline_attachment,
            client.release_attachment, client.comment_attachment,
        })
        for asset in manifest["assets"]:
            self.assertEqual(b"".join((self.output / part["path"]).read_bytes() for part in asset["parts"]), client.file_data)
            self.assertTrue(all(part["size"] <= 8 for part in asset["parts"]))
        self.assertEqual(dump.verify_archive(self.output), [])
        indexed = json.loads((self.output / "metadata/issues-index.json").read_text())
        self.assertEqual([row["state"] for row in indexed], ["closed", "closed"])
        self.assertTrue(all("state=all" in path for path in client.calls if path.split("?")[0] in (client.prefix + "/issues", client.prefix + "/pulls")))

    def test_empty_repository_with_metadata_and_no_assets_is_complete(self):
        manifest = self.run_archive(FixtureClient(populated=False))
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["assets"], [])
        self.assertTrue(manifest["metadata"])
        self.assertEqual(dump.verify_archive(self.output), [])

    def test_generated_release_archives_are_saved_without_uploaded_assets(self):
        client = FixtureClient(populated=False)
        urls = {f"https://api.github.com{client.prefix}/{kind}/v1" for kind in ("zipball", "tarball")}
        client.pages[client.prefix + "/releases"] = [{
            "id": 7, "tag_name": "v1", "body": "release notes",
            "zipball_url": f"https://api.github.com{client.prefix}/zipball/v1",
            "tarball_url": f"https://api.github.com{client.prefix}/tarball/v1",
        }]
        client.pages[client.prefix + "/releases/7/assets"] = []
        manifest = self.run_archive(client)
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["counts"]["release_source_archives"], 2)
        self.assertEqual({asset["url"] for asset in manifest["assets"]}, urls)
        self.assertEqual(set(client.downloads), urls)
        self.assertEqual(dump.verify_archive(self.output), [])

    def test_failed_release_source_archive_makes_snapshot_partial(self):
        client = FixtureClient(populated=False)
        url = f"https://api.github.com{client.prefix}/zipball/v1"
        client.pages[client.prefix + "/releases"] = [{"id": 7, "tag_name": "v1", "zipball_url": url}]
        client.pages[client.prefix + "/releases/7/assets"] = []
        client.asset_failures.add(url)
        manifest = self.run_archive(client)
        self.assertEqual(manifest["status"], "partial")
        self.assertTrue(any(failure.get("url") == url for failure in manifest["failures"]))
        self.assertTrue(dump.verify_archive(self.output))

    def test_release_source_archive_must_belong_to_source_repository(self):
        client = FixtureClient(populated=False)
        url = "https://api.github.com/repos/different/project/zipball/v1"
        client.pages[client.prefix + "/releases"] = [{"id": 7, "tag_name": "v1", "zipball_url": url}]
        client.pages[client.prefix + "/releases/7/assets"] = []
        manifest = self.run_archive(client)
        self.assertEqual(manifest["status"], "partial")
        self.assertNotIn(url, client.downloads)
        self.assertTrue(any(failure.get("format") == "zipball" for failure in manifest["failures"]))

    def test_api_failure_is_not_mistaken_for_an_empty_successful_repository(self):
        client = FixtureClient(populated=False)
        client.api_failures.add(client.prefix + "/issues")
        manifest = self.run_archive(client)
        self.assertEqual(manifest["status"], "partial")
        self.assertTrue(any(failure["kind"] == "api" for failure in manifest["failures"]))
        self.assertTrue(dump.verify_archive(self.output))

    def test_failed_review_endpoint_cannot_produce_complete_snapshot(self):
        client = FixtureClient()
        client.api_failures.add(client.prefix + "/pulls/2/reviews")
        manifest = self.run_archive(client)
        self.assertEqual(manifest["status"], "partial")
        self.assertTrue(any("/reviews" in failure.get("path", "") for failure in manifest["failures"]))

    def test_unavailable_attachment_is_recorded_as_partial(self):
        client = FixtureClient()
        client.asset_failures.add(client.review_attachment)
        manifest = self.run_archive(client)
        self.assertEqual(manifest["status"], "partial")
        self.assertTrue(any(failure.get("url") == client.review_attachment for failure in manifest["failures"]))
        self.assertEqual(len(manifest["assets"]), 3)

    def test_cached_asset_is_checked_and_corruption_is_repaired_on_rerun(self):
        client = FixtureClient(populated=False)
        client.single[client.prefix]["description"] = client.review_attachment
        first = self.run_archive(client)
        self.assertEqual(len(client.downloads), 1)
        self.run_archive(client)
        self.assertEqual(len(client.downloads), 1, "A verified cached file should not be downloaded again")
        part = self.output / first["assets"][0]["parts"][0]["path"]
        part.write_bytes(b"damaged cached bytes")
        repaired = self.run_archive(client)
        self.assertEqual(len(client.downloads), 2)
        self.assertEqual(repaired["status"], "complete")
        self.assertEqual(dump.verify_archive(self.output), [])

    def test_declared_content_length_mismatch_does_not_create_a_good_asset(self):
        client = FixtureClient(populated=False)
        client.single[client.prefix]["description"] = client.review_attachment
        client._open = lambda *args, **kwargs: Response(b"short", {"Content-Length": "99", "Content-Type": "application/octet-stream"})
        manifest = self.run_archive(client)
        self.assertEqual(manifest["status"], "partial")
        self.assertEqual(manifest["assets"], [])
        self.assertTrue(any("Truncated" in failure.get("error", "") for failure in manifest["failures"]))
        self.assertEqual(list((self.output / "assets").glob(".repo-archive-*")), [])

    def test_atomic_asset_publication_works_when_system_temp_is_on_another_volume(self):
        client = FixtureClient(populated=False)
        client.single[client.prefix]["description"] = client.review_attachment
        real_replace = os.replace
        replacements = []

        def separate_volumes(source, destination):
            # Model a system TEMP volume separate from the archive volume.
            # The filesystem rejects cross-volume rename instead of copying.
            source, destination = Path(source).resolve(), Path(destination).resolve()
            replacements.append((source, destination))
            if not source.is_relative_to(self.output.resolve()):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_replace(source, destination)

        with mock.patch.object(dump.os, "replace", side_effect=separate_volumes):
            manifest = self.run_archive(client)
        self.assertEqual(manifest["status"], "complete", manifest["failures"])
        self.assertTrue(replacements)
        self.assertEqual(dump.verify_archive(self.output), [])
        self.assertEqual(list((self.output / "assets").glob(".repo-archive-*")), [])


if __name__ == "__main__":
    unittest.main()
