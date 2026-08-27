#!/usr/bin/env bash
# Thin wrapper so you can run ./tf instead of activating the venv.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$here/.venv/bin/python" -m telefetcher "$@"
