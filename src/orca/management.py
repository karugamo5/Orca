"""UI-independent management operations and explicit configuration additions."""
import asyncio
import json
from contextlib import closing
from .config import MAX_CONFIG, decode_json, parse_models, parse_tools
from .errors import OrcaError
from .installation import atomic_write, exclusive_write
from .ipc import request
from .model_setup import read_snapshot, save_models
from .platforms.macos import InstanceLock
from .tool_setup import read_tools, revision_of, tool_settings


def model_revision(path):
    info = path.stat()
    return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"


def model_settings(paths):
    # Use file metadata, never a digest of a document that may contain tokens.
    revision = model_revision(paths.models)
    raw = read_snapshot(paths.models)
    models = parse_models(decode_json(raw))
    if revision != model_revision(paths.models):
        raise OrcaError("config")
    return {"revision": revision, "models": [{"id": m.id, "name": m.model} for m in models]}


def add_definition(paths, payload, *, tool=False):
    key = "tool" if tool else "model"
    if type(payload) is not dict or set(payload) != {key, "revision"} or not isinstance(payload["revision"], str):
        raise OrcaError("invalid_input")
    entry = payload[key]
    if type(entry) is not dict or (not tool and set(entry) - {
            "id", "model", "base_url", "keychain", "max_concurrent_requests", "tokens_per_second"}):
        raise OrcaError("invalid_input")
    try:
        (parse_tools if tool else parse_models)({"tools" if tool else "models": [entry]})
    except OrcaError:
        raise OrcaError("invalid_input") from None
    with closing(InstanceLock(paths.root / ".installation.lock")):
        if tool:
            original, document = read_tools(paths.tools, allow_missing=True)
            revision = revision_of(original)
        else:
            revision = model_settings(paths)["revision"]
            original = read_snapshot(paths.models)
            document = decode_json(original)
            if revision != model_revision(paths.models):
                raise OrcaError("config")
        if revision != payload["revision"]:
            raise OrcaError("config")
        entries = document["tools" if tool else "models"]
        if any(item["id"] == entry["id"] for item in entries):
            raise OrcaError("invalid_input")
        if tool:
            registered = {m["id"] for m in model_settings(paths)["models"]}
            if set(entry["models"]) - registered - {"all"}:
                raise OrcaError("invalid_input")
        entries.append(entry)
        if tool:
            parse_tools(document)
            replacement = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            if len(replacement) > MAX_CONFIG or read_tools(paths.tools, allow_missing=True)[0] != original:
                raise OrcaError("config")
            (exclusive_write if original is None else atomic_write)(paths.tools, replacement)
            return tool_settings(paths, allow_missing=False)
        save_models(paths.models, original, document)
        return model_settings(paths)


async def operate(args, paths, payload):
    if args.command == "tool":
        if args.settings:
            return tool_settings(paths)
        if args.add:
            return add_definition(paths, payload, tool=True)
        if type(payload) is not dict or set(payload) != {"models", "revision"}:
            raise OrcaError("invalid_input")
        # The runtime applies assignment changes before acknowledging the save.
        return await request(paths.socket, "set_tool_models", {"tool_id": args.assign, **payload})
    if args.settings:
        return model_settings(paths)
    if args.add:
        return add_definition(paths, payload)
    if args.enable or args.disable:
        return await request(paths.socket, "enable", {"model_id": args.enable or args.disable,
                                                      "enabled": bool(args.enable)})
    result = await request(paths.socket, "refresh_models")
    if args.wait == "true":
        while result["status"] == "running":
            await asyncio.sleep(0.1)
            result = (await request(paths.socket, "snapshot"))["model_refresh"]
        if result["status"] != "completed":
            raise OrcaError("internal")
    return result
