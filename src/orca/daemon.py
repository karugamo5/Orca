"""Owns configuration, scheduler, history, health and the private Unix listener."""
import asyncio
import os
import signal
import stat
from .backend import OpenAIBackend
from .config import ToolRegistry, load_config, load_models, select_models, validate_input
from .errors import ErrorLog, OrcaError
from .ipc import MAX_FRAME, encode, read_frame, request
from .platforms.macos import InstanceLock, Keychain, Paths
from .routing import Scheduler
from .storage import Store
from .tool_setup import set_tool_models, tool_settings


class Daemon:
    def __init__(self, paths: Paths, *, backend=None, model_ids=None):
        self.paths = paths
        self.model_ids = frozenset(model_ids) if model_ids is not None else None
        self.model_definitions = []
        self.log = ErrorLog(paths.log)
        self.stop_event = asyncio.Event()
        self.background = []
        self.connections = set()
        self.server = None
        self.lock = None
        self.store = None
        self.scheduler = None
        self.backend = backend
        self.config_error = False
        self.model_config_error = False
        self.tool_config_error = False
        self.tool_registry = ToolRegistry(paths.tools)
        self.background_error = False
        self.ready = False
        self.health_tasks = {}
        self.refresh_task = None
        self.model_refresh = {"generation": 0, "status": "idle"}

    async def start(self):
        self.paths.prepare()
        self.lock = InstanceLock(self.paths.root / "daemon.lock")
        try:
            config = load_config(self.paths.config)
            models = load_models(self.paths.models)
            self.tool_registry.reload()
            select_models(models, self.model_ids)
            self.store = Store(self.paths.database)
            self.store.recover()
            self.store.prune()
            self.backend = self.backend or OpenAIBackend(config, Keychain())
            self.scheduler = Scheduler(config, self.store, self.backend, self.log)
            self.scheduler.apply_tools(self.tool_registry.tools)
            self.scheduler.apply_models(models, model_ids=self.model_ids)
            self.model_definitions = models
            if self.paths.socket.exists() or self.paths.socket.is_symlink():
                info = self.paths.socket.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise OrcaError("internal")
                self.paths.socket.unlink()
            self.server = await asyncio.start_unix_server(self.handle, str(self.paths.socket), limit=MAX_FRAME)
            os.chmod(self.paths.socket, 0o600)
            await self.check_health()
            self.ready = True
            for coroutine in (self.watch_models(), self.watch_tools(), self.monitor_health(), self.watch_local_models(), self.prune()):
                task = asyncio.create_task(self.guard(coroutine))
                self.background.append(task)
        except BaseException:
            await self.close()
            raise

    async def guard(self, coroutine):
        try:
            await coroutine
        except asyncio.CancelledError:
            raise
        except Exception:
            self.background_error = True
            self.log.record("internal")

    async def check_health(self, states=None):
        async def check(state):
            try:
                online = await self.backend.health(state.model)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.record("backend")
                online = False
            self.scheduler.health(state, online)
        tasks = []
        for state in (list(self.scheduler.models.values()) if states is None else states):
            key = id(state)
            task = self.health_tasks.get(key)
            if task is None:
                task = asyncio.create_task(check(state))
                self.health_tasks[key] = task
                task.add_done_callback(lambda completed, key=key: self.health_tasks.pop(key, None))
            tasks.append(asyncio.shield(task))
        await asyncio.gather(*tasks)

    async def refresh_models(self):
        try:
            await self.check_health()
            self.model_refresh["status"] = "completed"
        except asyncio.CancelledError:
            self.model_refresh["status"] = "cancelled"
            raise
        except Exception:
            self.model_refresh["status"] = "failed"
            self.log.record("backend")

    async def watch_local_models(self):
        """Observe process transitions without owning or controlling the manager."""
        previous = {}
        while True:
            await asyncio.sleep(1)
            states = [state for state in self.scheduler.models.values() if state.model.local_start]
            if not states:
                continue
            try:
                value = await request(self.paths.models_socket, "snapshot")
                current = {row["id"]: (row["process_state"], row["health"]) for row in value["models"]}
            except (OrcaError, KeyError, TypeError):
                continue
            changed = [state for state in states if current.get(state.model.id) != previous.get(state.model.id)]
            previous = current
            if changed:
                await self.check_health(changed)

    async def monitor_health(self):
        while True:
            await asyncio.sleep(self.scheduler.config.health_interval)
            await self.check_health()

    async def watch_models(self):
        previous = self.model_definitions
        while True:
            await asyncio.sleep(1)
            try:
                models = load_models(self.paths.models)
            except OrcaError:
                if not self.model_config_error:
                    self.log.record("config")
                self.model_config_error = True
                self.config_error = True
                continue
            self.model_config_error = False
            self.config_error = self.tool_config_error
            if models != previous:
                self.scheduler.apply_models(models, model_ids=self.model_ids)
                previous = models
                await self.check_health()

    async def watch_tools(self):
        while True:
            await asyncio.sleep(1)
            try:
                changed = self.tool_registry.reload()
            except OrcaError:
                if not self.tool_config_error:
                    self.log.record("config")
                self.tool_config_error = True
                self.config_error = True
                continue
            self.tool_config_error = False
            self.config_error = self.model_config_error
            if changed:
                self.scheduler.apply_tools(self.tool_registry.tools)

    async def prune(self):
        while True:
            await asyncio.sleep(3600)
            self.store.prune()

    def snapshot(self, *, monitor=False):
        scheduler = self.scheduler
        # Capture without yielding so all monitor pages describe the same instant.
        rows = self.store.active(None if monitor else 10)
        count, pending = ((len(rows), sum(row["status"] != "running" for row in rows))
                          if monitor else self.store.active_counts())
        result = {"state": "error" if self.config_error or self.background_error or scheduler.failed or self.log.failed else "running",
                "ready": self.ready, "max_pending": scheduler.config.max_pending,
                "pending_count": pending, "request_count": count, "requests": rows}
        if not monitor:
            result["model_refresh"] = dict(self.model_refresh)
            result["launch_models"] = sorted(self.model_ids) if self.model_ids is not None else None
            result["models"] = [{"id": state.model.id, "name": state.model.model, "enabled": state.enabled,
                                 "health": state.health} for state in scheduler.models.values()]
        return result

    async def dispatch(self, message):
        op, data = message["op"], message["data"]
        if op == "refresh_models" and data == {}:
            if not self.ready:
                raise OrcaError("unavailable")
            if self.refresh_task is None or self.refresh_task.done():
                self.model_refresh = {"generation": self.model_refresh["generation"] + 1, "status": "running"}
                self.refresh_task = asyncio.create_task(self.refresh_models())
            return dict(self.model_refresh)
        if op == "generate":
            if not self.ready:
                raise OrcaError("no_models")
            tool_id = data.get("tool_id", "generate_text")
            if not isinstance(tool_id, str) or tool_id not in self.scheduler.tools:
                raise OrcaError("invalid_input")
            prompt, max_tokens = validate_input({key: value for key, value in data.items() if key != "tool_id"})
            return {"text": await self.scheduler.execute(prompt, max_tokens, tool_id=tool_id)}
        if op == "tools" and data == {}:
            return {"tools": [tool.public() for tool in self.scheduler.tools.values()]}
        if op == "tool_settings" and data == {}:
            return tool_settings(self.paths, allow_missing=not self.tool_registry.seen_file)
        if op == "set_tool_models":
            result = set_tool_models(self.paths, data, allow_missing=not self.tool_registry.seen_file)
            self.tool_registry.reload()
            self.scheduler.apply_tools(self.tool_registry.tools)
            self.tool_config_error = False
            self.config_error = self.model_config_error
            return result
        if op == "snapshot" and data == {}:
            return self.snapshot()
        if op == "monitor" and data == {}:
            return self.snapshot(monitor=True)
        if op == "enable" and set(data) == {"model_id", "enabled"}:
            if not isinstance(data["model_id"], str):
                raise OrcaError("invalid_input")
            self.scheduler.enable(data["model_id"], data["enabled"])
            return {"enabled": data["enabled"]}
        if op == "stop" and data == {}:
            return {}
        raise OrcaError("invalid_input")

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        work = disconnected = None
        correlation = None
        try:
            async with asyncio.timeout(5):
                message = await read_frame(reader)
            correlation = message.get("id")
            if (message.get("v") != 1 or not isinstance(correlation, str) or len(correlation) > 128
                    or not isinstance(message.get("op"), str) or type(message.get("data")) is not dict
                    or set(message) != {"v", "id", "op", "data"}):
                raise OrcaError("invalid_input")
            work = asyncio.create_task(self.dispatch(message))
            disconnected = asyncio.create_task(reader.read(1))
            done, _ = await asyncio.wait({work, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done and work not in done:
                return
            result = await work
            if message["op"] == "monitor":
                rows = result.pop("requests")
                for seq, offset in enumerate(range(0, max(1, len(rows)), 100)):
                    if disconnected.done():
                        return
                    page = {**result, "requests": rows[offset:offset + 100]}
                    writer.write(encode({"v": 1, "id": correlation, "ok": True, "seq": seq,
                                         "more": offset + 100 < len(rows), "result": page}))
                    async with asyncio.timeout(5):
                        await writer.drain()
                    await asyncio.sleep(0)
            else:
                writer.write(encode({"v": 1, "id": correlation, "ok": True, "result": result}))
                await writer.drain()
            if message["op"] == "stop":
                self.stop_event.set()
        except OrcaError as error:
            try:
                writer.write(encode({"v": 1, "id": correlation, "ok": False, "error": error.public()}))
                await writer.drain()
            except (OSError, OrcaError):
                pass
        except (OSError, TimeoutError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            self.log.record("ipc")
            try:
                writer.write(encode({"v": 1, "id": correlation, "ok": False, "error": OrcaError("internal").public()}))
                await writer.drain()
            except OSError:
                pass
        finally:
            for child in (work, disconnected):
                if child and not child.done():
                    child.cancel()
            await asyncio.gather(*(child for child in (work, disconnected) if child), return_exceptions=True)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self.connections.discard(task)

    async def close(self):
        self.ready = False
        tasks = list(self.health_tasks.values()) + ([self.refresh_task] if self.refresh_task else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.server:
            self.server.close()
        for task in self.background:
            task.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)
        if self.scheduler:
            await self.scheduler.stop()
        for task in list(self.connections):
            task.cancel()
        await asyncio.gather(*self.connections, return_exceptions=True)
        if self.server:
            # Python 3.12+ waits for active client connections here. Cancel
            # requests and close their writers first, including idle clients.
            await self.server.wait_closed()
        if self.backend:
            await self.backend.close()
        if self.store:
            self.store.close()
            self.store = None
        if self.server:
            self.paths.socket.unlink(missing_ok=True)
            self.server = None
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
