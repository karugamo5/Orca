"""macOS boundaries: paths, single-instance locking and read-only Keychain."""
import asyncio
import fcntl
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from ..errors import BackendError, OrcaError


def private_directory(path: Path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OrcaError("internal")


@dataclass(frozen=True)
class Paths:
    root: Path
    runtime: Path | None = None

    @classmethod
    def default(cls):
        return cls(Path.home() / "Library" / "Application Support" / "Orca")

    @property
    def socket_dir(self):
        if self.runtime is not None:
            return self.runtime
        digest = hashlib.sha256(os.fsencode(self.root.resolve())).hexdigest()[:16]
        return Path("/tmp") / f"orca-{os.getuid()}-{digest}"

    @property
    def socket(self):
        return self.socket_dir / "daemon.sock"

    @property
    def models_socket(self):
        return self.socket_dir / "models.sock"

    @property
    def config_dir(self):
        # Keep the installed --home contract (including existing MCP/launchd
        # registrations) while separating user JSON from program and state.
        if self.root.parts[-3:] == ("Library", "Application Support", "Orca"):
            return self.root.parents[2] / ".config" / "orca"
        # Explicit development roots remain fully isolated in that directory.
        return self.root

    @property
    def config(self):
        return self.config_dir / "config.json"

    @property
    def models(self):
        return self.config_dir / "models.json"

    @property
    def tools(self):
        return self.config_dir / "tools.json"

    @property
    def database(self):
        return self.root / "orca.sqlite3"

    @property
    def log(self):
        return self.root / "logs" / "errors.jsonl"

    def arguments(self):
        result = ["--home", str(self.root)]
        if self.runtime is not None:
            result += ["--runtime-dir", str(self.runtime)]
        return result

    def prepare(self):
        private_directory(self.root)
        private_directory(self.socket_dir)


class InstanceLock:
    @staticmethod
    def is_held(path: Path):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(fd)

    def __init__(self, path: Path):
        self.fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise OrcaError("internal") from None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Keychain:
    async def read(self, service: str, account: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=16384)
        try:
            async with asyncio.timeout(10):
                output, _ = await process.communicate()
            if process.returncode or len(output) > 8192:
                raise BackendError()
            return output.decode("utf-8").rstrip("\n")
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise


class ModelProcess:
    """Own only a direct foreground child; never discover or signal external PIDs."""
    def __init__(self, process):
        self.process = process

    @classmethod
    async def start(cls, definition):
        process = await asyncio.create_subprocess_exec(
            definition.command, *definition.args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        return cls(process)

    @property
    def exited(self):
        return self.process.returncode is not None

    async def wait(self):
        return await self.process.wait()

    async def stop(self):
        if not self.exited:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                async with asyncio.timeout(3):
                    await self.process.wait()
            except TimeoutError:
                if not self.exited:
                    self.process.kill()
        async with asyncio.timeout(2):
            await self.process.wait()


class LoginItems:
    """Write RunAtLoad-only definitions for the next login; never KeepAlive.

    Deliberately does not bootstrap them now, so applying login preferences
    cannot start a stopped daemon. Tests inject a project-local agents directory.
    """
    LABELS = ("local.orca.daemon", "local.orca.menu")

    def __init__(self, directory: Path):
        self.directory = directory

    def sync(self, enabled: bool, daemon_command: list[str], menu_command: list[str]):
        import plistlib
        import tempfile
        if self.directory.is_symlink():
            raise OrcaError("internal")
        originals = {}
        for label in self.LABELS:
            path = self.directory / f"{label}.plist"
            if path.is_symlink():
                raise OrcaError("internal")
            raw = path.read_bytes() if path.exists() else None
            if raw is not None and plistlib.loads(raw).get("Label") != label:
                raise OrcaError("internal")
            originals[path] = raw

        def write(destination, raw):
            fd, name = tempfile.mkstemp(prefix=".orca-", dir=self.directory)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                os.replace(name, destination)
            finally:
                Path(name).unlink(missing_ok=True)

        changed = []
        try:
            if enabled:
                self.directory.mkdir(parents=True, exist_ok=True)
            for (path, original), command in zip(originals.items(), (daemon_command, menu_command)):
                if enabled:
                    payload = {"Label": path.stem, "ProgramArguments": command, "RunAtLoad": True,
                               "KeepAlive": False, "ProcessType": "Background"}
                    write(path, plistlib.dumps(payload))
                elif original is not None:
                    path.unlink()
                else:
                    continue
                changed.append(path)
        except BaseException:
            for path in reversed(changed):
                if originals[path] is None:
                    path.unlink(missing_ok=True)
                else:
                    write(path, originals[path])
            raise


def sync_installed_login(paths: Paths):
    """Development roots without installer ownership never touch native settings."""
    import json
    from ..config import load_config
    marker = paths.root / "installed.json"
    if not marker.exists():
        return
    data = json.loads(marker.read_text())
    if data.get("format") != "orca-install-v1" or not isinstance(data.get("user_home"), str):
        raise OrcaError("config")
    user = Path(data["user_home"])
    if paths.root != user / "Library" / "Application Support" / "Orca":
        raise OrcaError("config")
    current = paths.root / "current"
    if not current.is_symlink() or current.resolve().parent != (paths.root / "releases").resolve():
        raise OrcaError("config")
    python = str(current / "venv" / "bin" / "python")
    LoginItems(user / "Library" / "LaunchAgents").sync(
        load_config(paths.config).auto_start,
        [python, "-m", "orca", *paths.arguments(), "daemon"],
        menu_launch_command(paths))


def menu_launch_command(paths: Paths, *, resolve=False):
    app = paths.root / "current" / "Orca.app"
    if resolve:
        app = app.resolve()
    return ["/usr/bin/open", "-n", "-g", "-W", str(app),
            "--args", *paths.arguments()]


def open_menu(paths: Paths):
    """Open the app bundle through LaunchServices, waiting for its lifetime."""
    import subprocess
    executable = paths.root / "current" / "Orca.app" / "Contents" / "MacOS" / "OrcaMenu"
    if not executable.is_file():
        raise OrcaError("config")
    # Pin this launch to its release while login definitions retain current.
    return subprocess.Popen(menu_launch_command(paths, resolve=True), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def process_executable(pid: int):
    """Read the kernel's executable identity, never process arguments or content."""
    import ctypes
    library = ctypes.CDLL("/usr/lib/libproc.dylib")
    buffer = ctypes.create_string_buffer(4096)
    if library.proc_pidpath(pid, buffer, len(buffer)) <= 0:
        return None
    return Path(os.fsdecode(buffer.value))


class InstalledMenu:
    """Replaceable native boundary with confirmed stop/start, scoped to this root."""
    def __init__(self, paths: Paths):
        self.paths = paths
        self.child = None

    def locked(self):
        try:
            fd = os.open(self.paths.root / "menu.lock", os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return False
            except BlockingIOError:
                return True
        finally:
            os.close(fd)

    def identity(self):
        import json
        import re
        if not self.locked():
            return None  # A stale PID alone never authorizes a signal.
        try:
            fd = os.open(self.paths.root / "menu.pid", os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                raw = stream.read(33)
            if len(raw) > 32 or not raw.strip().isdigit():
                raise ValueError()
            pid = int(raw)
            if pid <= 1:
                raise ValueError()
            executable = process_executable(pid)
            if executable is None and not self.locked():
                return None
            relative = executable.relative_to(self.paths.root.resolve() / "releases") if executable else None
            if (relative is None or len(relative.parts) != 5
                    or not re.fullmatch(r"[0-9a-f]{32}", relative.parts[0])
                    or relative.parts[1:] != ("Orca.app", "Contents", "MacOS", "OrcaMenu")):
                raise ValueError()
            release = self.paths.root / "releases" / relative.parts[0]
            marker = release / "release.json"
            if release.is_symlink() or marker.is_symlink():
                raise ValueError()
            with marker.open("rb") as stream:
                ownership = json.loads(stream.read(1025))
            if ownership != {"format": "orca-install-v1", "release": release.name}:
                raise ValueError()
            return pid, executable
        except (OSError, ValueError, UnicodeError):
            if not self.locked():
                return None
            raise OrcaError("internal") from None

    def stop(self):
        import signal
        import time
        deadline = time.monotonic() + 10
        # open -W is a launcher, not the app. Do not kill it and then assume
        # that a pending LaunchServices request cannot start a late app.
        while True:
            try:
                identity = self.identity()
            except OrcaError:
                if self.child is None or self.child.poll() is not None:
                    raise
                identity = None
            if identity or self.child is None or self.child.poll() is not None:
                break
            if time.monotonic() >= deadline:
                raise OrcaError("internal")
            time.sleep(0.05)
        if identity is None:
            if self.child is not None:
                self.child.wait(timeout=max(0, deadline - time.monotonic()))
                self.child = None
            return
        try:
            os.kill(identity[0], signal.SIGTERM)
        except ProcessLookupError:
            pass
        while self.locked():
            if time.monotonic() >= deadline:
                raise OrcaError("internal")
            try:
                current = self.identity()
            except OrcaError:
                # During exit proc_pidpath (or menu.pid) can disappear before
                # the kernel releases flock. Wait for release after our signal;
                # never send another signal using an unverified identity.
                current = None
            if current not in (None, identity):
                raise OrcaError("internal")
            time.sleep(0.05)
        if self.child is not None:
            self.child.wait(timeout=max(0, deadline - time.monotonic()))
            self.child = None

    def launch(self):
        import time
        expected = (self.paths.root / "current" / "Orca.app" / "Contents" / "MacOS" / "OrcaMenu").resolve()
        identity = self.identity()
        if identity:
            if identity[1] != expected:
                raise OrcaError("internal")
            return
        self.child = open_menu(self.paths)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.child.poll() is not None:
                raise OrcaError("internal")
            # The app publishes its PID after AppKit finishes launching.
            try:
                identity = self.identity()
            except OrcaError:
                identity = None
            if identity and identity[1] == expected:
                return
            time.sleep(0.05)
        raise OrcaError("internal")
