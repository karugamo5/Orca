"""Bounded asynchronous OpenAI-compatible HTTP; no persistence or HTTP logging."""
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Protocol
import httpx
from .config import Config, Model
from .errors import BackendError, CredentialError

MAX_RESPONSE = 1024 * 1024
MAX_TEXT = 512 * 1024


class ConnectionFailure(BackendError):
    """An HTTP connection could not be established; no raw exception escapes."""
    def __init__(self):
        super().__init__(True)


class Credentials(Protocol):
    async def read(self, service: str, account: str) -> str: ...


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    elapsed: float | None = None


def tokens(value):
    return value if type(value) is int and value >= 0 else None


class OpenAIBackend:
    def __init__(self, config: Config, credentials: Credentials, *, transport=None):
        self.config = config
        self.credentials = credentials
        self.client = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                       timeout=httpx.Timeout(config.request_timeout, connect=config.connect_timeout))

    async def close(self):
        await self.client.aclose()

    async def _request(self, model: Model, method: str, endpoint: str, payload=None, *, health=False):
        headers = dict(model.headers)
        if model.token is not None:
            headers["Authorization"] = "Bearer " + model.token
        elif model.keychain:
            try:
                key = await self.credentials.read(*model.keychain)
                if not key or any(ord(c) < 32 for c in key):
                    raise CredentialError()
                headers["Authorization"] = "Bearer " + key
            except asyncio.CancelledError:
                raise
            except Exception:
                raise CredentialError() from None
        try:
            timeout = self.config.health_timeout if health else self.config.request_timeout
            started = time.monotonic()
            async with asyncio.timeout(timeout):
                async with self.client.stream(method, model.base_url + endpoint, headers=headers, json=payload) as response:
                    if response.status_code != 200:
                        raise BackendError(response.status_code in {429, 503}, response.status_code)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE:
                            raise BackendError()
                    return json.loads(raw), time.monotonic() - started
        except httpx.ConnectError:
            raise ConnectionFailure() from None
        except (TimeoutError, httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            raise BackendError(True) from None
        except (ValueError, UnicodeError, RecursionError, httpx.HTTPError):
            raise BackendError() from None

    async def health(self, model: Model) -> bool:
        data, _ = await self._request(model, "GET", "/models", health=True)
        return self.model_entry(data, model) is not None

    @staticmethod
    def model_entry(data, model):
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            return None
        for item in data["data"]:
            if not isinstance(item, dict) or item.get("id") != model.model:
                continue
            architecture = item.get("architecture")
            modalities = architecture.get("output_modalities") if isinstance(architecture, dict) else None
            # Exclude explicitly non-text models; compatible listings may omit
            # this optional metadata.
            if isinstance(modalities, list) and "text" not in modalities:
                continue
            return item
        return None

    async def availability(self, model: Model):
        """Management-only state: online, safe to try starting, loaded (if known)."""
        try:
            data, _ = await self._request(model, "GET", "/models", health=True)
        except ConnectionFailure:
            # HTTPS failures can be certificate failures against a live server.
            return False, model.base_url.startswith("http://"), None
        except (BackendError, CredentialError):
            return False, False, None
        entry = self.model_entry(data, model)
        loaded = entry.get("loaded") if entry is not None else None
        return entry is not None, False, loaded if type(loaded) is bool else None

    async def complete(self, model: Model, prompt: str, max_tokens: int | None) -> Completion:
        payload = {"model": model.model, "messages": [{"role": "user", "content": prompt}], "stream": False}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        data, elapsed = await self._request(model, "POST", "/chat/completions", payload)
        try:
            message = data["choices"][0]["message"]
            if not isinstance(message, dict) or message.get("tool_calls") or message.get("function_call"):
                raise BackendError()
            text = message["content"]
            # Treat a nullable final answer as empty text.
            # Never replace it with reasoning or try to execute generated tools.
            if text is None:
                text = ""
            if (not isinstance(text, str) or len(text.encode("utf-8")) > MAX_TEXT
                    or len(json.dumps(text, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE - 4096):
                raise BackendError()
            usage = data.get("usage") or {}
            if not isinstance(usage, dict):
                usage = {}
            return Completion(text, tokens(usage.get("prompt_tokens")), tokens(usage.get("completion_tokens")), elapsed)
        except (KeyError, TypeError, IndexError, UnicodeError):
            raise BackendError() from None
