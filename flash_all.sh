#!/usr/bin/env bash
# Fresh installs only; the Python installer owns backup, validation and readback.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${PYTHON:-python3}" "$HERE/scripts/install.py" "$@"
