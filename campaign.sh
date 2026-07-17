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
COOL="${5:-42}"       # target smoothed idle CPU °C
MIN_COOLDOWN="${MIN_COOLDOWN:-360}"  # ALWAYS cool at least this long. cpu_temp_avg is
                      # the core-junction temp, which collapses within ~1s of load
                      # ending while the chassis/package stays heat-soaked — so a
                      # bare temperature threshold starts the next run far too early.
                      # This floor covers the chassis the sensor can't see.
COOL_TIMEOUT="${COOL_TIMEOUT:-1200}" # ...but never wait more than this

command -v macmon >/dev/null || { echo "macmon not found — brew install macmon" >&2; exit 1; }

cpu_temp() {  # 5-sample (≈5s) mean of CPU temp — idle reading is noisy (±3°C)
  macmon pipe -s 5 -i 1000 2>/dev/null | python3 -c '
import sys, json
v = [json.loads(l)["temp"]["cpu_temp_avg"] for l in sys.stdin if l.strip()]
print(round(sum(v)/len(v), 1) if v else 999)
' 2>/dev/null || echo 999
}

wait_cool() {
  # Reset = smoothed idle temp ≤ target AND plateaued (2 consecutive polls below
  # target) AND at least MIN_COOLDOWN elapsed. Belt-and-suspenders because no
  # sensor exposes chassis/skin temperature.
  local target="$1" start=$SECONDS t below=0 el
  echo "  cooling (min ${MIN_COOLDOWN}s; then until CPU ≤ ${target}°C and steady)…"
  while :; do
    t=$(cpu_temp); el=$((SECONDS-start))
    printf "\r  cooling: CPU %5.1f°C  (%ds elapsed)          " "$t" "$el"
    if awk "BEGIN{exit !($t <= $target)}"; then below=$((below+1)); else below=0; fi
    if (( el >= MIN_COOLDOWN && below >= 2 )); then
      printf "\n  ✓ reset at %.1f°C after %ds\n" "$t" "$el"; return
    fi
    if (( el > COOL_TIMEOUT )); then
      printf "\n  ⚠ still %.1f°C after %ds — starting anyway (warm start; note it)\n" "$t" "$el"; return
    fi
    sleep 10
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
