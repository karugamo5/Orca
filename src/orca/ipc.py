"""One operation per local connection; closing a generate connection cancels it."""
import asyncio
import json
import uuid
from pathlib import Path
from .errors import OrcaError

MAX_FRAME = 1024 * 1024


def encode(data: dict) -> bytes:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
    if len(raw) > MAX_FRAME:
        raise OrcaError("invalid_input")
    return raw


async def read_frame(reader: asyncio.StreamReader) -> dict:
    try:
        raw = await reader.readline()
        if not raw or len(raw) > MAX_FRAME or not raw.endswith(b"\n"):
            raise OrcaError("internal")
        data = json.loads(raw)
        if type(data) is not dict:
            raise OrcaError("internal")
        return data
    except (ValueError, UnicodeError, RecursionError):
        raise OrcaError("internal") from None


def result_of(response: dict, correlation: str):
    if response.get("v") != 1 or response.get("id") != correlation or type(response.get("ok")) is not bool:
        raise OrcaError("internal")
    if not response["ok"]:
        error = response.get("error")
        if type(error) is not dict:
            raise OrcaError("internal")
        raise OrcaError(error.get("code", "internal"), error.get("http_status"))
    return response.get("result")


async def request(path: Path, op: str, data: dict | None = None):
    writer = None
    try:
        async with asyncio.timeout(5):
            reader, writer = await asyncio.open_unix_connection(str(path), limit=MAX_FRAME)
        correlation = uuid.uuid4().hex
        writer.write(encode({"v": 1, "id": correlation, "op": op, "data": data or {}}))
        await writer.drain()
        if op == "generate":
            response = await read_frame(reader)
        else:
            async with asyncio.timeout(5):
                response = await read_frame(reader)
        return result_of(response, correlation)
    except (FileNotFoundError, ConnectionRefusedError):
        raise OrcaError("stopped") from None
    except (OSError, TimeoutError):
        raise OrcaError("internal") from None
    finally:
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def monitor_pages(path: Path):
    """One immutable metadata snapshot, delivered in bounded ordered frames."""
    writer = None
    try:
        async with asyncio.timeout(5):
            reader, writer = await asyncio.open_unix_connection(str(path), limit=MAX_FRAME)
        correlation = uuid.uuid4().hex
        writer.write(encode({"v": 1, "id": correlation, "op": "monitor", "data": {}}))
        await writer.drain()
        seq = total = 0
        header = None
        while True:
            async with asyncio.timeout(5):
                response = await read_frame(reader)
            page = result_of(response, correlation)
            if (type(response.get("seq")) is not int or response["seq"] != seq
                    or type(response.get("more")) is not bool or type(page) is not dict
                    or set(page) != {"state", "ready", "max_pending", "pending_count", "request_count", "requests"}
                    or type(page["requests"]) is not list or len(page["requests"]) > 100
                    or any(type(page[key]) is not int or page[key] < 0
                           for key in ("max_pending", "pending_count", "request_count"))
                    or page["pending_count"] > page["request_count"]):
                raise OrcaError("internal")
            current = {key: value for key, value in page.items() if key != "requests"}
            if header is not None and current != header:
                raise OrcaError("internal")
            header = current
            total += len(page["requests"])
            if (total > page["request_count"] or (response["more"] and not page["requests"])
                    or (not response["more"] and total != page["request_count"])):
                raise OrcaError("internal")
            yield page
            if not response["more"]:
                return
            seq += 1
    except (FileNotFoundError, ConnectionRefusedError):
        raise OrcaError("stopped") from None
    except (OSError, TimeoutError):
        raise OrcaError("internal") from None
    finally:
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
