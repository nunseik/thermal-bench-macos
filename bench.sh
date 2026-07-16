#!/usr/bin/env bash
# thermal-bench — sustained CPU+GPU soak with power/temp/throughput logging.
#
#   ./bench.sh <label> [duration_s] [mode]
#     label       run name, e.g. baseline  or  modded   (required)
#     duration_s  soak length in seconds                (default 1200 = 20 min)
#     mode        both | cpu | gpu                       (default both)
#
# Examples:
#   ./bench.sh baseline               # 20-min combined soak, before the mod
#   ./bench.sh modded                 # 20-min combined soak, after the mod
#   ./bench.sh quick-check 120 cpu    # 2-min CPU-only sanity run
set -euo pipefail
cd "$(dirname "$0")"

# Keep the machine awake for the whole run (idle/display/disk/system sleep),
# on battery too. Re-exec self under caffeinate once.
if [ -z "${CAFFEINATED:-}" ] && command -v caffeinate >/dev/null; then
  exec env CAFFEINATED=1 caffeinate -dimsu "$0" "$@"
fi

LABEL="${1:?usage: ./bench.sh <label> [duration_s] [mode]}"
DURATION="${2:-1200}"
MODE="${3:-both}"

# Build the load generator if missing or out of date.
if [[ ! -x ./soakload || src/soakload.swift -nt ./soakload ]]; then
  echo "building soakload…"
  swiftc -O -o soakload src/soakload.swift -framework Metal
fi

command -v macmon >/dev/null || { echo "macmon not found — run: brew install macmon" >&2; exit 1; }

exec python3 collect.py --label "$LABEL" --duration "$DURATION" --mode "$MODE"
