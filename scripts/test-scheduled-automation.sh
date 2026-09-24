#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

python3 -m unittest discover -s "$REPO_DIR/scripts/tests" -p 'test_reconcile_openclaw_cron.py' -v
