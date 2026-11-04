"""Helpers for the manual GitHub Release workflow (stdlib only)."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

VERSION = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"


def validate_version(version):
    if not re.fullmatch(VERSION, version):
        raise ValueError("Version must be MAJOR.MINOR.PATCH without leading zeros")


def command(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def resolve_commit(source, commit):
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit):
        raise ValueError("Commit must be a 7-40 digit hexadecimal SHA")
    matches = command("git", "rev-parse", "--disambiguate=" + commit.lower(), cwd=source).splitlines()
    if len(matches) != 1 or command("git", "cat-file", "-t", matches[0], cwd=source) != "commit":
        raise ValueError("Commit SHA is missing, ambiguous, or does not identify a commit")
    return matches[0]


def set_version(source, version):
    validate_version(version)
    path = source / "src/orca/__init__.py"
    updated, count = re.subn(r"^__version__\s*=.*$", f'__version__ = "{version}"',
                             path.read_text(), flags=re.MULTILINE)
    if count != 1:
        raise ValueError("Source must define __version__ exactly once")
    path.write_text(updated)


def verify_artifacts(folder, version):
    validate_version(version)
    names = {f"orca_local_mcp-{version}-py3-none-any.whl", f"orca_local_mcp-{version}.tar.gz"}
    if {path.name for path in folder.iterdir()} != names | {"SHA256SUMS"}:
        raise ValueError("Release must contain exactly the versioned wheel, sdist and SHA256SUMS")
    if any(path.is_symlink() or not path.is_file() for path in folder.iterdir()):
        raise ValueError("Release assets must be regular files")
    checksums = {}
    for line in (folder / "SHA256SUMS").read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match or match[2] not in names or match[2] in checksums:
            raise ValueError("Invalid or duplicate checksum entry")
        checksums[match[2]] = match[1]
    if set(checksums) != names:
        raise ValueError("Checksums must cover both release distributions")
    for name, digest in checksums.items():
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Release checksum mismatch")
    return [folder / name for name in sorted(names | {"SHA256SUMS"})]


def api(repository, endpoint, *args):
    return json.loads(command("gh", "api", f"repos/{repository}/{endpoint}", *args))


def tag_commit(repository, tag):
    refs = api(repository, "git/matching-refs/tags/" + tag)
    refs = [ref for ref in refs if ref["ref"] == "refs/tags/" + tag]
    if not refs:
        return None
    obj = refs[0]["object"]
    for _ in range(10):
        if obj["type"] == "commit":
            return obj["sha"]
        if obj["type"] != "tag":
            break
        obj = api(repository, "git/tags/" + obj["sha"])["object"]
    raise ValueError("Release tag does not resolve to a commit")


def publish(repository, commit, version, folder):
    validate_version(version)
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Publishing requires a full commit SHA")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid GitHub repository")
    assets = verify_artifacts(folder, version)
    tag = "v" + version
    # Listing with pagination distinguishes absence from auth/network errors and
    # includes drafts. Any existing release requires deliberate manual recovery.
    pages = api(repository, "releases?per_page=100", "--paginate", "--slurp")
    if any(release["tag_name"] == tag for page in pages for release in page):
        raise ValueError("Release already exists; existing releases and drafts are never overwritten")
    existing = tag_commit(repository, tag)
    if existing is not None and existing != commit:
        raise ValueError("Release tag points to a different commit")
    if existing is None:
        api(repository, "git/refs", "--method", "POST", "-f", "ref=refs/tags/" + tag,
            "-f", "sha=" + commit)
    notes = (f"Orca {version}\n\nSource commit: `{commit}`\n\n"
             f"The attached wheel and sdist use version {version}, set during CI. "
             "The tag points to the original commit; GitHub's Source code archives "
             "retain that commit's original version.\n\n"
             "Install on macOS 13+ with Python 3.11+, Swift tools 5.9+ and Git:\n\n"
             "```sh\ngit clone https://github.com/karugamo5/Orca.git\ncd Orca\n./setup.sh\n```\n\n"
             "The installer fetches the latest stable release and verifies SHA256SUMS. "
             "A model server must be supplied separately. This is an alpha release. "
             "No signed or notarized app binaries are provided.\n")
    with tempfile.TemporaryDirectory(prefix="release-notes-", dir=folder.parent) as temporary:
        body = Path(temporary) / "notes.md"
        body.write_text(notes)
        command("gh", "release", "create", tag, "--repo", repository, "--verify-tag",
                "--target", commit, "--title", "Orca " + version, "--draft", "--notes-file", str(body))
    # No --clobber: an upload failure leaves an unpublished draft for inspection.
    command("gh", "release", "upload", tag, "--repo", repository, *map(str, assets))
    release = json.loads(command("gh", "release", "view", tag, "--repo", repository,
                                 "--json", "assets,isDraft"))
    expected = {path.name: path.stat().st_size for path in assets}
    actual = {asset["name"]: asset["size"] for asset in release["assets"]}
    if (not release["isDraft"] or len(release["assets"]) != len(assets) or actual != expected
            or tag_commit(repository, tag) != commit):
        raise ValueError("Draft assets or release tag changed; refusing to publish")
    command("gh", "release", "edit", tag, "--repo", repository, "--draft=false", "--latest")
    return f"https://github.com/{repository}/releases/tag/{tag}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    resolve = subparsers.add_parser("resolve", allow_abbrev=False)
    resolve.add_argument("--commit", required=True)
    resolve.add_argument("--version", required=True)
    resolve.add_argument("--source", type=Path, default=Path.cwd())
    version = subparsers.add_parser("set-version", allow_abbrev=False)
    version.add_argument("--version", required=True)
    version.add_argument("--source", type=Path, default=Path.cwd())
    release = subparsers.add_parser("publish", allow_abbrev=False)
    release.add_argument("--repository", required=True)
    release.add_argument("--commit", required=True)
    release.add_argument("--version", required=True)
    release.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    try:
        validate_version(args.version)
        if args.operation == "resolve":
            commit = resolve_commit(args.source, args.commit)
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                output.write(f"commit={commit}\nversion={args.version}\n")
            print(f"Resolved commit {commit} for Orca {args.version}")
        elif args.operation == "set-version":
            set_version(args.source, args.version)
        else:
            url = publish(args.repository, args.commit, args.version, args.artifacts.resolve())
            summary = f"Orca {args.version}\n\nSource commit: `{args.commit}`\n\n{url}\n"
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as output:
                output.write(summary)
            print(url)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        message = "Git or GitHub command failed; no existing release was overwritten" if isinstance(
            error, subprocess.CalledProcessError) else str(error)
        parser.exit(1, "Release failed: " + message + "\n")


if __name__ == "__main__":
    main()
