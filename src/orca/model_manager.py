"""Independent, memory-only ownership of explicitly started local model servers.

No scheduler, request history, daemon socket, launchd registration or PID adoption.
"""
import asyncio
import os
import signal
import stat
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from urllib.parse import urlsplit
from .backend import OpenAIBackend
from .config import Config, Model, load_models
from .errors import ErrorLog, OrcaError
from .ipc import MAX_FRAME, encode, read_frame, request
from .platforms.macos import InstanceLock, Keychain, ModelProcess


@dataclass
class OwnedModel:
    model: Model
    state: str = "starting"
    error: str | None = None
    process: object | None = None
    task: asyncio.Task | None = None

    @property
    def active(self):
        return bool((self.task and not self.task.done()) or self.process is not None)


def endpoint(model):
    url = urlsplit(model.base_url)
    return url.port or (443 if url.scheme == "https" else 80)


class ModelManager:
    def __init__(self, paths, *, backend=None, process_factory=None, interval=1, idle_seconds=60):
        self.paths = paths
        self.backend = backend
        self.process_factory = process_factory or ModelProcess.start
        self.interval, self.idle_seconds = interval, idle_seconds
        self.models = {}
        self.owned = {}
        self.availability = {}
        self.config_error = False
        self.log = ErrorLog(paths.log)
        self.stop_event = asyncio.Event()
        self.last_used = time.monotonic()
        self.server = self.lock = self.watcher = None
        self.connections = set()
        self.shutdown_task = None
        self.shutting_down = self.shutdown_error = False

    def reload(self):
        try:
            models = load_models(self.paths.models)
        except OrcaError:
            if not self.config_error:
                self.log.record("config")
            self.config_error = True
            return
        self.config_error = False
        self.models = {model.id: model for model in models}
        self.availability = {key: value for key, value in self.availability.items()
                             if self.models.get(key.id) == key}
        self.owned = {key: entry for key, entry in self.owned.items()
                      if key in self.models or entry.active}

    async def probe(self, model):
        try:
            value = await self.backend.availability(model)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.log.record("backend")
            value = False, False, None
        if self.models.get(model.id) == model:
            self.availability[model] = value
        return value

    async def watch(self):
        while True:
            self.reload()
            await asyncio.gather(*(self.probe(model) for model in self.models.values() if model.local_start))
            if not any(entry.active for entry in self.owned.values()):
                if time.monotonic() - self.last_used > self.idle_seconds:
                    self.stop_event.set()
                    return
            await asyncio.sleep(self.interval)

    def snapshot(self):
        rows = []
        for model_id in dict.fromkeys([*self.models, *self.owned]):
            entry = self.owned.get(model_id)
            configured = self.models.get(model_id)
            model = configured or entry.model
            online, absent, _ = self.availability.get(model, (None, False, None))
            active = entry is not None and entry.active
            state = entry.state if entry else "external" if online else "stopped"
            conflict = any(other.model.id != model_id and other.active
                           and endpoint(other.model) == endpoint(model) for other in self.owned.values())
            rows.append({"id": model.id, "name": model.model,
                         "health": "unknown" if online is None else "online" if online else "offline",
                         "process_state": state, "error": entry.error if entry else None,
                         "can_start": bool(configured and model.local_start and absent and not active
                                           and not conflict and not self.config_error),
                         "can_stop": bool(active and entry.state != "stopping"),
                         "configured": configured is not None})
        return {"models": rows, "config_error": self.config_error, "shutdown_error": self.shutdown_error}

    def start_model(self, model_id):
        if self.shutting_down:
            raise OrcaError("unavailable")
        if self.config_error:
            raise OrcaError("config")
        model = self.models.get(model_id)
        if not model or not model.local_start:
            raise OrcaError("invalid_input")
        existing = self.owned.get(model_id)
        if existing and existing.task and not existing.task.done():
            if existing.state == "stopping":
                raise OrcaError("unavailable")
            return  # Repeated starts refer to the same operation, never a second child.
        if existing and existing.process is not None:
            raise OrcaError("unavailable")
        if any(entry.active and endpoint(entry.model) == endpoint(model)
               for entry in self.owned.values()):
            raise OrcaError("unavailable")
        entry = OwnedModel(model)
        self.owned[model_id] = entry
        entry.task = asyncio.create_task(self.run_model(entry))
        entry.task.add_done_callback(lambda task: self.finished(entry, task))

    def finished(self, entry, task):
        if task.cancelled() and entry.process is None:
            entry.state = "stopped"
        self.last_used = time.monotonic()

    async def run_model(self, entry):
        try:
            online, absent, _ = await self.probe(entry.model)
            if not absent:
                entry.state, entry.error = "failed", "server_exists" if online else "connection_failed"
                return
            if self.models.get(entry.model.id) != entry.model or self.config_error:
                entry.state, entry.error = "failed", "configuration_changed"
                return
            # Shield creation so a stop arriving during spawn cannot lose its child.
            spawn = asyncio.create_task(self.process_factory(entry.model.local_start))
            try:
                entry.process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                entry.process = await spawn
                raise
            async with asyncio.timeout(entry.model.local_start.startup_timeout_seconds):
                while True:
                    if entry.process.exited:
                        entry.error = "process_exited"
                        return
                    online, _, loaded = await self.probe(entry.model)
                    if online and loaded is not False:
                        break
                    await asyncio.sleep(self.interval)
            entry.state = "running"
            await entry.process.wait()
            entry.error = "process_exited"
        except asyncio.CancelledError:
            entry.state = "stopping"
        except TimeoutError:
            entry.error = "startup_timeout"
        except Exception:
            entry.error = "start_failed"
            self.log.record("lifecycle")
        finally:
            if entry.process:
                entry.state = "stopping"
                try:
                    await entry.process.stop()
                    entry.process = None
                except Exception:
                    entry.error = "stop_failed"
                    self.log.record("lifecycle")
            entry.state = "failed" if entry.error else "stopped"
            self.availability.pop(entry.model, None)

    def stop_model(self, model_id):
        entry = self.owned.get(model_id)
        if entry is None:
            raise OrcaError("invalid_input")  # Never find/kill a process by port or stale PID.
        if entry.state == "stopping":
            return
        entry.error = None
        if entry.task and not entry.task.done():
            entry.state = "stopping"
            entry.task.cancel()
        elif entry.process is not None:
            entry.state = "stopping"
            entry.task = asyncio.create_task(self.finish_stop(entry))
        else:
            entry.state = "stopped"

    async def finish_stop(self, entry):
        try:
            await entry.process.stop()
            entry.process = None
            entry.state = "stopped"
        except Exception:
            entry.state, entry.error = "failed", "stop_failed"
            self.log.record("lifecycle")

    async def dispatch(self, message):
        op, data = message["op"], message["data"]
        self.last_used = time.monotonic()
        self.reload()
        if op == "snapshot" and not data:
            return self.snapshot()
        if op == "shutdown" and not data:
            if not self.shutting_down:
                self.shutting_down, self.shutdown_error = True, False
                for entry in self.owned.values():
                    self.stop_model(entry.model.id)
                self.shutdown_task = asyncio.create_task(self.prepare_shutdown())
            return {}
        if op not in {"start", "stop"} or set(data) != {"model_id"} or type(data["model_id"]) is not str:
            raise OrcaError("invalid_input")
        if op == "start":
            self.start_model(data["model_id"])
        else:
            self.stop_model(data["model_id"])
        return self.snapshot()

    async def prepare_shutdown(self):
        await asyncio.gather(*(entry.task for entry in self.owned.values() if entry.task), return_exceptions=True)
        if any(entry.process is not None for entry in self.owned.values()):
            self.shutting_down, self.shutdown_error = False, True
        else:
            self.stop_event.set()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        correlation = None
        try:
            async with asyncio.timeout(5):
                message = await read_frame(reader)
                correlation = message.get("id")
                if (set(message) != {"v", "id", "op", "data"} or message["v"] != 1
                        or type(correlation) is not str or len(correlation) > 128
                        or type(message["op"]) is not str or type(message["data"]) is not dict):
                    raise OrcaError("invalid_input")
                try:
                    result = await self.dispatch(message)
                    response = {"v": 1, "id": correlation, "ok": True, "result": result}
                except OrcaError as error:
                    response = {"v": 1, "id": correlation, "ok": False, "error": error.public()}
                writer.write(encode(response))
                await writer.drain()
        except (OrcaError, OSError, TimeoutError):
            pass
        except Exception:
            self.log.record("ipc")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self.connections.discard(task)

    async def start(self):
        self.paths.prepare()
        self.lock = InstanceLock(self.paths.root / "models.lock")
        try:
            self.reload()
            if self.config_error:
                raise OrcaError("config")
            self.backend = self.backend or OpenAIBackend(
                Config(connect_timeout=2, health_timeout=2), Keychain())
            path = self.paths.models_socket
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise OrcaError("internal")
                path.unlink()
            self.server = await asyncio.start_unix_server(self.handle, str(path), limit=MAX_FRAME)
            os.chmod(path, 0o600)
            self.watcher = asyncio.create_task(self.watch())
            self.watcher.add_done_callback(self.watcher_done)
        except BaseException:
            await self.close()
            raise

    def watcher_done(self, task):
        if not task.cancelled() and task.exception() is not None:
            self.log.record("internal")
            self.stop_event.set()

    async def close(self):
        if self.server:
            self.server.close()
        if self.watcher:
            self.watcher.cancel()
            await asyncio.gather(self.watcher, return_exceptions=True)
        for entry in self.owned.values():
            self.stop_model(entry.model.id)
        await asyncio.gather(*(entry.task for entry in self.owned.values() if entry.task), return_exceptions=True)
        for task in list(self.connections):
            task.cancel()
        await asyncio.gather(*self.connections, return_exceptions=True)
        if self.server:
            await self.server.wait_closed()
            self.paths.models_socket.unlink(missing_ok=True)
            self.server = None
        if self.backend:
            await self.backend.close()
        if self.lock:
            self.lock.close()
            self.lock = None

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop_event.set)
        try:
            await self.start()
            await self.stop_event.wait()
        finally:
            await self.close()


