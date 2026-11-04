"""Explicit configuration edits shared by management clients."""
import hashlib
import json
from contextlib import closing
from .config import MAX_CONFIG, decode_json, load_models, parse_tools
from .errors import OrcaError
from .installation import atomic_write, exclusive_write
from .platforms.macos import InstanceLock


def read_tools(path, *, allow_missing):
    if path.is_symlink() or path.parent.is_symlink():
        raise OrcaError("config")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CONFIG + 1)
    except FileNotFoundError:
        if not allow_missing:
            raise OrcaError("config") from None
        raw = None
    except OSError:
        raise OrcaError("config") from None
    data = ({"tools": [{"id": "generate_text", "models": ["all"]}]} if raw is None else decode_json(raw))
    parse_tools(data)
    return raw, data


def revision_of(raw):
    return hashlib.sha256(b"missing" if raw is None else b"present\0" + raw).hexdigest()


def tool_settings(paths, *, allow_missing=True):
    raw, data = read_tools(paths.tools, allow_missing=allow_missing)
    return {"revision": revision_of(raw),
            "tools": [{"id": tool.id, "models": list(tool.models)} for tool in parse_tools(data)],
            "models": [{"id": model.id, "name": model.model} for model in load_models(paths.models)]}


def set_tool_models(paths, data, *, allow_missing=True):
    if (type(data) is not dict or set(data) != {"tool_id", "models", "revision"}
            or not isinstance(data["revision"], str)):
        raise OrcaError("invalid_input")
    try:
        requested = parse_tools({"tools": [{"id": data["tool_id"], "models": data["models"]}]})[0]
    except OrcaError:
        raise OrcaError("invalid_input") from None
    with closing(InstanceLock(paths.root / ".installation.lock")):
        original, document = read_tools(paths.tools, allow_missing=allow_missing)
        if revision_of(original) != data["revision"]:
            raise OrcaError("config")
        entry = next((item for item in document["tools"] if item["id"] == requested.id), None)
        if entry is None:
            raise OrcaError("invalid_input")
        registered = {model.id for model in load_models(paths.models)}
        if set(requested.models) - registered - set(entry["models"]) - {"all"}:
            raise OrcaError("invalid_input")
        entry["models"] = list(requested.models)
        parse_tools(document)
        replacement = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(replacement) > MAX_CONFIG:
            raise OrcaError("config")
        if read_tools(paths.tools, allow_missing=allow_missing)[0] != original:
            raise OrcaError("config")
        if original is None:
            exclusive_write(paths.tools, replacement)
        else:
            atomic_write(paths.tools, replacement)
        return tool_settings(paths, allow_missing=False)
