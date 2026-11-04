#!/bin/sh
set -eu
ORCA_SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "${ORCA_PYTHON:-python3}" "$ORCA_SOURCE/tools/install.py" setup "$@"
