"""Export dependency metadata and verbatim notices into one distributable file."""
import importlib.metadata as metadata
from pathlib import Path

root = Path(__file__).resolve().parents[1]
out = root / "third_party" / "THIRD_PARTY_NOTICES.md"
out.parent.mkdir(parents=True, exist_ok=True)
sections = ["# Third-party notices\n\nGenerated from the project venv. Orca itself is Apache-2.0. "
            "Each third-party package retains its own license. License and copyright texts below are verbatim.\n"]
supplement = ""
if out.exists():
    previous = out.read_text(encoding="utf-8")
    start = previous.find("<!-- bundled-native:start -->")
    end = previous.find("<!-- bundled-native:end -->")
    if start >= 0 and end > start:
        supplement = previous[start:end + len("<!-- bundled-native:end -->")]
from cryptography.hazmat.backends.openssl.backend import backend
if backend.openssl_version_text() not in supplement:
    raise SystemExit("Review bundled OpenSSL license and update the native supplement before regenerating")
missing = []
count = 0
for dist in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
    name = dist.metadata["Name"]
    if name.lower() == "orca-local-mcp":
        continue
    expression = dist.metadata.get("License-Expression") or dist.metadata.get("License")
    classifiers = [c.rsplit(" :: ", 1)[-1] for c in dist.metadata.get_all("Classifier", []) if c.startswith("License ::")]
    sections.append(f"\n## {name} {dist.version}\n\nLicense metadata: {expression or ', '.join(classifiers) or 'see text below'}\n\n"
                    f"Source distribution: https://pypi.org/project/{name}/{dist.version}/#files\n")
    for link in dist.metadata.get_all("Project-URL", []):
        sections.append(f"\nProject URL: {link}\n")
    found = False
    for file in dist.files or []:
        if ".dist-info/" in str(file) and any(part.lower().startswith(("license", "copying", "notice", "authors")) for part in file.parts[1:]):
            source = Path(dist.locate_file(file))
            if source.is_file():
                sections.append(f"\n### {Path(*file.parts[1:])}\n\n```text\n{source.read_text(encoding='utf-8')}\n```\n")
                found = True
    if not found:
        missing.append(name)
    count += 1
if missing:
    raise SystemExit("License files missing: " + ", ".join(missing))
out.write_text("".join(sections) + "\n" + supplement + "\n", encoding="utf-8")
print(f"Exported {count} dependency notices to {out.relative_to(root)}")
