#!/usr/bin/env bash
set -euo pipefail

# One-click wrapper: writes a single redacted log file.
#
# Usage:
#   export ARGUS_TOKEN="..."   # optional (but strongly recommended)
#   bash docs/debug/collect-debug-info-to-file.sh
#
# Output:
#   /tmp/argus-debug-YYYYMMDD-HHMMSS.log

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TS="$(date -u +%Y%m%d-%H%M%S)"
LOG_FILE="${LOG_FILE:-/tmp/argus-debug-${TS}.log}"

{
  echo "Argus debug log (redacted)"
  echo "log_file=${LOG_FILE}"
  echo "time_utc=$(date -u +%FT%TZ)"
  echo
  bash "${SCRIPT_DIR}/collect-debug-info.sh"
} >"${LOG_FILE}" 2>&1 || true

echo "Wrote: ${LOG_FILE}"

