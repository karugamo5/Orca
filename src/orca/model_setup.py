"""Explicit interactive model editing; never collect or display API tokens."""
import json
from .config import MAX_CONFIG, decode_json, parse_models, string
from .errors import OrcaError
from .installation import atomic_write


def prompt(label, default=None, convert=str):
    while True:
        suffix = f" [{default}]" if default is not None else ""
        value = input(f"{label}{suffix}: ").strip()
        if not value and default is not None:
            value = str(default)
        try:
            return convert(value)
        except (ValueError, OrcaError):
            print("Invalid value; please try again")


def choice(*values):
    def convert(value):
        value = value.lower()
        if value not in values:
            raise ValueError()
        return value
    return convert


def model_field(name, convert=str):
    def validate(value):
        value = convert(value)
        entry = {"id": "model", "model": "model", "base_url": "http://localhost/v1", name: value}
        parse_models({"models": [entry]})
        return value
    return validate


def read_snapshot(path):
    if path.is_symlink() or path.parent.is_symlink():
        raise OrcaError("config")
    try:
        with path.open("rb") as stream:
            return stream.read(MAX_CONFIG + 1)
    except OSError:
        raise OrcaError("config") from None


def save_models(path, original, data):
    """Validate and commit a document; the caller holds the installation lock."""
    parse_models(data)
    replacement = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(replacement) > MAX_CONFIG or read_snapshot(path) != original:
        raise OrcaError("config")
    atomic_write(path, replacement)


def edit_model(paths):
    original = read_snapshot(paths.models)
    data = decode_json(original)
    parse_models(data)
    print("Model setup: enter an existing ID to edit, or a new ID to add")
    print("Press Enter to use the shown default; press Ctrl-C to cancel before saving")
    try:
        model_id = prompt("Model ID", convert=model_field("id"))
        entries = data["models"]
        existing = next((entry for entry in entries if entry["id"] == model_id), None)
        if existing is None and len(entries) >= 256:
            raise OrcaError("config")
        entry = dict(existing or {"id": model_id})
        print("Editing existing model" if existing is not None else "Adding new model")
        entry["model"] = prompt("API model name", entry.get("model"), model_field("model"))
        entry["base_url"] = prompt("Base URL (http(s)://host[:port]/v1)", entry.get("base_url"), model_field("base_url"))
        has_token = entry.get("token") is not None
        auth_default = "keep" if has_token else "keychain" if entry.get("keychain") else "none"
        options = ("keep", "none", "keychain") if has_token else ("none", "keychain")
        if has_token:
            print("A JSON token is configured; keep preserves it without displaying it")
        authentication = prompt("Authentication (" + "/".join(options) + ")", auth_default, choice(*options))
        if authentication != "keep":
            entry.pop("token", None)
            reference = entry.pop("keychain", None) or {}
            if authentication == "keychain":
                entry["keychain"] = {
                    "service": prompt("Keychain service", reference.get("service"), string),
                    "account": prompt("Keychain account", reference.get("account"), string),
                }
        entry["max_concurrent_requests"] = prompt("Maximum concurrent requests", entry.get("max_concurrent_requests", 1),
                                                   model_field("max_concurrent_requests", int))
        entry["tokens_per_second"] = prompt("Tokens per second (auto uses measured speed or 100)",
            entry.get("tokens_per_second") or "auto",
            model_field("tokens_per_second", lambda value: None if value.lower() == "auto" else float(value)))
    except (EOFError, KeyboardInterrupt):
        print("\nModel setup cancelled; settings were not changed")
        return False
    if existing is None:
        entries.append(entry)
    else:
        entries[entries.index(existing)] = entry
    parse_models(data)
    replacement = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(replacement) > MAX_CONFIG:
        raise OrcaError("config")
    if read_snapshot(paths.models) != original:
        print("Models changed during setup; settings were not overwritten")
        raise OrcaError("config")
    save_models(paths.models, original, data)
    print("Model settings saved")
    return True


def confirm_restart():
    try:
        return prompt("Restart Orca to reload settings? (yes/no; running requests will be cancelled)",
                      "no", choice("yes", "no")) == "yes"
    except (EOFError, KeyboardInterrupt):
        print()
        return False
