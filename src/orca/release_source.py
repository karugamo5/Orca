"""Fetch a published source distribution for the explicit setup command only."""
import ast
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from urllib.error import URLError
from urllib.request import Request, urlopen

REPOSITORY = "karugamo5/Orca"
LATEST = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
VERSION = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"


class ReleaseSourceError(Exception):
    MESSAGES = {
        "download": "Could not download the latest GitHub Release. Check the connection; private repositories require GitHub CLI authentication (gh auth login). Use --source only to install the local checkout",
        "metadata": "The latest GitHub Release does not contain the expected source distribution and checksums",
        "checksum": "The source distribution checksum does not match SHA256SUMS",
        "archive": "The source distribution has an invalid or unsupported archive layout",
        "version": "The source version does not match the GitHub Release version",
    }

    def __init__(self, code):
        super().__init__(self.MESSAGES.get(code, self.MESSAGES["metadata"]))


def source_version(source: Path):
    try:
        values = [ast.literal_eval(node.value) for node in ast.parse(
            (source / "src/orca/__init__.py").read_text()).body
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)]
        if len(values) != 1 or not isinstance(values[0], str) or not re.fullmatch(VERSION, values[0]):
            raise ValueError()
        return values[0]
    except (OSError, ValueError, SyntaxError, UnicodeError, TypeError, RecursionError):
        raise ReleaseSourceError("version") from None


def download(url, limit):
    accept = "application/octet-stream" if "/releases/assets/" in url else "application/vnd.github+json"
    request = Request(url, headers={"User-Agent": "Orca-setup", "Accept": accept})
    try:
        with urlopen(request, timeout=30) as response:
            data = response.read(limit + 1)
        if len(data) > limit:
            raise ReleaseSourceError("download")
        return data
    except (OSError, URLError, ValueError):
        # gh keeps authentication opaque; never read, store, or print its token.
        executable = shutil.which("gh")
        if executable and url.startswith(f"https://api.github.com/repos/{REPOSITORY}/"):
            try:
                result = subprocess.run([executable, "api", "--hostname", "github.com",
                                         url.removeprefix("https://api.github.com/"), "-H", "Accept: " + accept],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, timeout=30, check=True)
                if len(result.stdout) <= limit:
                    return result.stdout
            except (OSError, subprocess.SubprocessError):
                pass
        raise ReleaseSourceError("download") from None


def extract_source(archive: Path, destination: Path):
    """Extract regular files only, with bounded size and no links or traversal."""
    try:
        with tarfile.open(archive, "r:gz") as stream:
            members = []
            seen = set()
            total = 0
            for member in stream:
                path = PurePosixPath(member.name)
                total += member.size
                if ((not member.isdir() and not member.isfile()) or path.is_absolute()
                        or ".." in path.parts or not path.parts or len(members) >= 4096
                        or member.size < 0 or total > 256 * 1024 * 1024
                        or path in seen):
                    raise ReleaseSourceError("archive")
                seen.add(path)
                members.append((member, path))
            roots = {path.parts[0] for _, path in members}
            if len(roots) != 1:
                raise ReleaseSourceError("archive")
            for member, path in members:
                target = destination.joinpath(*path.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with stream.extractfile(member) as source, target.open("xb") as output:
                        while chunk := source.read(1024 * 1024):
                            output.write(chunk)
                    target.chmod(0o700 if member.mode & 0o111 else 0o600)
        source = destination / next(iter(roots))
        required = ("src/orca/__init__.py", "pyproject.toml", "requirements-lock.txt",
                    "setup.sh", "tools/build_macos.sh", "examples/config.json", "examples/models.json")
        if not all((source / name).is_file() for name in required):
            raise ReleaseSourceError("archive")
        return source
    except (OSError, ValueError, tarfile.TarError, EOFError):
        raise ReleaseSourceError("archive") from None


def latest_source(directory: Path, *, fetch=download):
    try:
        release = json.loads(fetch(LATEST, 1024 * 1024))
        tag = release["tag_name"]
        if (not isinstance(tag, str) or not re.fullmatch("v" + VERSION, tag)
                or release.get("draft") is not False or release.get("prerelease") is not False):
            raise ValueError()
        version = tag[1:]
        filename = f"orca_local_mcp-{version}.tar.gz"

        def asset_url(name):
            assets = [asset for asset in release["assets"] if asset["name"] == name]
            if len(assets) != 1 or type(assets[0]["id"]) is not int or assets[0]["id"] <= 0:
                raise ValueError()
            return f"https://api.github.com/repos/{REPOSITORY}/releases/assets/{assets[0]['id']}"

        archive_url = asset_url(filename)
        checksums = fetch(asset_url("SHA256SUMS"), 1024 * 1024).decode("ascii")
        matches = re.findall(r"^([0-9a-f]{64})  " + re.escape(filename) + r"$", checksums, re.MULTILINE)
        if len(matches) != 1:
            raise ValueError()
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise ReleaseSourceError("metadata") from None
    data = fetch(archive_url, 64 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != matches[0]:
        raise ReleaseSourceError("checksum")
    archive = directory / filename
    archive.write_bytes(data)
    source = extract_source(archive, directory / "source")
    if source_version(source) != version:
        raise ReleaseSourceError("version")
    return source, version
