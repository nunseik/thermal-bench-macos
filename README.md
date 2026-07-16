# thermal-bench

Sustained CPU + GPU soak test for measuring passive-cooling changes on this
MacBook Air (Apple M3). Drives the whole SoC to thermal steady state and logs
**power (W), temperature (°C), clock (MHz), and throughput (GFLOP/s)** over time,
so you can compare a baseline run against a modded run and see whether the mod
lets the chip sustain more power / performance and/or run cooler.

Everything is **sudoless** — telemetry comes from [`macmon`](https://github.com/vladkens/macmon).

## Why this measures the mod

On Apple Silicon the CPU and GPU share one die, so heat is shared. A passive Air
runs at full clock for a few minutes, then throttles as the die heats up. The mod
(bridging the cooling shield to the bottom case) should improve dissipation, which
shows up as **one or more of**:

- **Higher sustained power** — the SoC can hold a higher wattage before throttling.
- **Higher sustained throughput** — GFLOP/s stays up longer / settles higher.
- **Lower peak temperature** at the same power.
- **Throttling starts later** (clock stays at max for longer).

The report computes *peak* (first ¼ of the run, before heat builds) vs *sustained*
(last ¼, thermal steady state). The gap between them **is** the throttling.

## Usage

```bash
./bench.sh <label> [duration_s] [mode]
```

- `label` — run name, e.g. `baseline` or `modded` (required)
- `duration_s` — soak length, default `1200` (20 min)
- `mode` — `both` (default) · `cpu` · `gpu`

```bash
./bench.sh baseline          # single 20-min combined soak
python3 report.py            # build report.html from all runs in runs/
open report.html
```

### Campaigns (unattended, repeated runs)

For a real comparison you want **≥2 runs per condition** (3 is better). `campaign.sh`
runs N reps under one label back-to-back, cooling the machine to a consistent
thermal start (CPU ≤ 45 °C by default) before each rep:

```bash
./campaign.sh <label> [reps] [duration_s] [mode] [cooldown_C]
./campaign.sh base-ac 3 900          # 3 × 15-min runs, plugged in
```

All reps share the label (timestamps keep the CSVs distinct); `report.py` groups
them automatically. Start it and walk away.

### The 2×2 design (mod-state × power-source)

Power source must be held constant *within* a baseline↔modded comparison. Label
runs `<state>-<source>` and the report pairs them into one verdict per source:

```bash
# today (before the mod):
./campaign.sh base-ac   3 900        # plugged in   — Low Power Mode OFF
./campaign.sh base-batt 3 900        # on battery    — Low Power Mode OFF
# … perform the physical mod, then:
./campaign.sh mod-ac    3 900
./campaign.sh mod-batt  3 900
python3 report.py                    # 2 verdict tables: on charger, on battery
```

`report.py` recognises `base`/`stock`/`before` vs `mod`/`after` in the label and
the trailing `-ac` / `-batt` (or `-charger` / `-battery`) as the condition. You
can still scope it manually: `python3 report.py runs/*-batt-*.csv`.

> **Low Power Mode must be OFF** for every run — it's the one setting that changes
> throttling by power source and would confound the comparison. On Apple Silicon,
> CPU/GPU power limits are otherwise ~identical on battery vs charger, so the
> power-source axis mostly answers the **battery-temperature** question.

Each run writes `runs/<label>-<timestamp>.csv` (+ a `.meta.json`). `report.py`
**groups runs by label** — all `baseline` runs vs all `modded` runs — so run
each side **≥2 times** (3 is better). The report then shows each group as a mean
line with a min–max band (the band width = run-to-run scatter) and a **verdict
table** that flags whether the baseline→modded gap is bigger than that scatter:

- **resolved ✓** — gap ≥ 2× the combined run-to-run scatter and in the good
  direction (more sustained power/clock/throughput, or lower peak temp).
- **within noise** — gap smaller than the scatter; don't trust it.
- **resolved ✗** — a real change in the *bad* direction (e.g. hotter battery).

Power reported is **SoC compute power (CPU+GPU)** — the part the mod affects —
not total system watts. **Battery temperature** (from `ioreg`) is logged and
charted too, so you can catch the mod pushing heat toward the battery.

## Getting a fair comparison

A 20-30% difference is only trustworthy if the runs are otherwise identical:

1. **Same cool starting point.** Let the machine idle until the CPU is back near
   ambient (~35-40 °C) before each run. Back-to-back runs start hot and skew high.
2. **Same physical setup.** Same surface (a hard desk, not a lap or blanket), same
   orientation, lid open the same amount. Surface contact matters a lot for a
   bottom-case dissipation mod.
3. **Same power state.** Keep it plugged in (or on battery) consistently — power
   source changes the power limits.
4. **Quiet machine.** Close other apps; background work adds heat and noise.
5. **Similar ambient room temperature** between baseline and modded runs.
6. **Repeat.** Do 2-3 baselines and 2-3 modded runs; look for a consistent gap,
   not a single reading.

## Files

| File | What it is |
|------|------------|
| `bench.sh` | Entry point — builds `soakload`, runs a soak, logs a CSV. |
| `campaign.sh` | Runs N reps of one label unattended, cooling to a set temp between reps. |
| `src/soakload.swift` | Load generator: all-core FP work + a Metal GPU compute kernel, reports GFLOP/s. |
| `collect.py` | Merges `soakload` throughput + `macmon` telemetry → one CSV, prints a summary. |
| `report.py` | Builds `report.html` (self-contained, interactive) from the CSVs. |
| `runs/` | Per-run CSV + metadata. |

## Requirements

- Apple Silicon Mac, Xcode command-line tools (`swiftc`), Python 3.
- `macmon`: `brew install macmon` (already installed).

## Reading the numbers

- **`GFLOP/s` is a relative performance index, not a peak-FLOPS benchmark** — the
  loops are tuned to keep the units busy and to move with clock, so a drop means
  throttling. Compare it across *your own* runs, not against spec sheets.
- Expect CPU P-cores to sit at ~4056 MHz (max) early, then step down as it heats.
- macmon samples at 1 Hz; small second-to-second jitter in power/clock is normal —
  look at the trend and the sustained average.
