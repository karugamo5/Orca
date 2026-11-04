"""Write versioned bundle metadata and licenses without installing the app."""
import ast
import plistlib
import shutil
import sys
from pathlib import Path


def prepare_bundle(source, app):
    namespace = {}
    # __init__ only defines the version; parse the literal without importing Orca.
    for node in ast.parse((source / "src/orca/__init__.py").read_text()).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets):
            namespace["version"] = ast.literal_eval(node.value)
    version = namespace["version"]
    metadata = plistlib.loads((source / "macos/Info.plist").read_bytes())
    metadata.update(CFBundleVersion=version, CFBundleShortVersionString=version)
    contents = app / "Contents"
    resources = contents / "Resources"
    resources.mkdir(parents=True, exist_ok=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps(metadata))
    shutil.copy2(source / "LICENSE", resources / "LICENSE")
    shutil.copy2(source / "third_party/THIRD_PARTY_NOTICES.md", resources / "THIRD_PARTY_NOTICES.md")


if __name__ == "__main__":
    prepare_bundle(Path(__file__).resolve().parents[1], Path(sys.argv[1]))
