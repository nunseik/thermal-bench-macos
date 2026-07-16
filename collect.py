#!/usr/bin/env python3
"""Run a thermal soak and log a unified time series to CSV.

Launches the compiled `soakload` load generator and `macmon` (sudoless power/
temp telemetry), merges both streams at 1 Hz, and writes runs/<label>-<ts>.csv.

No sudo required. Usage:
    python3 collect.py --label baseline --duration 1200 --mode both
"""
import argparse, csv, json, os, re, subprocess, sys, threading, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

COLUMNS = [
    "elapsed_s", "epoch", "cpu_w", "gpu_w", "soc_w", "ane_w", "sys_w",
    "cpu_temp_c", "gpu_temp_c", "batt_temp_c", "batt_pct", "charging",
    "pcpu_mhz", "ecpu_mhz", "gpu_mhz", "cpu_pct", "cpu_gflops", "gpu_gflops",
]

_BATT_TEMP = re.compile(r'"Temperature"\s*=\s*(\d+)')
_BATT_PCT = re.compile(r'"CurrentCapacity"\s*=\s*(\d+)')
_BATT_CHG = re.compile(r'"IsCharging"\s*=\s*(Yes|No)')


def read_battery():
    """Battery pack temp (°C), charge %, and charging flag via ioreg (sudoless).

    Watching battery temperature matters here: the cooling-shield mod could push
    more SoC heat toward the battery, so we log it to catch any long-term risk.
    """
    try:
        out = subprocess.run(["ioreg", "-r", "-n", "AppleSmartBattery", "-w0"],
                             capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return {"batt_temp_c": 0.0, "batt_pct": 0, "charging": 0}
    t = _BATT_TEMP.search(out)      # top-level "Temperature" is in 1/100 °C
    p = _BATT_PCT.search(out)
    c = _BATT_CHG.search(out)
    return {
        "batt_temp_c": round(int(t.group(1)) / 100, 2) if t else 0.0,
        "batt_pct": int(p.group(1)) if p else 0,
        "charging": 1 if (c and c.group(1) == "Yes") else 0,
    }


def reader_soakload(proc, state, lock):
    """Parse `SAMPLE key=val ...` lines into state['cpu_gflops'/'gpu_gflops']."""
    for raw in proc.stdout:
        line = raw.decode(errors="replace").strip()
        if not line.startswith("SAMPLE"):
            continue
        vals = {}
        for tok in line.split()[1:]:
            if "=" in tok:
                k, v = tok.split("=", 1)
                try:
                    vals[k] = float(v)
                except ValueError:
                    pass
        with lock:
            if "cpu_gflops" in vals:
                state["cpu_gflops"] = vals["cpu_gflops"]
            if "gpu_gflops" in vals:
                state["gpu_gflops"] = vals["gpu_gflops"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True, help="run label, e.g. baseline / modded")
    ap.add_argument("--duration", type=int, default=1200, help="seconds (default 1200)")
    ap.add_argument("--mode", default="both", choices=["both", "cpu", "gpu"])
    ap.add_argument("--outdir", default=os.path.join(HERE, "runs"))
    args = ap.parse_args()

    soak = os.path.join(HERE, "soakload")
    if not os.path.exists(soak):
        sys.exit("soakload binary not found — run bench.sh (it builds it) or "
                 "swiftc -O -o soakload src/soakload.swift -framework Metal")

    os.makedirs(args.outdir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in args.label)
    csv_path = os.path.join(args.outdir, f"{safe}-{ts}.csv")
    meta_path = csv_path[:-4] + ".meta.json"

    meta = {
        "label": args.label, "mode": args.mode, "duration_s": args.duration,
        "started_local": datetime.now().isoformat(timespec="seconds"),
        "chip": subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                               capture_output=True, text=True).stdout.strip(),
        "macos": subprocess.run(["sw_vers", "-productVersion"],
                                capture_output=True, text=True).stdout.strip(),
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"▶ {args.label}: {args.mode} load for {args.duration}s  →  {os.path.relpath(csv_path, HERE)}")
    print("  (leave the machine idle & on a consistent surface; Ctrl-C to abort)\n")

    state = {"cpu_gflops": 0.0, "gpu_gflops": 0.0}
    lock = threading.Lock()

    soak_p = subprocess.Popen(
        [soak, "--mode", args.mode, "--duration", str(args.duration + 2)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    mac_p = subprocess.Popen(
        ["macmon", "pipe", "-i", "1000", "-s", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    threading.Thread(target=reader_soakload, args=(soak_p, state, lock), daemon=True).start()

    rows = []
    start = time.time()
    try:
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            # Pace off macmon's 1 Hz JSONL stream.
            for raw in mac_p.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                now = time.time()
                elapsed = now - start
                with lock:
                    cg, gg = state["cpu_gflops"], state["gpu_gflops"]
                cpu_w = round(d.get("cpu_power", 0.0), 3)
                gpu_w = round(d.get("gpu_power", 0.0), 3)
                batt = read_battery()
                row = {
                    "elapsed_s": round(elapsed, 1),
                    "epoch": round(now, 3),
                    "cpu_w": cpu_w,
                    "gpu_w": gpu_w,
                    "soc_w": round(cpu_w + gpu_w, 3),
                    "ane_w": round(d.get("ane_power", 0.0), 3),
                    "sys_w": round(d.get("sys_power", 0.0), 3),
                    "cpu_temp_c": round(d.get("temp", {}).get("cpu_temp_avg", 0.0), 2),
                    "gpu_temp_c": round(d.get("temp", {}).get("gpu_temp_avg", 0.0), 2),
                    "batt_temp_c": batt["batt_temp_c"],
                    "batt_pct": batt["batt_pct"],
                    "charging": batt["charging"],
                    "pcpu_mhz": round(d.get("pcpu_usage", [0])[0]),
                    "ecpu_mhz": round(d.get("ecpu_usage", [0])[0]),
                    "gpu_mhz": round(d.get("gpu_usage", [0])[0]),
                    "cpu_pct": round(d.get("cpu_usage_pct", 0.0) * 100, 1),
                    "cpu_gflops": round(cg, 2),
                    "gpu_gflops": round(gg, 2),
                }
                w.writerow(row); f.flush()
                rows.append(row)
                bar = f"\r  t={elapsed:5.0f}s  SoC {row['soc_w']:4.1f}W  cpu {row['cpu_temp_c']:5.1f}°C " \
                      f"{row['pcpu_mhz']:4d}MHz | gpu {row['gpu_temp_c']:5.1f}°C | batt {row['batt_temp_c']:4.1f}°C " \
                      f"| {row['cpu_gflops']:.0f}+{row['gpu_gflops']:.0f} GFLOP/s "
                sys.stdout.write(bar); sys.stdout.flush()
                if elapsed >= args.duration:
                    break
    except KeyboardInterrupt:
        print("\n  aborted by user (partial CSV kept)")
    finally:
        for p in (soak_p, mac_p):
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()

    print("\n")
    summarize(rows, args.label)
    print(f"\n✔ saved {os.path.relpath(csv_path, HERE)}")
    return csv_path


def _avg(rows, key, lo, hi):
    vals = [r[key] for r in rows[lo:hi]]
    return sum(vals) / len(vals) if vals else 0.0


def summarize(rows, label):
    if len(rows) < 5:
        print("  (too few samples for a summary)")
        return
    n = len(rows)
    q = max(1, n // 4)
    peak_lo, peak_hi = 0, q                 # first quarter ≈ pre-throttle peak
    sust_lo, sust_hi = n - q, n             # last quarter ≈ thermal steady state
    print(f"── summary: {label} ({n}s) " + "─" * 24)
    for k, unit in [("soc_w", "W"), ("cpu_w", "W"), ("gpu_w", "W"),
                    ("cpu_temp_c", "°C"), ("gpu_temp_c", "°C"), ("batt_temp_c", "°C"),
                    ("pcpu_mhz", "MHz"), ("gpu_mhz", "MHz"),
                    ("cpu_gflops", "GFLOP/s"), ("gpu_gflops", "GFLOP/s")]:
        peak = _avg(rows, k, peak_lo, peak_hi)
        sust = _avg(rows, k, sust_lo, sust_hi)
        drop = (sust - peak) / peak * 100 if peak else 0
        tag = ""
        if k in ("pcpu_mhz", "gpu_mhz", "cpu_gflops", "gpu_gflops") and drop < -3:
            tag = f"  ⟵ throttled {drop:+.0f}%"
        print(f"  {k:12s} peak(1st¼)={peak:8.1f}  sustained(last¼)={sust:8.1f} {unit:<8}{tag}")
    print(f"  max cpu_temp={max(r['cpu_temp_c'] for r in rows):.1f}°C  "
          f"max gpu_temp={max(r['gpu_temp_c'] for r in rows):.1f}°C  "
          f"max batt_temp={max(r['batt_temp_c'] for r in rows):.1f}°C")


if __name__ == "__main__":
    main()
