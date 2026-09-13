#!/usr/bin/env python3
"""Export GitHub repository conversations and release assets; verify every byte.

Python standard library only. Remote text is archived as data, never executed.
"""
import argparse
import concurrent.futures
import hashlib
import html
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, HTTPRedirectHandler, build_opener

ASSET_PART_SIZE = 40 * 1024 * 1024
GITHUB_ASSET_HOSTS = {"github-production-user-asset-6210df.s3.amazonaws.com"}
REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
URL_PATTERN = re.compile(r'https://[^\s<>"\x27`\\]+')


def is_allowed_download(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return (parsed.scheme == "https" and parsed.username is None
                and parsed.password is None and parsed.port in (None, 443)
                and (host in {"github.com", "api.github.com", "codeload.github.com", "githubusercontent.com"} | GITHUB_ASSET_HOSTS
                     or host.endswith(".githubusercontent.com")))
    except (ValueError, TypeError, AttributeError):
        return False


class SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_allowed_download(newurl):
            raise ValueError("Refused redirect outside HTTPS GitHub asset hosts")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urlparse(req.full_url).netloc != urlparse(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


class GitHubClient:
    def __init__(self, token=None, opener=None):
        self.token = token if token is not None else os.environ.get("GH_TOKEN", os.environ.get("GITHUB_TOKEN", ""))
        self.opener = opener or build_opener(SafeRedirectHandler())

    def _open(self, url, accept="application/vnd.github+json"):
        if not is_allowed_download(url):
            raise ValueError("Refused URL outside HTTPS GitHub asset hosts")
        headers = {"User-Agent": "repository-archive/1.0", "Accept": accept}
        if urlparse(url).hostname == "api.github.com":
            headers["X-GitHub-Api-Version"] = "2022-11-28"
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
        for attempt in range(4):
            try:
                return self.opener.open(Request(url, headers=headers), timeout=90)
            except HTTPError as exc:
                if attempt == 3 or exc.code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"HTTP {exc.code} downloading {urlparse(url).path}") from None
                retry = exc.headers.get("Retry-After", "")
                time.sleep(min(30, int(retry)) if retry.isdigit() else 2 ** attempt)
            except URLError:
                if attempt == 3:
                    raise RuntimeError(f"Network failure downloading {urlparse(url).path}") from None
                time.sleep(2 ** attempt)

    @staticmethod
    def api_url(path):
        url = path if path.startswith("https://") else "https://api.github.com" + path
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com" or parsed.username:
            raise ValueError("Refused non-GitHub API pagination URL")
        return url

    def json(self, path):
        with self._open(self.api_url(path)) as response:
            return json.load(response)

    def json_pages(self, path):
        url = self.api_url(path)
        url += ("&" if "?" in url else "?") + "per_page=100"
        records, visited = [], set()
        while url:
            if url in visited:
                raise ValueError("Repeated pagination URL")
            visited.add(url)
            with self._open(self.api_url(url)) as response:
                page = json.load(response)
                if not isinstance(page, list):
                    raise ValueError("Expected a JSON array from paginated endpoint")
                records.extend(page)
                links = response.headers.get("Link", "")
            match = re.search(r'<([^>]+)>;\s*rel="next"', links)
            url = self.api_url(match.group(1)) if match else None
        return records


def extract_attachment_urls(value):
    found = set()
    if isinstance(value, dict):
        for item in value.values():
            found.update(extract_attachment_urls(item))
    elif isinstance(value, list):
        for item in value:
            found.update(extract_attachment_urls(item))
    elif isinstance(value, str):
        for match in URL_PATTERN.findall(html.unescape(value)):
            url = match.rstrip(".,;:!?")
            while url.endswith(")") and url.count(")") > url.count("("):
                url = url[:-1]
            url = url.rstrip("]}")
            if not is_allowed_download(url):
                continue
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            # Documentation can contain example URLs such as /assets/xxxx.
            # Uploaded assets use UUIDs; named uploads use a numeric file ID.
            # Keep examples in the raw text, but do not invent files for them.
            is_upload = (host == "github.com" and
                         (re.fullmatch(r"/user-attachments/assets/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}/?", parsed.path) or
                          re.match(r"/user-attachments/files/[0-9]+/[^/]+", parsed.path) or
                          re.match(r"/[^/]+/[^/]+/(files|assets)/", parsed.path)))
            if is_allowed_download(url) and (is_upload or
                    (host.endswith(".githubusercontent.com") and not host.startswith("avatars"))):
                found.add(url)
    return found


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_path(root, relative):
    root = Path(root).resolve()
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ValueError("Invalid archive path")
    child = (root / relative).resolve()
    def comparable(path):
        # Windows may retain the extended-length prefix when another worker
        # creates a missing parent during resolve(). It is the same resolved
        # drive/UNC path, but pathlib treats the two anchors as different.
        value = str(path)
        if os.name == "nt":
            if value.startswith("\\\\?\\UNC\\"):
                value = "\\\\" + value[8:]
            elif value.startswith("\\\\?\\") and re.match(r"[A-Za-z]:\\", value[4:]):
                value = value[4:]
        return Path(value)

    checked_root, checked_child = comparable(root), comparable(child)
    if checked_child == checked_root or not checked_child.is_relative_to(checked_root):
        raise ValueError("Archive path escapes output directory")
    return child


def verify_asset(root, asset):
    errors, combined, size = [], hashlib.sha256(), 0
    if not asset.get("parts"):
        return [f"Asset has no parts: {asset.get('url', '?')}"]
    for part in asset["parts"]:
        try:
            path = safe_path(root, part["path"])
            digest, count = hashlib.sha256(), 0
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                    combined.update(chunk)
                    count += len(chunk)
            size += count
            if count != part["size"] or digest.hexdigest() != part["sha256"]:
                errors.append(f"Corrupt asset part: {part['path']}")
        except (OSError, ValueError, KeyError) as exc:
            errors.append(f"Unreadable asset part: {part.get('path', '?')}: {exc}")
    if size != asset.get("size") or combined.hexdigest() != asset.get("sha256"):
        errors.append(f"Asset reconstruction mismatch: {asset.get('url', '?')}")
    return errors


def verify_archive(output):
    output = Path(output)
    try:
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"Cannot read manifest: {exc}"]
    errors = []
    if manifest.get("status") != "complete" or manifest.get("failures"):
        errors.append("Snapshot is incomplete; inspect manifest failures")
    if not manifest.get("metadata"):
        errors.append("Snapshot has no metadata records")
    for entry in manifest.get("metadata", []):
        try:
            path = safe_path(output, entry["path"])
            if path.stat().st_size != entry["size"] or sha256_file(path) != entry["sha256"]:
                errors.append(f"Metadata checksum mismatch: {entry['path']}")
        except (OSError, ValueError, KeyError) as exc:
            errors.append(f"Unreadable metadata: {entry.get('path', '?')}: {exc}")
    for asset in manifest.get("assets", []):
        errors.extend(verify_asset(output, asset))
    return errors


