#!/usr/bin/env python3
"""Retain the original configured-seed checksum's Python JSON encoding.

Used by the Node provisioning helper; this process never writes credentials.
Raw JSON travels over stdin and only its canonical SHA-256 returns over stdout.
"""
import hashlib
import json
import sys

try:
    value = json.load(sys.stdin)
    canonical = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    print(hashlib.sha256(canonical).hexdigest())
except (ValueError, TypeError, OSError):
    raise SystemExit('ERROR: Configured-seed checksum failed; private diagnostics withheld.') from None
