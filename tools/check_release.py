"""Build and inspect distributions locally; never upload or install them."""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from hatchling.builders.sdist import SdistBuilder
from hatchling.builders.wheel import WheelBuilder

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from orca import __version__


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def check_wheel(path, cwd, *, minimal=False):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
        require(len(metadata) == 1, "wheel metadata is missing or duplicated")
        data = BytesParser().parsebytes(archive.read(metadata[0]))
        require(data["Name"] == "orca-local-mcp" and data["Version"] == __version__, "wheel version mismatch")
        require(data["Requires-Python"] == ">=3.11", "Python requirement mismatch")
        require({"Repository, https://github.com/karugamo5/Orca",
                 "Issues, https://github.com/karugamo5/Orca/issues"}
                <= set(data.get_all("Project-URL", [])), "public repository URLs mismatch")
        for entry, original in [("orca/LICENSE", ROOT / "LICENSE"),
                                ("orca/THIRD_PARTY_NOTICES.md", ROOT / "third_party/THIRD_PARTY_NOTICES.md")]:
            require(archive.read(entry) == original.read_bytes(), f"license mismatch: {entry}")
        entry_points = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        require("orca = orca.cli:main" in archive.read(entry_points).decode(), "CLI entry point missing")
    if minimal:
        return
    # -I removes user site, cwd and PYTHONPATH. Import the built wheel explicitly.
    for args, expected in [(["--version"], f"orca {__version__}\n"),
                           (["--license"], (ROOT / "LICENSE").read_text()),
                           (["launch", "--help"], "--model")]:
        code = "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); sys.argv[0]='orca'; runpy.run_module('orca',run_name='__main__')"
        result = subprocess.run([sys.executable, "-I", "-c", code, str(path), *args],
                                cwd=cwd, capture_output=True, text=True, timeout=15)
        require(result.returncode == 0 and not result.stderr, f"wheel CLI failed: {args}")
        require(result.stdout.isascii(), f"wheel CLI output is not English: {args}")
        require(expected in result.stdout, f"wheel CLI output mismatch: {args}")


def build_and_check(folder, *, minimal=False):
    require(re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", __version__),
            "release version must be MAJOR.MINOR.PATCH without leading zeros")
    artifacts = folder / "artifacts"
    artifacts.mkdir()
    wheel = Path(next(WheelBuilder(str(ROOT)).build(directory=str(artifacts))))
    sdist = Path(next(SdistBuilder(str(ROOT)).build(directory=str(artifacts))))
    extracted = folder / "extracted"
    extracted.mkdir()
    required = {"setup.sh", "uninstall.sh", "tools/build_macos.sh", "tools/prepare_macos.py",
                "macos/Package.swift", "requirements-lock.txt", "src/orca/cli.py",
                "README.md", "LICENSE", "third_party/THIRD_PARTY_NOTICES.md",
                "examples/tools.json", "examples/tools.example.json",
                ".github/workflows/ci.yml", ".github/workflows/release.yml",
                "tools/release.py", "PKG-INFO"}
    allowed = {"src", "macos", "tools", "examples", "third_party", ".github"}
    local_only = {"tools/check_package.py", "tools/check_swift_ipc.py", "tools/check_swift_ipc.sh",
                  "tools/ProtocolProbe.swift"}
    allowed_files = {"pyproject.toml", "requirements-lock.txt", "README.md", "LICENSE", "PKG-INFO",
                     ".gitignore", "setup.sh", "uninstall.sh"}
    seen = set()
    with tarfile.open(sdist) as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            require(not path.is_absolute() and ".." not in path.parts, "invalid archive path")
            require(member.isdir() or member.isfile(), "links are not allowed in the source archive")
            relative = PurePosixPath(*path.parts[1:])
            if member.isdir():
                continue
            require(bool(relative.parts), "invalid archive member")
            require(relative.parts[0] in allowed or str(relative) in allowed_files,
                    f"unexpected source archive entry: {relative}")
            require(not str(relative).startswith("macos/Tests/") and str(relative) not in local_only,
                    f"local validation source in source archive: {relative}")
            require(relative.suffix != ".md" or str(relative) in {"README.md", "third_party/THIRD_PARTY_NOTICES.md"},
                    f"private documentation in source archive: {relative}")
            require(not any((part.startswith(".") and part not in {".github", ".gitignore"})
                            or part == "__pycache__" for part in relative.parts)
                    and relative.suffix != ".pyc", f"local artifact in source archive: {relative}")
            seen.add(str(relative))
            destination = extracted.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source:
                destination.write_bytes(source.read())
            if str(relative) == "PKG-INFO":
                metadata = BytesParser().parsebytes(destination.read_bytes())
                require(metadata["Name"] == "orca-local-mcp" and metadata["Version"] == __version__,
                        "sdist version mismatch")
            destination.chmod(member.mode & 0o777)
    require(required <= seen, f"source archive is incomplete: {required - seen}")
    source = next(extracted.iterdir())
    require(os.access(source / "setup.sh", os.X_OK), "setup.sh is not executable")
    check_wheel(wheel, folder, minimal=minimal)
    if minimal:
        return artifacts
    rebuilt = folder / "rebuilt"
    rebuilt.mkdir()
    rebuilt_wheel = Path(next(WheelBuilder(str(source)).build(directory=str(rebuilt))))
    check_wheel(rebuilt_wheel, folder)
    require(wheel.read_bytes() == rebuilt_wheel.read_bytes(), "sdist rebuild differs from source wheel")
    return artifacts


def main():
    parser = argparse.ArgumentParser(description="Build and inspect distributions in the project (no upload or installation)")
    parser.add_argument("--output-dir", type=Path, help="Output directory for verified wheel, sdist and SHA256SUMS (within the project)")
    parser.add_argument("--minimal", action="store_true", help="Check packaging metadata and contents only; skip CLI execution and rebuild comparison")
    args = parser.parse_args()
    output = args.output_dir.resolve() if args.output_dir else None
    if output and not output.is_relative_to(ROOT):
        parser.error("The output directory must be within the project")
    test_root = ROOT / ".test-runs"
    test_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-", dir=test_root) as temporary:
        artifacts = build_and_check(Path(temporary), minimal=args.minimal)
        if output:
            output.mkdir(parents=True, exist_ok=True)
            checksums = []
            for path in sorted(artifacts.iterdir()):
                shutil.copy2(path, output / path.name)
                checksums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n")
            (output / "SHA256SUMS").write_text("".join(checksums))
    checks = "wheel / sdist / version / license" if args.minimal else "wheel / sdist / sdist rebuild / CLI / license"
    print(f"Orca {__version__}: {checks} checks passed")


if __name__ == "__main__":
    main()