async def ensure_manager(paths):
    try:
        return await request(paths.models_socket, "snapshot")
    except OrcaError as error:
        if error.code != "stopped":
            raise
    # Serialize manager creation with installation, but never hold this lock while
    # waiting for a model to load: another CLI must be able to stop that model.
    with closing(InstanceLock(paths.root / ".installation.lock")):
        return await launch_manager(paths)


async def launch_manager(paths):
    load_models(paths.models)
    process = subprocess.Popen([sys.executable, "-m", "orca", *paths.arguments(), "model_service"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    for _ in range(100):
        try:
            return await request(paths.models_socket, "snapshot")
        except OrcaError as error:
            if error.code != "stopped":
                raise
        await asyncio.sleep(0.05)
    if process.poll() is None:
        process.terminate()
        await asyncio.to_thread(process.wait, 5)
    raise OrcaError("internal")


async def shutdown_manager(paths):
    try:
        await request(paths.models_socket, "shutdown")
    except OrcaError as error:
        if error.code != "stopped":
            raise
    for _ in range(150):
        # The listener can close/reset connections before backend cleanup ends.
        # The lock is released last; do not query the closing listener again.
        if not InstanceLock.is_held(paths.root / "models.lock"):
            return
        await asyncio.sleep(0.05)
    raise OrcaError("internal")
