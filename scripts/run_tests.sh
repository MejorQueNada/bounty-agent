#!/usr/bin/env bash
# Run the full test suite (stdlib unittest, no network, no secrets required).
set -euo pipefail
cd "$(dirname "$0")/.."

for t in tests/test_scout.py tests/test_watch_proposals.py \
         tests/test_notify_telegram.py tests/test_verify.py; do
  echo "== $t =="
  python3 "$t" -v
done
echo "All test suites passed."