#!/usr/bin/env bash
# campaign.sh — run N soak reps under one label, unattended, cooling to a
# consistent thermal start before each rep.
#
#   ./campaign.sh <label> [reps] [duration_s] [mode] [cooldown_C]
#     label       shared run label, e.g. base-ac  base-batt  mod-ac  mod-batt
#     reps        how many runs (default 3)
#     duration_s  soak length each (default 900 = 15 min)
#     mode        both | cpu | gpu (default both)
#     cooldown_C  wait until CPU ≤ this °C before each rep (default 45)
#
# All reps share <label> (report.py groups by label); timestamps keep the CSVs
# distinct. Example day:
#     ./campaign.sh base-ac   3 900     # plugged in
#     ./campaign.sh base-batt 3 900     # on battery
set -euo pipefail
cd "$(dirname "$0")"

# Keep awake for the ENTIRE campaign — reps and the cooldown waits between them.
# (bench.sh sees CAFFEINATED=1 and won't wrap again.)
if [ -z "${CAFFEINATED:-}" ] && command -v caffeinate >/dev/null; then
  exec env CAFFEINATED=1 caffeinate -dimsu "$0" "$@"
fi

LABEL="${1:?usage: ./campaign.sh <label> [reps] [duration_s] [mode] [cooldown_C]}"
REPS="${2:-3}"
DURATION="${3:-900}"
MODE="${4:-both}"
COOL="${5:-45}"
COOL_TIMEOUT=720   # give up waiting to cool after 12 min

command -v macmon >/dev/null || { echo "macmon not found — brew install macmon" >&2; exit 1; }

cpu_temp() {  # one macmon sample → CPU avg °C
  macmon pipe -s 1 -i 400 2>/dev/null \
    | python3 -c 'import sys,json; print(json.loads(sys.stdin.readline())["temp"]["cpu_temp_avg"])' 2>/dev/null \
    || echo 999
}

wait_cool() {
  local target="$1" start=$SECONDS t
  while :; do
    t=$(cpu_temp)
    printf "\r  cooling: CPU %5.1f°C  (target ≤ %s°C, %ds elapsed)   " "$t" "$target" "$((SECONDS-start))"
    if awk "BEGIN{exit !($t <= $target)}"; then printf "\n"; return; fi
    if (( SECONDS - start > COOL_TIMEOUT )); then
      printf "\n  ⚠ still %.1f°C after %ds — starting anyway (note the warm start)\n" "$t" "$COOL_TIMEOUT"
      return
    fi
    sleep 5
  done
}

echo "════════════════════════════════════════════════════════"
echo " campaign: $LABEL  ·  $REPS reps × ${DURATION}s  ·  mode=$MODE"
echo " cooldown target ≤ ${COOL}°C between reps"
echo "════════════════════════════════════════════════════════"
for i in $(seq 1 "$REPS"); do
  echo ""
  echo "── rep $i/$REPS ────────────────────────────────────────"
  wait_cool "$COOL"
  ./bench.sh "$LABEL" "$DURATION" "$MODE"
done

echo ""
echo "✔ campaign '$LABEL' done ($REPS reps). Build the report with:  python3 report.py"
