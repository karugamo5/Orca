"""Single-event-loop scheduler. Reservation and FIFO transitions never await."""
import asyncio
import math
from collections import deque
from dataclasses import dataclass, field
from .backend import Completion
from .config import DEFAULT_TOOLS, Config, Model, Tool
from .errors import BackendError, ErrorLog, OrcaError
from .storage import Store, ulid


@dataclass
class ModelState:
    model: Model
    enabled: bool
    health: str
    measured: float | None

    @property
    def speed(self):
        return self.model.tokens_per_second or self.measured or 100.0


@dataclass
class Job:
    id: str
    prompt: str
    max_tokens: int | None
    future: asyncio.Future
    tool_id: str = "generate_text"
    attempts: int = 0
    tried: set[str] = field(default_factory=set)
    task: asyncio.Task | None = None
    reservation: object | None = None


class Scheduler:
    def __init__(self, config: Config, store: Store, backend, log: ErrorLog, *, retry_delays=(0.25, 0.5)):
        self.config, self.store, self.backend, self.log = config, store, backend, log
        self.retry_delays = retry_delays
        self.models: dict[str, ModelState] = {}
        self.tools = {tool.id: tool for tool in DEFAULT_TOOLS}
        self.pending: deque[Job] = deque()
        self.jobs: dict[str, Job] = {}
        self.running: dict[str, int] = {}
        self.stopping = False
        self.failed = False

    def apply_models(self, models: list[Model], *, model_ids=None):
        self.store.sync(models)
        rows = self.store.models()
        old = self.models
        self.models = {}
        for model in models:
            if model_ids is not None and model.id not in model_ids:
                continue
            state = old.get(model.id)
            if state and state.model == model:
                self.models[model.id] = state
            else:
                row = rows[model.id]
                self.models[model.id] = ModelState(model, bool(row["enabled"]), row["health_status"], row["measured_tokens_per_second"])
        self.pump()

    def health(self, state: ModelState, online: bool):
        if self.models.get(state.model.id) is state:
            self.store.health(state.model.id, online)
            state.health = "online" if online else "offline"
            self.pump()

    def enable(self, model_id: str, value: bool):
        state = self.models.get(model_id)
        if state is None or type(value) is not bool:
            raise OrcaError("invalid_input")
        self.store.enabled(model_id, value)
        state.enabled = value
        self.pump()

    def apply_tools(self, tools: tuple[Tool, ...]):
        self.tools = {tool.id: tool for tool in tools}
        # Removed tools must not linger behind an unrelated blocked FIFO head.
        for job in list(self.jobs.values()):
            if job.tool_id not in self.tools and job.reservation is None:
                if job in self.pending:
                    self.pending.remove(job)
                if job.task:
                    job.task.cancel()
                self.finish(job, error=OrcaError("invalid_input"))
        self.pump()

    def candidates(self, tool_id="generate_text"):
        tool = self.tools.get(tool_id)
        if tool is None:
            raise OrcaError("invalid_input")
        return [s for s in self.models.values() if s.enabled and s.health == "online"
                and (tool.models == ("all",) or s.model.id in tool.models)]

    async def execute(self, prompt: str, max_tokens: int | None = None, *, tool_id="generate_text") -> str:
        if self.stopping:
            raise OrcaError("stopped")
        if self.failed:
            raise OrcaError("internal")
        candidates = self.candidates(tool_id)
        if not candidates:
            raise OrcaError("no_models")
        waiting = sum(j.task is None or j.task.get_name() == "retry_wait" for j in self.jobs.values())
        free = any(self.running.get(s.model.id, 0) < s.model.max_concurrent_requests for s in candidates)
        if (self.pending or not free) and self.config.max_pending and waiting >= self.config.max_pending:
            raise OrcaError("queue_full")
        job = Job(ulid(), prompt, max_tokens, asyncio.get_running_loop().create_future(), tool_id=tool_id)
        self.store.accept(job.id)
        self.jobs[job.id] = job
        self.pending.append(job)
        self.pump()
        try:
            return await job.future
        except asyncio.CancelledError:
            await self.cancel(job)
            raise

    def pump(self):
        try:
            self._pump()
        except Exception:
            self.abort()

    def abort(self):
        self.failed = True
        self.log.record("storage")
        current = asyncio.current_task()
        for job in list(self.jobs.values()):
            if not job.future.done():
                job.future.set_exception(OrcaError("internal"))
            job.prompt = ""
            if job.task and job.task is not current:
                job.task.cancel()
            if job.task is None:
                self.jobs.pop(job.id, None)
        self.pending.clear()

    def _pump(self):
        if self.stopping or self.failed:
            return
        while self.pending:
            job = self.pending[0]
            try:
                candidates = self.candidates(job.tool_id)
            except OrcaError as error:
                self.pending.popleft()
                self.finish(job, error=error)
                continue
            if not candidates:
                self.pending.popleft()
                self.finish(job, error=OrcaError("no_models"))
                continue
            free = [s for s in candidates if self.running.get(s.model.id, 0) < s.model.max_concurrent_requests]
            if not free:
                break
            # Switch to an untried available model, then stable speed/order ranking.
            free.sort(key=lambda s: (s.model.id in job.tried, -s.speed, s.model.id))
            state = free[0]
            self.pending.popleft()
            self.store.start(job.id, state.model.id)
            self.running[state.model.id] = self.running.get(state.model.id, 0) + 1
            job.attempts += 1
            job.tried.add(state.model.id)
            token = object()
            job.reservation = token
            job.task = asyncio.create_task(self.run(job, state, token), name="running")
            job.task.add_done_callback(lambda task, j=job, s=state, t=token: self.worker_done(j, s, t, task))

    def release(self, job: Job, state: ModelState, token: object):
        if job.reservation is token:
            job.reservation = None
            self.running[state.model.id] -= 1
            if not self.running[state.model.id]:
                self.running.pop(state.model.id)
            self.pump()

    def worker_done(self, job: Job, state: ModelState, token: object, task: asyncio.Task):
        # A task cancelled before its coroutine first runs never enters its finally.
        self.release(job, state, token)
        if task.cancelled():
            if self.jobs.get(job.id) is job and job.task is task:
                self.finish(job, error=OrcaError("cancelled"))
        elif task.exception() is not None:
            self.abort()

    def finish(self, job: Job, result: Completion | None = None, error: OrcaError | None = None):
        try:
            self.store.finish(job.id, "succeeded" if result else "cancelled" if error and error.code == "cancelled" else "failed",
                              input_tokens=result.input_tokens if result else None,
                              output_tokens=result.output_tokens if result else None, error=error)
        except Exception:
            self.abort()
            error, result = OrcaError("internal"), None
        self.jobs.pop(job.id, None)
        job.prompt = ""
        if not job.future.done():
            if result is not None:
                job.future.set_result(result.text)
            else:
                job.future.set_exception(error or OrcaError("internal"))

    async def run(self, job: Job, state: ModelState, token: object):
        retry = False
        try:
            result = await self.backend.complete(state.model, job.prompt, job.max_tokens)
            if (result.output_tokens is not None and result.output_tokens > 0 and result.elapsed is not None
                    and math.isfinite(result.elapsed) and result.elapsed > 0):
                speed = result.output_tokens / result.elapsed
                if math.isfinite(speed) and self.models.get(state.model.id) is state:
                    self.store.measure(state.model.id, speed)
                    state.measured = speed
            self.finish(job, result=result)
        except asyncio.CancelledError:
            self.finish(job, error=OrcaError("cancelled"))
        except BackendError as error:
            self.log.record("backend")
            if error.temporary and job.attempts < 3 and not self.stopping:
                try:
                    self.store.retry(job.id)
                    retry = True
                except Exception:
                    self.abort()
            else:
                self.finish(job, error=error)
        except Exception:
            self.log.record("internal")
            self.finish(job, error=OrcaError("internal"))
        finally:
            self.release(job, state, token)
        if retry:
            job.task.set_name("retry_wait")
            try:
                await asyncio.sleep(self.retry_delays[job.attempts - 1])
                job.task = None
                self.pending.append(job)
                self.pump()
            except asyncio.CancelledError:
                if job.id in self.jobs:
                    self.finish(job, error=OrcaError("cancelled"))
            except Exception:
                self.failed = True
                self.log.record("storage")
                self.finish(job, error=OrcaError("internal"))

    async def cancel(self, job: Job):
        if job.id not in self.jobs:
            if job.future.done() and not job.future.cancelled():
                job.future.exception()
            return
        if job.task:
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
        else:
            if job in self.pending:
                self.pending.remove(job)
            self.finish(job, error=OrcaError("cancelled"))
            self.pump()
        if job.future.done() and not job.future.cancelled():
            job.future.exception()

    async def stop(self):
        self.stopping = True
        for job in list(self.jobs.values()):
            await self.cancel(job)
