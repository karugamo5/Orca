"""JSON configuration parsing with complete snapshot validation."""
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from .errors import OrcaError

MAX_CONFIG = 1024 * 1024


def fail():
    raise OrcaError("config")


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            fail()
        result[key] = value
    return result


def decode_json(raw):
    try:
        if len(raw) > MAX_CONFIG:
            fail()
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: fail())
    except (ValueError, UnicodeError, RecursionError):
        raise OrcaError("config") from None


def read_json(path: Path):
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CONFIG + 1)
        return decode_json(raw)
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise OrcaError("config") from None


def obj(value, keys):
    if type(value) is not dict or set(value) - set(keys):
        fail()
    return value


def integer(value, minimum=0, maximum=2**31-1):
    if type(value) is not int or not minimum <= value <= maximum:
        fail()
    return value


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        fail()
    return float(value)


def string(value, maximum=256):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        fail()
    return value


@dataclass(frozen=True)
class Tool:
    id: str
    models: tuple[str, ...] = ("all",)
    description: str = "テキストによる処理をローカルサービスへ依頼します"

    def public(self):
        return {"id": self.id, "description": self.description}


DEFAULT_TOOLS = (Tool("generate_text"),)


def parse_tools(data) -> tuple[Tool, ...]:
    entries = obj(data, {"tools"}).get("tools")
    if type(entries) is not list or len(entries) > 256:
        fail()
    result, ids = [], set()
    for entry in entries:
        entry = obj(entry, {"id", "description", "models"})
        tool_id = string(entry.get("id"), 128)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", tool_id) or tool_id in ids:
            fail()
        ids.add(tool_id)
        models = entry.get("models")
        if type(models) is not list or len(models) > 256:
            fail()
        for model_id in models:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", string(model_id, 128)):
                fail()
        if len(set(models)) != len(models) or ("all" in models and models != ["all"]):
            fail()
        description = string(entry.get("description", Tool.description), 1024)
        try:
            description.encode("utf-8")
        except UnicodeError:
            fail()
        result.append(Tool(tool_id, tuple(models), description))
    return tuple(result)


class ToolRegistry:
    """Keep the last valid file; only an initially absent file uses legacy defaults."""
    def __init__(self, path: Path):
        self.path = path
        self.tools = DEFAULT_TOOLS
        self.seen_file = False

    def reload(self):
        try:
            self.path.lstat()
        except FileNotFoundError:
            if self.seen_file:
                fail()
            tools = DEFAULT_TOOLS
        except OSError:
            raise OrcaError("config") from None
        else:
            self.seen_file = True
            tools = parse_tools(read_json(self.path))
        changed = tools != self.tools
        self.tools = tools
        return changed


@dataclass(frozen=True)
class Config:
    auto_start: bool = False
    health_interval: int = 300
    max_pending: int = 10
    connect_timeout: float = 10.0
    request_timeout: float = 120.0
    health_timeout: float = 10.0


def load_config(path: Path) -> Config:
    data = obj(read_json(path), {"auto_start", "health_check", "queue", "timeouts"})
    auto = data.get("auto_start", False)
    if type(auto) is not bool:
        fail()
    health = obj(data.get("health_check", {}), {"interval_seconds"})
    queue = obj(data.get("queue", {}), {"max_pending"})
    limits = obj(data.get("timeouts", {}), {"connect_seconds", "request_seconds", "health_seconds"})
    return Config(auto, integer(health.get("interval_seconds", 300), 1),
                  integer(queue.get("max_pending", 10)), number(limits.get("connect_seconds", 10)),
                  number(limits.get("request_seconds", 120)), number(limits.get("health_seconds", 10)))


@dataclass(frozen=True)
class LocalStart:
    command: str = field(repr=False)
    args: tuple[str, ...] = field(default=(), repr=False)
    startup_timeout_seconds: float = 300.0


def parse_local_start(value, base_url):
    if value is None:
        return None
    value = obj(value, {"command", "args", "startup_timeout_seconds"})
    command = string(value.get("command"), 4096)
    if not Path(command).is_absolute() or urlsplit(base_url).hostname not in {"127.0.0.1", "::1", "localhost"}:
        fail()
    args = value.get("args", [])
    if type(args) is not list or len(args) > 256:
        fail()
    for arg in args:
        if not isinstance(arg, str) or len(arg) > 4096 or any(ord(c) < 32 for c in arg):
            fail()
    timeout = number(value.get("startup_timeout_seconds", 300))
    if timeout > 86400:
        fail()
    return LocalStart(command, tuple(args), timeout)


@dataclass(frozen=True)
class Model:
    id: str
    model: str
    base_url: str
    keychain: tuple[str, str] | None = None
    headers: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    max_concurrent_requests: int = 1
    tokens_per_second: float | None = None
    token: str | None = field(default=None, repr=False)
    local_start: LocalStart | None = field(default=None, repr=False)

    @property
    def identity(self):
        return self.model, self.base_url, self.keychain, self.headers, self.token


class ModelSelectionError(ValueError):
    """A management-only selection error; never returned through MCP."""


def select_models(models, model_ids):
    if model_ids is None:
        return models
    selected = set(model_ids)
    if not selected or selected - {model.id for model in models}:
        raise ModelSelectionError("--model requires ids registered in models.json")
    return [model for model in models if model.id in selected]


def load_models(path: Path) -> list[Model]:
    return parse_models(read_json(path))


def parse_models(data) -> list[Model]:
    data = obj(data, {"models"})
    entries = data.get("models")
    if type(entries) is not list or len(entries) > 256:
        fail()
    result = []
    ids = set()
    for entry in entries:
        entry = obj(entry, {"id", "model", "base_url", "keychain", "token", "headers", "max_concurrent_requests", "tokens_per_second", "local_start"})
        model_id = string(entry.get("id"), 128)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", model_id) or model_id in ids:
            fail()
        ids.add(model_id)
        base = string(entry.get("base_url"), 2048).rstrip("/")
        try:
            parsed = urlsplit(base)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.query or parsed.fragment or parsed.path != "/v1"
                    or parsed.port == 0):
                fail()
        except ValueError:
            fail()
        reference = entry.get("keychain")
        if reference is not None:
            reference = obj(reference, {"service", "account"})
            reference = (string(reference.get("service")), string(reference.get("account")))
        token = entry.get("token")
        if token is not None:
            string(token, 4096)
            if reference is not None or any(not 33 <= ord(c) <= 126 for c in token):
                fail()
        headers = obj(entry.get("headers", {}), {"X-Client-Name", "X-Request-Source"})
        headers = tuple(sorted((k, string(v, 256)) for k, v in headers.items()))
        speed = entry.get("tokens_per_second")
        result.append(Model(model_id, string(entry.get("model")), base, reference, headers,
                            integer(entry.get("max_concurrent_requests", 1), 1, 1024),
                            number(speed) if speed is not None else None, token,
                            parse_local_start(entry.get("local_start"), base)))
    return result


def validate_input(data: dict) -> tuple[str, int | None]:
    if type(data) is not dict or set(data) - {"prompt", "max_tokens"}:
        raise OrcaError("invalid_input")
    prompt = data.get("prompt")
    try:
        valid_prompt = isinstance(prompt, str) and bool(prompt.strip()) and len(prompt.encode("utf-8")) <= 128 * 1024
    except UnicodeError:
        valid_prompt = False
    if not valid_prompt:
        raise OrcaError("invalid_input")
    limit = data.get("max_tokens")
    if "max_tokens" in data and (type(limit) is not int or not 1 <= limit <= 65536):
        raise OrcaError("invalid_input")
    return prompt, limit