def restore_asset(output, digest, destination):
    """Reconstruct a selected original without overwriting an existing file."""
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("--asset must be the full SHA-256 from manifest.json")
    output, destination = Path(output), Path(destination)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    asset = next((item for item in manifest.get("assets", []) if item["sha256"] == digest), None)
    if asset is None:
        raise ValueError("No matching asset in the manifest")
    errors = verify_asset(output, asset)
    if errors:
        raise ValueError("Cannot restore corrupt asset: " + "; ".join(errors))
    destination.parent.mkdir(parents=True, exist_ok=True)
    restored_hash, size = hashlib.sha256(), 0
    # Exclusive creation prevents accidental replacement of a user's file.
    with destination.open("xb") as target:
        for part in asset["parts"]:
            with safe_path(output, part["path"]).open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    target.write(chunk)
                    restored_hash.update(chunk)
                    size += len(chunk)
    if restored_hash.hexdigest() != digest or size != asset["size"]:
        raise ValueError("Restoration did not match the expected original; destination is incomplete")
    return destination


class Archive:
    def __init__(self, repo, output, client):
        if not REPO_PATTERN.fullmatch(repo) or any(part in (".", "..") for part in repo.split("/")):
            raise ValueError("Repository must be OWNER/REPO")
        self.repo, self.output, self.client = repo, Path(output), client
        self.output.mkdir(parents=True, exist_ok=True)
        self.output = self.output.resolve()
        (self.output / "assets").mkdir(exist_ok=True)
        self._asset_write_lock = threading.Lock()
        self.prefix = "/repos/" + repo
        self.failures, self.metadata, self.urls = [], [], set()
        self.counts = {"issues": 0, "pull_requests": 0, "issue_comments": 0,
                       "pr_comments": 0, "reviews": 0, "review_comments": 0,
                       "commit_comments": 0, "releases": 0, "tags": 0}
        self.cache = {}
        try:
            old = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
            if old.get("repository") == repo:
                self.cache = {item["url"]: item for item in old.get("assets", [])}
        except (OSError, ValueError, KeyError):
            pass

    def save(self, relative, value):
        path = safe_path(self.output, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        path.write_bytes(data)
        self.metadata.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
        self.urls.update(extract_attachment_urls(value))
        return value

    def collect(self, endpoint, relative, single=False):
        try:
            data = self.client.json(self.prefix + endpoint) if single else self.client.json_pages(self.prefix + endpoint)
            return self.save(relative, data)
        except Exception as exc:
            self.failures.append({"kind": "api", "path": self.prefix + endpoint, "error": str(exc)})
            return {} if single else []

    def _download(self, url):
        cached = self.cache.get(url)
        if cached and not verify_asset(self.output, cached):
            return cached
        # Atomic publication cannot cross volumes (for example Windows TEMP on
        # C: with the archive on D:). Stage beside the final assets instead.
        temporary = Path(tempfile.mkdtemp(prefix=".repo-archive-", dir=safe_path(self.output, "assets")))
        try:
            chunks, total, digest = [], 0, hashlib.sha256()
            with self.client._open(url, "application/octet-stream") as response:
                content_type = response.headers.get("Content-Type", "application/octet-stream").split(";")[0]
                if content_type == "text/html" and "attachment" not in response.headers.get("Content-Disposition", "").lower():
                    raise ValueError("Received HTML instead of an attachment")
                length = response.headers.get("Content-Length")
                suffix = Path(urlparse(url).path).suffix.lower()
                if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
                    suffix = mimetypes.guess_extension(content_type) or ".bin"
                eof = False
                while not eof:
                    path = temporary / str(len(chunks))
                    part_hash, part_size = hashlib.sha256(), 0
                    with path.open("wb") as stream:
                        while part_size < ASSET_PART_SIZE:
                            chunk = response.read(min(1024 * 1024, ASSET_PART_SIZE - part_size))
                            if not chunk:
                                eof = True
                                break
                            stream.write(chunk)
                            digest.update(chunk)
                            part_hash.update(chunk)
                            part_size += len(chunk)
                            total += len(chunk)
                    if part_size or not chunks:
                        chunks.append((path, part_size, part_hash.hexdigest()))
                if length is not None and int(length) != total:
                    raise ValueError(f"Truncated attachment: expected {length} bytes, got {total}")
            checksum, parts = digest.hexdigest(), []
            for index, (source, count, part_sha) in enumerate(chunks):
                ending = suffix if len(chunks) == 1 else f"{suffix}.part{index:04d}"
                relative = f"assets/{checksum[:2]}/{checksum}{ending}"
                # Distinct URLs can contain identical bytes and share a target.
                # Serialize publication to avoid Windows sharing violations.
                with self._asset_write_lock:
                    target = safe_path(self.output, relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, target)
                parts.append({"path": relative, "size": count, "sha256": part_sha})
            return {"url": url, "sha256": checksum, "size": total, "content_type": content_type, "parts": parts}
        finally:
            shutil.rmtree(temporary)

    def run(self):
        started = datetime.now(timezone.utc).isoformat()
        repo_info = self.collect("", "metadata/repository.json", single=True)
        all_issues = self.collect("/issues?state=all&sort=created&direction=asc", "metadata/issues-index.json")
        for item in all_issues:
            number = item["number"]
            is_pr = "pull_request" in item
            folder = f"metadata/{'pulls' if is_pr else 'issues'}/{number}"
            self.save(folder + "/issue.json", item)
            comments = self.collect(f"/issues/{number}/comments", folder + "/comments.json") if item.get("comments") else self.save(folder + "/comments.json", [])
            self.counts["pr_comments" if is_pr else "issue_comments"] += len(comments)
            if not is_pr:
                self.counts["issues"] += 1
        pulls = self.collect("/pulls?state=all&sort=created&direction=asc", "metadata/pulls-index.json")
        seen_prs = {item["number"] for item in all_issues if "pull_request" in item}
        seen_commits = set()
        for pull in pulls:
            number, folder = pull["number"], f"metadata/pulls/{pull['number']}"
            self.counts["pull_requests"] += 1
            self.collect(f"/pulls/{number}", folder + "/pull.json", single=True)
            if number not in seen_prs:
                comments = self.collect(f"/issues/{number}/comments", folder + "/comments.json")
                self.counts["pr_comments"] += len(comments)
            for endpoint, name in [("reviews", "reviews"), ("comments", "review_comments")]:
                records = self.collect(f"/pulls/{number}/{endpoint}", folder + f"/{name}.json")
                self.counts[name] += len(records)
            commits = self.collect(f"/pulls/{number}/commits", folder + "/commits.json")
            for commit in commits:
                sha = commit.get("sha", "")
                if re.fullmatch(r"[a-f0-9]{40}", sha) and sha not in seen_commits:
                    seen_commits.add(sha)
                    comments = self.collect(f"/commits/{sha}/comments", f"metadata/commit-comments/{sha}.json")
                    self.counts["commit_comments"] += len(comments)
        releases = self.collect("/releases", "metadata/releases.json")
        self.counts["releases"] = len(releases)
        self.counts["tags"] = len(self.collect("/tags", "metadata/tags.json"))
        self.counts["release_source_archives"] = 0
        for release in releases:
            # GitHub's generated Source code (zip/tar.gz) downloads appear on
            # release pages but are not returned by the release assets endpoint.
            for archive_format in ("zipball", "tarball"):
                url = release.get(archive_format + "_url")
                if not url:
                    continue  # Draft releases can have no source archive yet.
                parsed = urlparse(url) if is_allowed_download(url) else None
                expected_path = f"{self.prefix}/{archive_format}/"
                if (parsed is not None and parsed.hostname == "api.github.com"
                        and parsed.path.casefold().startswith(expected_path.casefold())
                        and len(parsed.path) > len(expected_path)):
                    self.urls.add(url)
                    self.counts["release_source_archives"] += 1
                else:
                    self.failures.append({"kind": "asset", "error": "Unsupported release source archive URL",
                                          "release_id": release.get("id"), "format": archive_format})
            assets = self.collect(f"/releases/{release['id']}/assets", f"metadata/releases/{release['id']}-assets.json")
            for asset in assets:
                url = asset.get("browser_download_url", "")
                if is_allowed_download(url):
                    self.urls.add(url)
                else:
                    self.failures.append({"kind": "asset", "error": "Missing or unsupported release asset URL", "asset_id": asset.get("id")})
        print(f"Collected metadata; downloading/verifying {len(self.urls)} distinct attachments", flush=True)
        assets = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as workers:
            futures = {workers.submit(self._download, url): url for url in sorted(self.urls)}
            for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
                try:
                    assets.append(future.result())
                except Exception as exc:
                    self.failures.append({"kind": "asset", "url": futures[future], "error": str(exc)})
                if index % 25 == 0 or index == len(futures):
                    print(f"Attachments processed: {index}/{len(futures)}; failures: {len(self.failures)}", flush=True)
        self.counts.update({"assets": len(assets), "asset_bytes": sum(item["size"] for item in assets)})
        # The archive stays verifiable and restorable after it is copied away
        # from this repository. This is our own source, never remote code.
        script = Path(__file__).read_bytes()
        (self.output / "repo_dump.py").write_bytes(script)
        self.metadata.append({"path": "repo_dump.py", "sha256": hashlib.sha256(script).hexdigest(), "size": len(script)})
        manifest = {"format_version": 1, "repository": self.repo, "started_at": started,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "status": "partial" if self.failures else "complete", "counts": self.counts,
                    "metadata": self.metadata, "assets": sorted(assets, key=lambda item: item["url"]),
                    "failures": self.failures,
                    "scope": "All accessible open/closed issues, PR conversations/reviews/commit comments, releases, tags, and GitHub-hosted attachments. This is a time-bounded API snapshot, not an atomic Git mirror. Deleted or inaccessible source material cannot be recovered."}
        (self.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.write_index(manifest, repo_info)
        return manifest

    def write_index(self, manifest, repo_info):
        lines = [f"# Repository archive: {self.repo}", "", f"Status: **{manifest['status']}**", "",
                 f"Snapshot: {manifest['started_at']} — {manifest['finished_at']}", "",
                 manifest["scope"], "", "## Contents", ""]
        lines.extend(f"- {key}: {value}" for key, value in manifest["counts"].items())
        lines += ["", "[Raw metadata and checksums](manifest.json)", "", "## Conversations", ""]
        for entry in self.metadata:
            if entry["path"].endswith(("/issue.json", "/pull.json")):
                lines.append(f"- [{entry['path']}]({entry['path']})")
        lines += ["", "## Attachments", "", "Large attachments are split into ordered parts below 40 MiB; manifest checksums verify both parts and the reconstructed original.", ""]
        for asset in manifest["assets"]:
            links = " · ".join(f"[part {i + 1}]({part['path']})" for i, part in enumerate(asset["parts"]))
            lines.append(f"- {asset['sha256'][:16]} — {asset['size']} bytes — {links}")
        if self.failures:
            lines += ["", "## Failures", "", "This archive is incomplete. See manifest.json for every failure; do not treat it as a complete backup."]
        (self.output / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="Source OWNER/REPO")
    parser.add_argument("--output", default="archive")
    parser.add_argument("--verify", metavar="ARCHIVE", help="Verify metadata, every part, and reconstructed assets")
    parser.add_argument("--restore", metavar="ARCHIVE", help="Restore one original attachment from archived parts")
    parser.add_argument("--asset", help="Full asset SHA-256 to restore")
    parser.add_argument("--destination", help="New file path for the restored original")
    args = parser.parse_args()
    if args.restore:
        if not args.asset or not args.destination or args.verify:
            parser.error("--restore requires --asset and --destination, and cannot be combined with --verify")
        restored = restore_asset(args.restore, args.asset, args.destination)
        print(f"Restored and verified {restored}")
        return 0
    if args.verify:
        errors = verify_archive(args.verify)
        for error in errors:
            print(error, file=sys.stderr)
        print("Archive verified" if not errors else f"Verification failed: {len(errors)} errors")
        return bool(errors)
    if not args.repo:
        parser.error("--repo is required unless --verify is used")
    manifest = Archive(args.repo, args.output, GitHubClient()).run()
    for failure in manifest["failures"][:20]:
        print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
    if len(manifest["failures"]) > 20:
        print("Additional failures are recorded in manifest.json", file=sys.stderr)
    print(json.dumps({"status": manifest["status"], "counts": manifest["counts"], "failures": len(manifest["failures"])}))
    return manifest["status"] != "complete"


if __name__ == "__main__":
    raise SystemExit(main())
