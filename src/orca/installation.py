"""Explicit installer operations. No installation is performed by importing this module.

Tests inject a project-local user directory, builder, service controller and
confirmation callback. Native launchctl/Keychain/Codex are not used by tests.
"""
import asyncio
import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import tomllib
import uuid
from contextlib import closing
from pathlib import Path
from .config import ToolRegistry, load_config
from .errors import OrcaError
from .platforms.macos import InstanceLock, LoginItems, Paths, menu_launch_command, private_directory
from .release_source import source_version

MARKER = "orca-install-v1"


def exclusive_write(path: Path, data: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def atomic_write(path: Path, data: bytes, mode=0o600):
    fd, name = tempfile.mkstemp(prefix=".orca-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def register_codex(path: Path, command: Path, arguments: list[str]):
    """Append only after explicit confirmation, keeping pre-existing TOML bytes."""
    if path.is_symlink():
        raise OrcaError("internal")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    old = path.read_bytes() if path.exists() else b""
    try:
        data = tomllib.loads(old.decode())
        servers = data.get("mcp_servers", {})
        if type(servers) is not dict:
            raise ValueError()
        if "orca" in servers:
            expected = {"command": str(command), "args": arguments}
            if servers["orca"] == expected:
                return False
            raise OrcaError("config")
        addition = ("\n[mcp_servers.orca]\ncommand = " + json.dumps(str(command))
                    + "\nargs = " + json.dumps(arguments) + "\n").encode()
        tomllib.loads((old + addition).decode())
    except (ValueError, UnicodeError):
        raise OrcaError("config") from None
    if old:
        exclusive_write(path.with_name(path.name + ".orca-backup-" + uuid.uuid4().hex), old)
    # Recheck before commit; never silently replace a concurrently edited config.
    if (path.read_bytes() if path.exists() else b"") != old:
        raise OrcaError("internal")
    atomic_write(path, old + addition)
    return True


class Installer:
    def __init__(self, source: Path, user_home: Path, *, builder=None, control=None, menu_control=None, confirm=None, native=False):
        self.source = source
        self.version = source_version(source)
        self.user_home = user_home
        self.paths = Paths(user_home / "Library" / "Application Support" / "Orca")
        self.bin = user_home / ".local" / "bin" / "orca"
        self.profile = user_home / ".zprofile"
        self.agents = LoginItems(user_home / "Library" / "LaunchAgents")
        self.confirm = confirm or (lambda prompt: input(prompt + " [y/N] ").strip().lower() == "y")
        self.builder = builder or self.build
        self.control = control or self.service_control
        self.native = native
        from .platforms.macos import InstalledMenu
        self.menu = InstalledMenu(self.paths)
        self.menu_control = menu_control or self.menu_service_control
        self.stage = "checking the installation"

    def failure_message(self, error):
        # Never format exception text, command arguments, or configuration values.
        if isinstance(error, OrcaError):
            reason = error.terminal()
        elif isinstance(error, PermissionError):
            reason = "Access was denied"
        elif isinstance(error, FileNotFoundError):
            reason = "A required file or command was not found"
        elif isinstance(error, sqlite3.Error):
            reason = "The database operation failed"
        elif isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
            reason = "The operation timed out"
        elif isinstance(error, subprocess.SubprocessError):
            reason = "A build or service subprocess failed"
        elif isinstance(error, KeyboardInterrupt):
            reason = "The operation was interrupted"
        else:
            reason = "Check the existing configuration and installation directory"
        return f"Operation failed while {self.stage}. {reason}"

    def menu_service_control(self, operation):
        if not self.native:
            return False
        if operation == "status":
            return self.menu.identity() is not None
        if operation == "stop":
            return self.menu.stop()
        if operation == "launch":
            return self.menu.launch()
        raise OrcaError("invalid_input")

    @property
    def marker(self):
        return self.paths.root / "installed.json"

    def current(self):
        current = self.paths.root / "current"
        if not current.is_symlink():
            raise OrcaError("config")
        release = current.resolve()
        if release.parent != self.paths.root / "releases" or not self.owned_release(release):
            raise OrcaError("config")
        return release

    def owned_release(self, path: Path):
        if (not re.fullmatch(r"[0-9a-f]{32}", path.name) or path.is_symlink()
                or not path.is_dir()):
            return False
        marker = path / "release.json"
        if marker.is_symlink():
            return False
        try:
            with marker.open("rb") as stream:
                raw = stream.read(1025)
                if len(raw) > 1024:
                    return False
                data = json.loads(raw)
            return data == {"format": MARKER, "release": path.name}
        except (OSError, ValueError, UnicodeError):
            return False

    def wrapper(self):
        python = shlex.quote(str(self.paths.root / "current" / "venv" / "bin" / "python"))
        arguments = shlex.join(self.paths.arguments())
        return f'#!/bin/sh\n# {MARKER}\nexec {python} -m orca {arguments} "$@"\n'.encode()

    def check_owned(self):
        if self.marker.is_symlink() or not self.marker.exists():
            raise OrcaError("config")
        if json.loads(self.marker.read_text()).get("format") != MARKER:
            raise OrcaError("config")
        self.current()
        if self.bin.is_symlink() or (self.bin.exists() and self.bin.read_bytes() != self.wrapper()):
            raise OrcaError("config")

    def build(self, release: Path):
        payload = release / "source"
        payload.mkdir()
        for name in ("src", "examples", "third_party", "macos", "tools"):
            shutil.copytree(self.source / name, payload / name,
                            ignore=shutil.ignore_patterns(".build", "__pycache__", "*.pyc"))
        for name in ("pyproject.toml", "README.md", "LICENSE", "requirements-lock.txt", "uninstall.sh"):
            shutil.copy2(self.source / name, payload / name)
        environment = release / "venv"
        build_env = {**os.environ, "LC_ALL": "C", "LANG": "C", "LANGUAGE": "C"}
        subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True, env=build_env)
        python = str(environment / "bin" / "python")
        subprocess.run([python, "-m", "pip", "install", "--no-cache-dir", "--no-deps", "-r", str(payload / "requirements-lock.txt")], check=True, env=build_env)
        subprocess.run([python, "-m", "pip", "install", "--no-build-isolation", "--no-deps", str(payload)], check=True, env=build_env)
        self.stage = "checking ~/.config/orca support in the new release"
        check = ("import sys; from pathlib import Path; from orca.platforms.macos import Paths; "
                 "p = Paths(Path(sys.argv[1])); "
                 "sys.exit((str(p.config), str(p.models), str(p.tools)) != tuple(sys.argv[2:]))")
        result = subprocess.run([python, "-I", "-c", check, str(self.paths.root),
                                 str(self.paths.config), str(self.paths.models), str(self.paths.tools)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=build_env)
        if result.returncode:
            print("The selected release does not support ~/.config/orca with tools.json. Use an updated release or ./setup.sh --source")
            raise OrcaError("config")
        self.stage = "building the new release"
        subprocess.run(["/bin/sh", str(payload / "tools" / "build_macos.sh")], check=True,
                       env={**build_env, "ORCA_PYTHON": python})
        shutil.copytree(payload / "macos" / ".build" / "artifacts" / "Orca.app", release / "Orca.app")

    def service_control(self, operation):
        import asyncio
        from .ipc import request
        from .cli import launch, stop
        async def action():
            if operation == "status":
                try:
                    await request(self.paths.socket, "snapshot")
                    return True
                except OrcaError as error:
                    if error.code == "stopped":
                        return False
                    raise
            if operation == "stop":
                await stop(self.paths)
            elif operation == "launch":
                # Invoke the installed Python, not the setup interpreter.
                await launch(self.paths, executable=str(self.paths.root / "current" / "venv" / "bin" / "python"))
        return asyncio.run(action())

    def login_commands(self):
        python = str(self.paths.root / "current" / "venv" / "bin" / "python")
        return [python, "-m", "orca", *self.paths.arguments(), "daemon"], menu_launch_command(self.paths)

    def sync_login(self):
        config = load_config(self.paths.config)
        self.agents.sync(config.auto_start, *self.login_commands())

    def preflight(self, *, settings=True):
        root = self.paths.root
        # Refuse ambiguous link targets before reading backups or mutating anything.
        paths = [root, self.paths.config_dir, self.marker, self.bin, root / "releases", self.agents.directory]
        if settings:
            paths += [self.profile, self.paths.config, self.paths.models, self.paths.tools, self.paths.database,
                      root / "config.json", root / "models.json"]
        for path in paths:
            relative = path.relative_to(self.user_home)
            current = self.user_home
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    raise OrcaError("config")
        for label in self.agents.LABELS:
            if (self.agents.directory / f"{label}.plist").is_symlink():
                raise OrcaError("config")

    def install(self):
        self.stage = "checking the installation"
        self.preflight()
        root = self.paths.root
        existing = self.marker.exists()
        if existing:
            self.check_owned()
            if not self.confirm("Update the existing Orca installation? Configuration, history and logs will be preserved"):
                return False
        elif root.exists() and any(root.iterdir()):
            # A logs-only directory may remain after uninstall.
            if set(p.name for p in root.iterdir()) - {"logs", ".installation.lock"}:
                raise OrcaError("config")
        if self.bin.is_symlink() or (self.bin.exists() and self.bin.read_bytes() != self.wrapper()):
            raise OrcaError("config")
        private_directory(root)
        # Keep this inode after uninstall so overlapping commands cannot lock
        # different replacement files. Declining an update never creates it.
        self.stage = "acquiring the installation lock"
        with closing(InstanceLock(root / ".installation.lock")):
            self.stage = "checking the installation"
            self.preflight()
            if self.marker.exists() != existing:
                raise OrcaError("config")
            if existing:
                self.check_owned()
            settings = self.missing_configuration(existing)
            if existing and settings:
                self.stage = "confirming missing configuration files"
                print("Missing configuration files:")
                descriptions = {"config.json": "default settings", "models.json": "empty model list",
                                "tools.json": "generate_text with all models"}
                for target, source in settings:
                    origin = ("copy existing legacy settings" if source.parent == root
                              else f"bundled template ({descriptions[target.name]})")
                    print(f"  {target.name}: {origin}")
                if not self.confirm("Create the missing configuration files listed above? Existing files will be preserved"):
                    print("Update cancelled. Missing configuration files were not created")
                    return False
            return self._install(existing, settings)

    def missing_configuration(self, existing):
        root = self.paths.root
        legacy_settings = existing and "config_layout" not in json.loads(self.marker.read_bytes())
        settings = []
        for target in (self.paths.config, self.paths.models, self.paths.tools):
            if target.exists():
                if not target.is_file():
                    raise OrcaError("config")
                continue
            legacy = root / target.name
            source = (legacy if legacy_settings and target != self.paths.tools and legacy.is_file()
                      else self.source / "examples" / target.name)
            settings.append((target, source))
        return settings

    def _install(self, existing, settings):
        self.stage = "preparing the new release"
        root = self.paths.root
        releases = root / "releases"
        old = self.current() if existing else None
        running = False
        stopped = False
        switched = False
        restart_attempted = False
        menu_running = menu_stopped = menu_restart_attempted = False
        created = []
        db_backup = None
        db_backup_ready = False
        preserve_recovery = False
        had_database = self.paths.database.exists()
        original_profile = original_bin = original_marker = None
        profile_changed = bin_changed = marker_changed = agents_changed = False
        agent_backups = {}
        original_modes = {}
        private_directory(releases)
        release = releases / uuid.uuid4().hex
        release.mkdir(mode=0o700)
        try:
            self.stage = "building the new release"
            self.builder(release)
            self.stage = "validating the installation and configuration"
            exclusive_write(release / "release.json", json.dumps({"format": MARKER, "release": release.name}).encode())
            self.preflight()
            if self.marker.exists() != existing or (self.bin.exists() and self.bin.read_bytes() != self.wrapper()):
                raise OrcaError("config")
            if existing:
                self.check_owned()
                old = self.current()
            original_bin = self.bin.read_bytes() if self.bin.exists() else None
            original_marker = self.marker.read_bytes() if self.marker.exists() else None
            agent_backups = {label: (self.agents.directory / f"{label}.plist").read_bytes()
                             if (self.agents.directory / f"{label}.plist").exists() else None for label in self.agents.LABELS}
            original_modes = {path: stat.S_IMODE(path.stat().st_mode) for path in
                              (self.bin, self.marker, *(self.agents.directory / f"{label}.plist" for label in self.agents.LABELS))
                              if path.exists()}
            approved = {target for target, _ in settings}
            for target in (self.paths.config, self.paths.models, self.paths.tools):
                if (target.exists() and not target.is_file()) or (not target.exists() and target not in approved):
                    raise OrcaError("config")
            private_directory(self.paths.config_dir)
            # Create only the missing files approved before building. Preserve
            # files supplied concurrently and legacy originals for rollback.
            for target, source in settings:
                if not target.exists():
                    exclusive_write(target, source.read_bytes())
                    created.append(target)
            load_config(self.paths.config)
            ToolRegistry(self.paths.tools).reload()
            if existing:
                self.stage = "checking the menu bar app"
                menu_running = bool(self.menu_control("status"))
                self.stage = "checking the Orca service"
                running = bool(self.control("status"))
                if running:
                    print("Cancelling pending and running requests and stopping the service for the update")
                    self.stage = "stopping the Orca service (up to 10 seconds)"
                    self.control("stop")
                    stopped = True
                if menu_running:
                    self.stage = "stopping the menu bar app (up to 10 seconds)"
                    self.menu_control("stop")
                    menu_stopped = True
                if self.paths.database.exists():
                    self.stage = "backing up the database"
                    db_backup = root / (".rollback-" + uuid.uuid4().hex + ".sqlite3")
                    exclusive_write(db_backup, b"")
                    with closing(sqlite3.connect(self.paths.database)) as source, closing(sqlite3.connect(db_backup)) as backup:
                        if source.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                            raise OrcaError("config")
                        source.backup(backup)
                    os.chmod(db_backup, 0o600)
                    db_backup_ready = True
            self.stage = "switching to the new release"
            link = root / (".current-" + uuid.uuid4().hex)
            link.symlink_to(release.relative_to(root))
            os.replace(link, root / "current")
            switched = True
            self.stage = "updating the CLI and PATH entry"
            self.bin.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.bin, self.wrapper(), 0o700)
            bin_changed = True
            if self.profile.is_symlink():
                raise OrcaError("config")
            profile = self.profile.read_bytes() if self.profile.exists() else b""
            original_profile = profile if self.profile.exists() else None
            if original_profile is not None:
                original_modes[self.profile] = stat.S_IMODE(self.profile.stat().st_mode)
            line = f'\n# Orca PATH\nexport PATH={shlex.quote(str(self.bin.parent))}:"$PATH"\n'.encode()
            if line not in profile:
                atomic_write(self.profile, profile + line, original_modes.get(self.profile, 0o600))
                profile_changed = True
            agents_changed = True
            self.stage = "synchronizing login settings"
            self.sync_login()
            self.stage = "writing the installation record"
            atomic_write(self.marker, json.dumps({"format": MARKER, "version": self.version,
                         "user_home": str(self.user_home), "config_layout": "dot-config-v1"}).encode())
            marker_changed = True
            if running:
                restart_attempted = True
                self.stage = "starting the updated Orca service"
                self.control("launch")
            if menu_running:
                menu_restart_attempted = True
                self.stage = "starting the updated menu bar app (up to 10 seconds)"
                self.menu_control("launch")
        except BaseException:
            failed_stage = self.stage
            self.stage = "recovering the previous installation"
            # Restore the previous program and metadata; logs are never rolled back.
            preserve_recovery = True
            # Never restore a database while a partially started replacement owns it.
            try:
                if menu_restart_attempted:
                    self.menu_control("stop")
                if restart_attempted:
                    self.control("stop")
                if switched and db_backup_ready:
                    with closing(sqlite3.connect(db_backup)) as source, closing(sqlite3.connect(self.paths.database)) as target:
                        source.backup(target)
                elif switched and not had_database:
                    for suffix in ("", "-journal", "-wal", "-shm"):
                        self.paths.database.with_name(self.paths.database.name + suffix).unlink(missing_ok=True)
            except BaseException:
                preserve_recovery = True
                print(f"Automatic recovery failed. Both releases and recovery files have been preserved: {root}")
                raise
            if switched:
                (root / "current").unlink(missing_ok=True)
                if old:
                    (root / "current").symlink_to(old.relative_to(root))
            for path, data, changed in [(self.profile, original_profile, profile_changed),
                                        (self.bin, original_bin, bin_changed),
                                        (self.marker, original_marker, marker_changed)]:
                if not changed:
                    continue
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, data, original_modes.get(path, 0o700 if path == self.bin else 0o600))
            for label, data in agent_backups.items() if agents_changed else ():
                path = self.agents.directory / f"{label}.plist"
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, data, original_modes.get(path, 0o600))
            for path in created:
                path.unlink(missing_ok=True)
            shutil.rmtree(release)
            if not existing:
                try:
                    releases.rmdir()
                except OSError:
                    pass
            if stopped and old:
                self.stage = "restarting the previous Orca service during recovery"
                self.control("launch")
            if menu_stopped and old:
                self.stage = "restarting the previous menu bar app during recovery (up to 10 seconds)"
                self.menu_control("launch")
            preserve_recovery = False
            self.stage = failed_stage
            raise
        finally:
            if db_backup and not preserve_recovery:
                db_backup.unlink(missing_ok=True)
        self.stage = "registering Orca with Codex (Orca installation already completed)"
        if self.confirm("Register Orca as a Codex MCP server? Existing settings will be preserved"):
            register_codex(self.user_home / ".codex" / "config.toml",
                           self.paths.root / "current" / "venv" / "bin" / "python",
                           ["-I", "-m", "orca.mcp", *self.paths.arguments()])
        print(f"Orca installed: {root}\nVersion: {self.version}\nRun orca launch in a new terminal")
        print("The service and menu bar app are not started automatically on first install")
        print("orca launch opens the menu bar app too; use orca menu if the daemon is already running")
        return True

    def uninstall(self):
        self.stage = "checking the installation"
        self.preflight(settings=False)
        self.check_owned()
        if not self.confirm("Remove Orca, its configuration and database? Logs, Codex settings and Keychain will be preserved"):
            return False
        self.stage = "acquiring the installation lock"
        with closing(InstanceLock(self.paths.root / ".installation.lock")):
            self.preflight(settings=False)
            self.check_owned()
            return self._uninstall()

    def _uninstall(self):
        self.stage = "stopping the Orca service (up to 10 seconds)"
        self.control("stop")
        self.stage = "stopping the menu bar app (up to 10 seconds)"
        self.menu_control("stop")
        self.stage = "stopping the local model manager"
        from .model_manager import shutdown_manager
        asyncio.run(shutdown_manager(self.paths))
        self.stage = "removing the Orca installation"
        if self.native:
            for label in self.agents.LABELS:
                # KeepAlive is false, so stopping does not trigger a restart.
                subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        self.agents.sync(False, [], [])
        root = self.paths.root
        self.bin.unlink(missing_ok=True)
        (root / "current").unlink()
        # Delete only enumerated Orca-owned entries, never the root or its logs.
        releases = root / "releases"
        for release in releases.iterdir():
            if self.owned_release(release):
                shutil.rmtree(release)
        try:
            releases.rmdir()
        except OSError:
            pass
        for name in ("config.json", "models.json", "orca.sqlite3", "orca.sqlite3-journal", "orca.sqlite3-wal",
                     "orca.sqlite3-shm", "daemon.lock", "installed.json", "menu.lock", "menu.pid"):
            path = root / name
            if path.is_symlink():
                path.unlink()
            elif path.is_file():
                path.unlink()
        for path in (self.paths.config, self.paths.models, self.paths.tools):
            if path.is_symlink() or path.is_file():
                path.unlink()
        try:
            self.paths.config_dir.rmdir()
        except OSError:
            pass
        print("Orca removed. Error logs, Codex registration and PATH entries have been preserved")
        return True
