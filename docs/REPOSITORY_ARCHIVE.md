# Archive the repository from a phone

The **Archive repository** workflow exports repository conversations and release
attachments into ordinary Git files on a separate `repository-archive` branch.
The code and workflow stay on the default branch. No paid service, personal access
token, Git LFS, or third-party Python package is required.

## Run from GitHub

1. Fork this repository, or use your own repository containing these tools and
   workflow on its default branch.
2. Open **Actions** in your fork. If GitHub asks you to enable workflows, enable
   them. On a phone, use GitHub in the browser; the desktop site option may make
   the controls easier to find.
3. Choose **Archive repository**, then **Run workflow**. Leave the default source
   `attogram/THE-ERROR-IS-THE-MESSAGE`, or enter another public `OWNER/REPOSITORY`.
4. Run the workflow and wait for all export, verification, and publication steps
   to succeed. The source repository receives no commits or comments.
5. Follow the archive link in the run summary, or select the
   **repository-archive** branch and open the **archive** directory.

The workflow uses only GitHub's automatic `GITHUB_TOKEN`. It writes to the
repository in which you run the workflow, including your fork; it does not push
to the source repository. No secrets need to be added. Organization policy or
branch rules may prevent the token from writing; those need to allow creation
and updates of the archive and temporary upload branches.

## What is preserved

The exporter saves API metadata and text for issues, issue comments, pull
requests, reviews, review comments and releases, plus the downloadable attachments
it discovers. `archive/manifest.json` records counts, source URLs, checksums,
attachment parts and failures. Follow the exported index and manifest to locate
the individual records and attachment files.

Files larger than 40 MiB are stored as numbered parts, each at most 40 MiB.
Parts are recorded in order with their sizes and SHA-256 hashes in the manifest;
concatenating them in that order reconstructs the original bytes. Preserve every
part and the manifest together. Verify the reconstructed bytes against the
asset-level size and SHA-256 before using them.

The archive includes a checksummed, standalone copy of `repo_dump.py`. After
cloning or downloading the archive branch, verify it or reconstruct an original:

```sh
python archive/repo_dump.py --verify archive
python archive/repo_dump.py --restore archive --asset FULL_SHA256_FROM_MANIFEST --destination restored/video.mp4
```

Restoration verifies the parts and the reconstructed original and refuses to
overwrite an existing destination. No network connection is needed.

This is a snapshot of accessible GitHub data, not a guarantee that deleted,
private, expired, or inaccessible attachments can be recovered. A missing download
or failed API request makes an export partial, and partial exports are **not
published**. The run logs and manifest identify the failure so it can be resolved
before rerunning. Optional **Save report artifact** retains only the small manifest
for seven days, including on failure, instead of duplicating all attachments in
Actions artifact storage.

## Safe publication and repeated runs

Publication checks the complete manifest and verifies file hashes again before
any upload. It constructs commits with a temporary Git index, so the checked-out
code, normal Git index and local branches are untouched. Existing archive branches
must contain the publisher's ownership marker; a coincidentally named user branch
is rejected.

New files are committed and pushed in batches containing at most 200 MiB of file
data. Each upload uses a unique `repository-archive-upload-...` branch. Only after
all batches are uploaded does the publisher move `repository-archive` forward and
read the remote reference back to confirm it. It never force-pushes. The unique
temporary branch is deleted afterward; if a network failure prevents cleanup,
the log gives its exact name for manual deletion.

Rerunning replaces the current archive directory with the newly verified snapshot,
including removal of files that are no longer part of it. Older snapshots remain
in Git history. Unchanged file contents reuse Git objects. If the resulting tree
is identical, no new archive commit is published. Failed exports leave the last
complete archive available. Interrupted batch uploads also leave the published
archive unchanged until the final update.

Only `manifest.json`, the generated `README.md`, and the metadata and attachment
parts named in the manifest are published. Unlisted local files and old download
cache files are ignored. Every listed path must be a safe relative path to an
existing regular file; symlinks, junctions and paths outside the archive are
rejected.

The archive uses ordinary repository storage, so GitHub repository limits and
your Actions usage policy still apply. Large or frequently changing video
collections will grow Git history even when old files disappear from the latest
snapshot. The workflow neither purchases storage nor configures billing.

## Run locally

Use Python 3.12 and Git from the code branch. For authenticated API access, set
`GITHUB_TOKEN` in your shell without saving it to the repository.

```sh
python tools/repo_dump.py --repo attogram/THE-ERROR-IS-THE-MESSAGE --output archive
python tools/repo_dump.py --verify archive
python tools/publish_archive.py --archive archive
```

The publisher defaults to a dry run. Review `git remote -v` and make sure `origin`
is your intended destination before explicitly publishing:

```sh
python tools/publish_archive.py --archive archive --push
```

The destination branch is fixed; there is no argument that can redirect the
publisher to `main` or another code branch. Fetching and dry-run preparation may
add objects to your local Git object database, but do not change tracked files,
stage changes, or update local branch references.

GitHub's [manual workflow documentation](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)
describes the required default-branch workflow and **Run workflow** control.
