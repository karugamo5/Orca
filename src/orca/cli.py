"""Command line lifecycle, connection checks and one-shot monitor."""
import argparse
import asyncio
import json
import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from importlib.resources import files
from . import __version__
from .config import ModelSelectionError, load_models, select_models
from .errors import CredentialError, ErrorLog, OrcaError
from .ipc import monitor_pages, request
from .platforms.macos import InstanceLock, Paths


async def status(paths):
    try:
        return await request(paths.socket, "snapshot")
    except OrcaError as error:
        if error.code == "stopped":
            return None
        raise


async def launch(paths, *, executable=None, model_ids=None):
    if model_ids is not None:
        select_models(load_models(paths.models), model_ids)
        model_ids = sorted(set(model_ids))

    def check_scope(snapshot):
        if model_ids is not None and snapshot.get("launch_models") != model_ids:
            raise ModelSelectionError("The running daemon uses a different model selection. Run orca stop, then launch --model")

    existing = await status(paths)
    if existing:
        for _ in range(300):
            if existing.get("ready"):
                check_scope(existing)
                return
            await asyncio.sleep(0.1)
            existing = await status(paths)
            if existing is None:
                break
        else:
            raise OrcaError("internal")
    command = [executable or sys.executable, "-m", "orca", *paths.arguments()]
    process = subprocess.Popen(command + ["daemon"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True,
                               env={**os.environ, "ORCA_LAUNCH_MODELS": json.dumps(model_ids)})
    try:
        for _ in range(300):
            current = await status(paths)
            if current and current.get("ready"):
                check_scope(current)
                return
            if process.poll() is not None and current is None:
                raise OrcaError("config")
            await asyncio.sleep(0.1)
        raise OrcaError("internal")
    finally:
        if process.poll() is None and not (await status(paths) or {}).get("ready"):
            process.terminate()
            await asyncio.to_thread(process.wait, 10)


async def stop(paths):
    if await status(paths) is not None:
        await request(paths.socket, "stop")
    for _ in range(100):
        # The listener closes before requests, backend, DB and socket cleanup finish.
        # Poll the lock first: probing a closing listener can reset the connection.
        if not InstanceLock.is_held(paths.root / "daemon.lock") and await status(paths) is None:
            return
        await asyncio.sleep(0.1)
    raise OrcaError("internal")


async def check_connections(paths, model_ids=None):
    from .backend import OpenAIBackend
    from .config import load_config, load_models
    from .platforms.macos import Keychain

    config = load_config(paths.config)
    models = select_models(load_models(paths.models), model_ids)
    if not models:
        raise OrcaError("no_models")
    backend = OpenAIBackend(config, Keychain())
    succeeded = 0
    try:
        for model in models:
            authentication = ("JSON token authentication" if model.token is not None else
                              "Keychain authentication" if model.keychain else "no authentication")
            key_result = "OK" if model.keychain or model.token is not None else "N/A"
            api_result = "NG"
            try:
                online = await backend.health(model)
            except CredentialError:
                key_result = "NG"
                api_result = "SKIP"
                detail = "Failed to retrieve API key (Keychain)"
            except OrcaError as error:
                public = error.public()
                detail = "API connection or response failed: " + error.terminal()
                if "http_status" in public:
                    detail += f" (HTTP {public['http_status']})"
            except Exception:
                ErrorLog(paths.log).record("backend")
                detail = "API connection or response failed: " + OrcaError("internal").terminal()
            else:
                if online:
                    succeeded += 1
                    print(f"OK {model.id} ({authentication}) key={key_result} API=OK")
                    continue
                detail = "API response validation failed: configured model is not available in the model list"
            print(f"NG {model.id} ({authentication}) key={key_result} API={api_result}: {detail}")
        failed = len(models) - succeeded
        print(f"Connection check: passed {succeeded} / failed {failed} / total {len(models)}")
        return 1 if failed else 0
    finally:
        await backend.close()


async def set_auto_start(paths, enabled):
    from .config import MAX_CONFIG, load_config, load_models
    from .installation import atomic_write
    from .platforms.macos import sync_installed_login

    if paths.config.is_symlink() or paths.models.is_symlink():
        raise OrcaError("config")
    load_config(paths.config)
    load_models(paths.models)
    original = paths.config.read_bytes()
    models = paths.models.read_bytes()
    if max(len(original), len(models)) > MAX_CONFIG:
        raise OrcaError("config")
    print("Checking API keys and connections for all models before changing auto-start")
    if await check_connections(paths):
        print("Some connection checks failed; auto-start settings were not changed")
        return 1
    if (paths.config.is_symlink() or paths.models.is_symlink()
            or paths.config.read_bytes() != original or paths.models.read_bytes() != models):
        print("Configuration changed during checks; auto-start settings were not changed", file=sys.stderr)
        return 1
    data = json.loads(original)
    data["auto_start"] = enabled
    replacement = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
    if len(replacement) > MAX_CONFIG:
        raise OrcaError("config")
    atomic_write(paths.config, replacement)
    try:
        sync_installed_login(paths)
    except BaseException:
        atomic_write(paths.config, original)
        raise
    state = "enabled" if enabled else "disabled"
    print(f"Login auto-start {state} (current service state unchanged)")
    if not (paths.root / "installed.json").exists():
        print("Only development settings were updated. LaunchAgents were not changed")
    return 0


async def run(args):
    paths = resolve_paths(args)
    if args.command == "auto_start" or (args.command == "model" and args.set) or (args.command in {"launch", "stop", "restart", "menu"}
                                        and (paths.root / "installed.json").exists()):
        with closing(InstanceLock(paths.root / ".installation.lock")):
            return await dispatch(args, paths)
    return await dispatch(args, paths)


async def start_installed_menu(paths):
    if not (paths.root / "installed.json").exists():
        return 0
    from .platforms.macos import InstalledMenu
    try:
        await asyncio.to_thread(InstalledMenu(paths).launch)
    except Exception:
        ErrorLog(paths.log).record("lifecycle")
        print("Orca is running, but the menu bar app could not start. Run orca menu to retry", file=sys.stderr)
        return 1
    return 0


async def restart(paths):
    await stop(paths)
    from .platforms.macos import sync_installed_login
    sync_installed_login(paths)
    await launch(paths)
    if await start_installed_menu(paths):
        return 1
    print("Orca restarted")


async def dispatch(args, paths):
    if args.command == "tool" or (args.command == "model" and any(
            getattr(args, name, None) for name in ("add", "settings", "enable", "disable", "refresh"))):
        from .management import operate
        from .config import MAX_CONFIG, decode_json
        payload = decode_json(sys.stdin.buffer.read(MAX_CONFIG + 1)) if args.input_json else None
        result = await operate(args, paths, payload)
        print(json.dumps({"v": 1, "ok": True, "result": result}, ensure_ascii=False) if args.json else "Operation completed")
        return 0
    if args.command == "test":
        return await check_connections(paths, args.model)
    elif args.command == "model":
        if not args.set:
            return await manage_model(args, paths)
        from .model_setup import confirm_restart, edit_model
        if not edit_model(paths):
            return 130
        if confirm_restart():
            return await restart(paths)
        print("Orca was not restarted; model changes are also detected automatically while running")
        return 0
    elif args.command == "model_service":
        from .model_manager import ModelManager
        await ModelManager(paths).run()
    elif args.command == "auto_start":
        return await set_auto_start(paths, args.auto_start == "true")
    elif args.command == "daemon":
        from .daemon import Daemon
        model_ids = json.loads(os.environ.pop("ORCA_LAUNCH_MODELS", "null"))
        if model_ids is not None and (type(model_ids) is not list or not model_ids
                                      or any(not isinstance(item, str) for item in model_ids)):
            raise OrcaError("config")
        await Daemon(paths, model_ids=model_ids).run()
    elif args.command == "launch":
        from .platforms.macos import sync_installed_login
        if args.model is not None:
            select_models(load_models(paths.models), args.model)
        sync_installed_login(paths)
        await launch(paths, model_ids=args.model)
        if await start_installed_menu(paths):
            return 1
        print("Orca started")
    elif args.command == "stop":
        await stop(paths)
        print("Orca is stopped")
    elif args.command == "restart":
        return await restart(paths)
    elif args.command == "menu":
        from .platforms.macos import InstalledMenu
        await asyncio.to_thread(InstalledMenu(paths).launch)
    elif args.command == "monitor":
        first = True
        try:
            async for page in monitor_pages(paths.socket):
                if first:
                    limit = page["max_pending"] or "unlimited"
                    print(f"Orca: {page['state']} / pending {page['pending_count']} / limit {limit}")
                    first = False
                for row in page["requests"]:
                    print(f"{row['request_id']} {row['source']} {row['status']} received:{row['received_at']} started:{row['started_at'] or '-'} model:{row['model_id'] or '-'}")
        except OrcaError as error:
            if error.code == "stopped" and first:
                print("Orca is stopped")
                return
            raise


def resolve_paths(args):
    root = Path(args.home).expanduser().absolute() if args.home else Paths.default().root
    runtime = Path(args.runtime_dir).expanduser().absolute() if args.runtime_dir else None
    return Paths(root, runtime)


async def manage_model(args, paths):
    from .model_manager import ensure_manager
    model_id = args.start or args.stop
    if args.start:
        selected = select_models(load_models(paths.models), [model_id])[0]
        if selected.local_start is None:
            if args.json:
                raise OrcaError("invalid_input")
            print("The model has no local start command", file=sys.stderr)
            return 1
        timeout = selected.local_start.startup_timeout_seconds + 10
    else:
        timeout = 10
    snapshot = await ensure_manager(paths)
    if args.status:
        if args.json:
            print(json.dumps({"v": 1, "ok": True, "result": snapshot}, ensure_ascii=False))
            return 0
        if not snapshot["models"]:
            print("No models are configured")
        for row in snapshot["models"]:
            detail = f" error:{row['error']}" if row["error"] else ""
            print(f"{row['id']} {row['process_state']} API:{row['health']}{detail}")
        if snapshot["config_error"]:
            raise OrcaError("config")
        return 0
    with closing(InstanceLock(paths.root / ".installation.lock")):
        await request(paths.models_socket, "start" if args.start else "stop", {"model_id": model_id})
    if args.wait == "false":
        result = {"accepted": True, "model_id": model_id}
        print(json.dumps({"v": 1, "ok": True, "result": result}) if args.json else "Operation accepted")
        return 0
    async with asyncio.timeout(timeout):
        while True:
            snapshot = await request(paths.models_socket, "snapshot")
            row = next((row for row in snapshot["models"] if row["id"] == model_id), None)
            if row is None:
                if args.stop:
                    print(f"Model {model_id} stopped")
                    return 0
                raise OrcaError("internal")
            if row["error"]:
                print(f"Model {model_id}: {row['error']}", file=sys.stderr)
                return 1
            if args.start and row["process_state"] == "stopped":
                print(f"Model {model_id}: start_cancelled", file=sys.stderr)
                return 1
            if row["process_state"] == ("running" if args.start else "stopped"):
                print(f"Model {model_id} {'started' if args.start else 'stopped'}")
                return 0
            await asyncio.sleep(0.1)


def make_parser():
    # Suppressed defaults preserve global paths when options follow the action.
    common = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    common.add_argument("--home", default=argparse.SUPPRESS, metavar="DIR",
                        help="Orca data directory (default: ~/Library/Application Support/Orca; configuration: ~/.config/orca). Custom roots also contain configuration")
    common.add_argument("--runtime-dir", default=argparse.SUPPRESS, metavar="DIR",
                        help="Unix socket directory (for isolated development)")
    parser = argparse.ArgumentParser(prog="orca", description="Local LLM service for macOS",
        parents=[common], allow_abbrev=False, epilog="Examples: orca launch --model local-a local-b\n"
        "    orca --auto_start true\nExit codes: 0 success / 1 operation failed / 2 invalid arguments / 130 interrupted",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    information = parser.add_mutually_exclusive_group()
    information.add_argument("--version", action="store_true", help="Show version and exit")
    information.add_argument("--license", action="store_true", help="Show the Orca license and exit")
    parser.add_argument("--auto_start", choices=["true", "false"], help="Set login auto-start after checking all model connections")
    parser.set_defaults(model=None)
    commands = parser.add_subparsers(dest="command", title="commands", metavar="COMMAND")
    descriptions = {
        "launch": "Start the daemon and open the installed menu bar app",
        "stop": "Cancel pending and running requests and stop the daemon",
        "restart": "Restart and reload configuration, then open the installed menu bar app (clear model selection)",
        "monitor": "Show requests in acceptance order and exit",
        "test": "Check API keys and connections for configured models (no inference)",
        "model": "Configure models or start, stop and inspect local model servers independently of Orca requests",
        "tool": "Configure public tools and model assignments",
        "menu": "Open the installed menu bar app",
        "daemon": "Internal: run the daemon in the foreground",
        "model_service": "Internal: run the independent local model manager",
    }
    for name, description in descriptions.items():
        command = commands.add_parser(name, help=description, description=description,
                                      parents=[common], allow_abbrev=False)
        if name in {"launch", "test"}:
            command.add_argument("--model", nargs="+", action="extend", metavar="ID",
                help="Select models by models.json id (multiple allowed; default: all models)")
        elif name in {"model", "tool"}:
            command.add_argument("--json", action="store_true", help="Return a versioned JSON result")
            command.add_argument("--input_json", action="store_true", help="Read one bounded JSON object from stdin")
            command.add_argument("--wait", choices=["true", "false"], default=None, help="Wait for completion (default: true)")
            action = command.add_mutually_exclusive_group(required=True)
            action.add_argument("--add", action="store_true", help="Add a definition from stdin JSON (requires --input_json)")
            action.add_argument("--settings", action="store_true", help="Read safe configuration choices and revision")
            if name == "tool":
                action.add_argument("--assign", metavar="ID", help="Save model assignments from stdin JSON")
                continue
            action.add_argument("--enable", metavar="ID", help="Allow new requests to use a model")
            action.add_argument("--disable", metavar="ID", help="Prevent new requests from using a model")
            action.add_argument("--refresh", action="store_true", help="Refresh runtime model health without inference")
            action.add_argument("--set", action="store_true",
                                 help="Configure one model interactively (tokens can only be entered in JSON)")
            action.add_argument("--start", metavar="ID", help="Start a configured local model server")
            action.add_argument("--stop", metavar="ID", help="Stop an owned model server without waiting for requests")
            action.add_argument("--status", action="store_true", help="Show model process and API states")
    return parser


def license_text():
    resource = files("orca").joinpath("LICENSE")
    if resource.is_file():
        return resource.read_text(encoding="utf-8")
    # Source checkout / sdist; wheels carry the license as a package resource.
    return (Path(__file__).resolve().parents[2] / "LICENSE").read_text(encoding="utf-8")


def main():
    parser = make_parser()
    args = parser.parse_args()
    args.home = getattr(args, "home", None)
    args.runtime_dir = getattr(args, "runtime_dir", None)
    if args.version or args.license:
        if args.command is not None or args.auto_start is not None:
            parser.error("--version/--license cannot be combined with a command or --auto_start")
        if args.version:
            print(f"orca {__version__}")
        else:
            print(license_text(), end="")
        return 0
    if args.auto_start is not None:
        if args.command is not None:
            parser.error("--auto_start cannot be combined with a command")
        args.command = "auto_start"
    elif args.command is None:
        parser.error("Specify a command or --auto_start true/false")
    if args.command in {"model", "tool"}:
        needs_input = args.add or bool(getattr(args, "assign", None))
        if args.input_json != needs_input:
            parser.error("--input_json is required only for --add/--assign")
        can_wait = args.command == "model" and bool(args.start or args.stop or args.refresh)
        if args.wait is not None and not can_wait:
            parser.error("--wait requires model --start/--stop/--refresh")
        args.wait = args.wait or "true"
        if args.json and args.command == "model" and (args.set or ((args.start or args.stop) and args.wait == "true")):
            parser.error("--json requires a non-interactive operation; start/stop require --wait false")
    try:
        return asyncio.run(run(args)) or 0
    except ModelSelectionError as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except OrcaError as error:
        if getattr(args, "json", False):
            print(json.dumps({"v": 1, "ok": False, "error": {"code": error.code, "message": error.terminal()}}))
            return 1
        print(error.terminal(), file=sys.stderr)
        return 1
    except Exception:
        paths = resolve_paths(args)
        ErrorLog(paths.log).record("lifecycle")
        if getattr(args, "json", False):
            error = OrcaError("internal")
            print(json.dumps({"v": 1, "ok": False, "error": {"code": error.code, "message": error.terminal()}}))
            return 1
        print(OrcaError("internal").terminal(), file=sys.stderr)
        return 1
