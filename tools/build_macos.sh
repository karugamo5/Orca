#!/bin/sh
set -eu
export LC_ALL=C LANG=C LANGUAGE=C
ORCA_SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ORCA_BUILD="$ORCA_SOURCE/macos/.build"
mkdir -p "$ORCA_BUILD/tmp" "$ORCA_BUILD/clang"
export TMPDIR="$ORCA_BUILD/tmp"
export CLANG_MODULE_CACHE_PATH="$ORCA_BUILD/clang"
export SWIFTPM_MODULECACHE_OVERRIDE="$ORCA_BUILD/clang"
swift build --quiet --package-path "$ORCA_SOURCE/macos" --configuration release --cache-path "$ORCA_BUILD/cache" --config-path "$ORCA_BUILD/config" --security-path "$ORCA_BUILD/security" --disable-sandbox
ORCA_APP="$ORCA_BUILD/artifacts/Orca.app"
mkdir -p "$ORCA_APP/Contents/MacOS"
cp "$ORCA_BUILD/release/OrcaMenu" "$ORCA_APP/Contents/MacOS/OrcaMenu"
"${ORCA_PYTHON:-python3}" "$ORCA_SOURCE/tools/prepare_macos.py" "$ORCA_APP"
printf '%s\n' "$ORCA_APP"
