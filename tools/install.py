"""Manual setup/uninstall entry point; never invoked by development tests."""
import argparse
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from orca import __version__


def main():
    parser = argparse.ArgumentParser(description="Install, update or remove Orca (confirmation required before changes)", allow_abbrev=False)
    parser.add_argument("--version", action="version", version=f"orca {__version__}", help="Show version and exit")
    parser.add_argument("action", choices=["setup", "uninstall"], help="Action supplied by the shell script")
    parser.add_argument("--source", action="store_true", help="Install this local source instead of the latest GitHub Release (setup only)")
    args = parser.parse_args()
    if args.source and args.action != "setup":
        parser.error("--source is only supported for setup")
    if sys.platform != "darwin" or sys.version_info < (3, 11):
        parser.error("macOS and Python 3.11 or later are required")
    from orca.installation import Installer
    from orca.release_source import ReleaseSourceError, latest_source
    installer = None
    try:
        installer = Installer(Path(__file__).resolve().parents[1], Path.home(), native=True)
        if args.action == "setup":
            if args.source:
                installer.install()
            else:
                installer.stage = "downloading and verifying the latest GitHub Release"
                with tempfile.TemporaryDirectory(prefix="orca-setup-") as folder:
                    source, version = latest_source(Path(folder))
                    print(f"Installing GitHub Release {version}")
                    installer.source = source
                    installer.version = version
                    installer.install()
        else:
            installer.uninstall()
    except ReleaseSourceError as error:
        stage = installer.stage if installer else "reading the source version"
        print(f"Operation failed while {stage}. {error}", file=sys.stderr)
        return 1
    except (Exception, KeyboardInterrupt) as error:
        print(installer.failure_message(error) if installer else "Operation failed while preparing the installer", file=sys.stderr)
        return 1
    return 0


raise SystemExit(main())
