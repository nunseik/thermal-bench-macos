#!/usr/bin/env python3
"""Build a self-contained HTML comparison report from soak-run CSVs.

    python3 report.py                       # all runs in runs/
    python3 report.py runs/baseline-*.csv runs/modded-*.csv   # a subset

Runs are grouped by label (e.g. all "baseline" runs vs all "modded" runs). Each
group is drawn as a mean line with a min/max band across its runs, so you can see
the run-to-run scatter. Sustained values (last quarter = thermal steady state)
are aggregated to mean ± SD per group, and a verdict flags whether the
baseline→modded gap is larger than the run-to-run noise.

Power shown is SoC compute power (CPU+GPU) — what the cooling mod affects — not
system watts. Battery temperature is charted too, to watch for the mod pushing
heat toward the battery.
"""
import csv, glob, os, statistics as st, sys
from datetime import datetime
try:
    import json
except ImportError:
    json = None

HERE = os.path.dirname(os.path.abspath(__file__))

SLOTS = [("#2a78d6", "#3987e5"), ("#eb6834", "#d95926"),
         ("#008300", "#008300"), ("#e87ba4", "#d55181")]

# (key, title, unit, higher_is_better | None for temps where lower is better)
CHARTS = [
    ("soc_w",       "SoC power (CPU+GPU)", "W",       True),
    ("cpu_w",       "CPU power",           "W",       True),
    ("gpu_w",       "GPU power",           "W",       True),
    ("cpu_temp_c",  "CPU temperature",     "°C",      False),
    ("gpu_temp_c",  "GPU temperature",     "°C",      False),
    ("batt_temp_c", "Battery temperature", "°C",      False),
    ("pcpu_mhz",    "CPU P-core clock",    "MHz",     True),
    ("gpu_mhz",     "GPU clock",           "MHz",     True),
    ("cpu_gflops",  "CPU throughput",      "GFLOP/s", True),
    ("gpu_gflops",  "GPU throughput",      "GFLOP/s", True),
]

# metrics summarised as sustained (last-¼ mean) in the verdict table
SUSTAINED = [("soc_w", "SoC power", "W", True),
             ("pcpu_mhz", "CPU P-core clock", "MHz", True),
             ("cpu_gflops", "CPU throughput", "GFLOP/s", True),
             ("gpu_gflops", "GPU throughput", "GFLOP/s", True)]
# metrics summarised as peak (max over run) in the verdict table
PEAK = [("cpu_temp_c", "CPU temp (max)", "°C", False),
        ("batt_temp_c", "Battery temp (max)", "°C", False)]


def load_run(path):
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append({k: float(v) for k, v in r.items()})
    label = os.path.basename(path).rsplit("-", 2)[0]  # strip -YYYYmmdd-HHMMSS
    meta_path = path[:-4] + ".meta.json"
    if json and os.path.exists(meta_path):
        try:
            label = json.load(open(meta_path)).get("label", label)
        except Exception:
            pass
    return {"label": label, "rows": rows, "file": os.path.basename(path)}


def sustained(rows, key):
    n = len(rows); q = max(1, n // 4)
    v = [r[key] for r in rows[n - q:]]
    return sum(v) / len(v)


def peak_first(rows, key):
    n = len(rows); q = max(1, n // 4)
    v = [r[key] for r in rows[:q]]
    return sum(v) / len(v)


def run_max(rows, key):
    return max(r[key] for r in rows)


def grid(rows, key, L):
    """Resample a run's metric onto an integer-second grid 0..L (carry-forward)."""
    buckets = {}
    for r in rows:
        b = int(r["elapsed_s"])
        if b <= L:
            buckets.setdefault(b, []).append(r.get(key, 0.0))
    out, last = [], None
    for t in range(L + 1):
        if t in buckets:
            last = sum(buckets[t]) / len(buckets[t])
        out.append(last)
    # backfill any leading None with first known value
    first = next((x for x in out if x is not None), 0.0)
    return [x if x is not None else first for x in out]


def smooth(a, w):
    if w <= 1 or len(a) < w:
        return a
    out = []
    for i in range(len(a)):
        lo = max(0, i - w // 2); hi = min(len(a), i + w // 2 + 1)
        out.append(sum(a[lo:hi]) / (hi - lo))
    return out


def aggregate(group, key):
    """Group → (mean, lo, hi) arrays over 0..Lg where Lg = shortest run."""
    Lg = min(int(r["rows"][-1]["elapsed_s"]) for r in group)
    per = [grid(r["rows"], key, Lg) for r in group]
    w = max(1, (Lg + 1) // 120)  # light smoothing, ~1% of the run
    mean = smooth([st.mean(col) for col in zip(*per)], w)
    lo = smooth([min(col) for col in zip(*per)], w)
    hi = smooth([max(col) for col in zip(*per)], w)
    return [round(x, 3) for x in mean], [round(x, 3) for x in lo], [round(x, 3) for x in hi]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    paths = args if args else sorted(glob.glob(os.path.join(HERE, "runs", "*.csv")))
    if not paths:
        sys.exit("no run CSVs found in runs/ — do a run first: ./bench.sh baseline")

    MIN_SAMPLES = 30  # skip empty/aborted stubs that never reached steady state
    runs = [load_run(p) for p in paths]
    short = [r for r in runs if len(r["rows"]) < MIN_SAMPLES]
    runs = [r for r in runs if len(r["rows"]) >= MIN_SAMPLES]
    if short:
        print("⚠ skipping " + str(len(short)) + " short/empty run(s) "
              f"(< {MIN_SAMPLES} samples):")
        for r in short:
            print(f"    {r['file']}  ({len(r['rows'])} samples)")
    if not runs:
        sys.exit("no usable runs left after skipping short files.")

    # group by label, preserving order but forcing a 'baseline'-ish label first
    order = []
    for r in runs:
        if r["label"] not in order:
            order.append(r["label"])
    order.sort(key=lambda l: (0 if "base" in l.lower() else 1, l))
    groups = [{"label": l, "runs": [r for r in runs if r["label"] == l]} for l in order]
    for i, g in enumerate(groups):
        g["light"], g["dark"] = SLOTS[i % len(SLOTS)]

    chart_data = []
    for key, title, unit, better in CHARTS:
        series = []
        for g in groups:
            mean, lo, hi = aggregate(g["runs"], key)
            series.append({"label": g["label"], "n": len(g["runs"]),
                          "mean": mean, "lo": lo, "hi": hi})
        chart_data.append({"title": title, "unit": unit, "series": series})

    tiles = build_tiles(groups)
    verdict = build_verdict(groups)
    chip = "Apple Silicon"
    meta0 = paths[0][:-4] + ".meta.json"
    if json and os.path.exists(meta0):
        try:
            chip = json.load(open(meta0)).get("chip", chip)
        except Exception:
            pass

    html = render(groups, tiles, verdict, chart_data, chip)
    out = os.path.join(HERE, "report.html")
    open(out, "w").write(html)
    desc = ", ".join(f"{g['label']}×{len(g['runs'])}" for g in groups)
    print(f"✔ wrote {os.path.relpath(out, HERE)}  ({desc})")
    print(f"  open it:  open {os.path.relpath(out, HERE)}")


def build_tiles(groups):
    tiles = []
    for i, g in enumerate(groups):
        socs = [sustained(r["rows"], "soc_w") for r in g["runs"]]
        thrus = [sustained(r["rows"], "cpu_gflops") + sustained(r["rows"], "gpu_gflops")
                 for r in g["runs"]]
        ctmax = [run_max(r["rows"], "cpu_temp_c") for r in g["runs"]]
        btmax = [run_max(r["rows"], "batt_temp_c") for r in g["runs"]]
        clk = [(sustained(r["rows"], "pcpu_mhz") - peak_first(r["rows"], "pcpu_mhz"))
               / peak_first(r["rows"], "pcpu_mhz") * 100 for r in g["runs"]]
        tiles.append({
            "i": i, "label": g["label"], "n": len(g["runs"]),
            "soc": pm(socs), "thru": pm(thrus, 0), "ctmax": pm(ctmax),
            "btmax": pm(btmax), "throttle": st.mean(clk),
        })
    return tiles


def classify(label):
    """→ (is_baseline, is_modded, condition) e.g. 'base-ac' → (True, False, 'ac')."""
    l = label.lower()
    is_base = any(k in l for k in ("base", "stock", "before"))
    is_mod = (not is_base) and any(k in l for k in ("mod", "after"))
    cond = l
    for tok in ("baseline", "base", "stock", "before", "modded", "mod", "after"):
        cond = cond.replace(tok, "")
    cond = cond.strip("-_ ").replace("--", "-") or "overall"
    return is_base, is_mod, cond


def cond_title(c):
    cl = c.lower()
    if cl in ("ac", "charger", "plugged", "charging"):
        return "on charger (AC)"
    if cl in ("batt", "battery", "bat"):
        return "on battery"
    return c if c != "overall" else None


def build_verdict(groups):
    """Return a list of verdict blocks.

    If groups pair up as baseline↔modded within a shared condition (AC/battery),
    emit one block per condition. Otherwise, if there are exactly two groups,
    compare them directly. Fewer than that → no verdict.
    """
    conds = {}
    for g in groups:
        b, m, c = classify(g["label"])
        if b:
            conds.setdefault(c, {})["base"] = g
        elif m:
            conds.setdefault(c, {})["mod"] = g
    paired = [(c, d) for c, d in conds.items() if "base" in d and "mod" in d]
    if paired:
        return [make_block(d["base"], d["mod"], cond_title(c)) for c, d in sorted(paired)]
    if len(groups) == 2:
        return [make_block(groups[0], groups[1], None)]
    return []


def make_block(a, b, title):
    rows = []
    for key, name, unit, better in SUSTAINED:
        rows.append(verdict_row(a, b, name, unit, better,
                                lambda rr, k=key: sustained(rr, k)))
    for key, name, unit, better in PEAK:
        rows.append(verdict_row(a, b, name, unit, better,
                                lambda rr, k=key: run_max(rr, k)))
    return {"a": a["label"], "b": b["label"], "title": title, "rows": rows,
            "few": min(len(a["runs"]), len(b["runs"])) < 2}


def verdict_row(a, b, name, unit, better, fn):
    va = [fn(r["rows"]) for r in a["runs"]]
    vb = [fn(r["rows"]) for r in b["runs"]]
    ma, mb = st.mean(va), st.mean(vb)
    sa = st.pstdev(va) if len(va) > 1 else 0.0
    sb = st.pstdev(vb) if len(vb) > 1 else 0.0
    gap = mb - ma
    gap_pct = gap / ma * 100 if ma else 0
    scatter = (sa ** 2 + sb ** 2) ** 0.5
    if len(a["runs"]) < 2 or len(b["runs"]) < 2:
        verdict, cls = "need ≥2 runs each", "flat"
    elif abs(gap) >= 2 * scatter and scatter > 0:
        good = (gap > 0) == bool(better)
        verdict = "resolved" + (" ✓" if good else " ✗")
        cls = "up" if good else "down"
    elif abs(gap) >= scatter and scatter > 0:
        verdict, cls = "marginal", "flat"
    else:
        verdict, cls = "within noise", "flat"
    return {"name": name, "unit": unit,
            "a": f"{ma:.1f} ± {sa:.1f}", "b": f"{mb:.1f} ± {sb:.1f}",
            "gap": f"{gap:+.1f} ({gap_pct:+.1f}%)", "verdict": verdict, "cls": cls}


def pm(vals, d=1):
    m = st.mean(vals)
    s = st.pstdev(vals) if len(vals) > 1 else 0.0
    return f"{m:.{d}f}" + (f" ± {s:.{d}f}" if len(vals) > 1 else "")


def render(groups, tiles, verdict, chart_data, chip):
    run_labels = " vs ".join(f"{g['label']} ×{len(g['runs'])}" for g in groups)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    tile_html = ""
    for t in tiles:
        warn = "warn" if any(x for x in [t["ctmax"]] if float(x.split()[0]) >= 97) else ""
        thr = f"{t['throttle']:+.0f}% clk" if t["throttle"] <= -3 else "no throttle"
        tile_html += f"""
        <div class="tile">
          <div class="tile-run"><span class="dot" style="--c:var(--s{t['i']})"></span>{t['label']}
            <span class="nrun">{t['n']} run{'s' if t['n']>1 else ''}</span></div>
          <div class="tile-grid">
            <div><b>{t['soc']}</b><span>W SoC sustained</span></div>
            <div class="{warn}"><b>{t['ctmax']}</b><span>°C CPU peak</span></div>
            <div><b>{t['thru']}</b><span>GFLOP/s sust.</span></div>
            <div><b>{t['btmax']}</b><span>°C battery peak</span></div>
          </div>
        </div>"""

    verdict_html = ""
    if verdict:
        for blk in verdict:
            title = f"Verdict — {blk['b']} vs {blk['a']}"
            if blk["title"]:
                title += f" · {blk['title']}"
            note = ""
            if blk["few"]:
                note = ("<div class='hint'>Only one run in a group — do ≥2 per side so "
                        "run-to-run scatter (±) can be estimated and the verdict is meaningful.</div>")
            vrows = ""
            for r in blk["rows"]:
                vrows += (f"<tr><th>{r['name']}</th><td>{r['a']}</td><td>{r['b']}</td>"
                          f"<td>{r['gap']}</td><td class='{r['cls']}'>{r['verdict']}</td></tr>")
            verdict_html += f"""
        <h2>{title}</h2>
        <table class="verdict">
          <thead><tr><th>Metric (sustained / peak)</th><th>{blk['a']}</th>
            <th>{blk['b']}</th><th>Δ</th><th>vs run-to-run scatter</th></tr></thead>
          <tbody>{vrows}</tbody>
        </table>{note}"""
        verdict_html += """
        <p class="hint">“resolved” = the gap is ≥ 2× the combined run-to-run scatter (±),
        so it's unlikely to be noise; ✓ means it moved the good way for a cooling mod
        (more sustained power/clock/throughput, lower peak temp). “within noise” = the
        gap is smaller than the scatter — not distinguishable from run-to-run variation.</p>"""

    slot_light = "".join(f"--s{i}:{g['light']};" for i, g in enumerate(groups))
    slot_dark = "".join(f"--s{i}:{g['dark']};" for i, g in enumerate(groups))

    data = json.dumps(chart_data) if json else "[]"
    return (TEMPLATE
            .replace("__SLOT_LIGHT__", slot_light)
            .replace("__SLOT_DARK__", slot_dark)
            .replace("__RUN_LABELS__", run_labels)
            .replace("__CHIP__", chip)
            .replace("__NOW__", now)
            .replace("__TILES__", tile_html)
            .replace("__VERDICT__", verdict_html)
            .replace("__DATA__", data))


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Thermal soak — __RUN_LABELS__</title>
<style>
  :root {
    color-scheme: light dark;
    --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e;
    --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
    --up:#006300; --down:#c0392b;
    __SLOT_LIGHT__
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
      --muted:#898781; --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
      --up:#0ca30c; --down:#e66767;
      __SLOT_DARK__
    }
  }
  :root[data-theme="dark"] {
    --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
    --muted:#898781; --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
    --up:#0ca30c; --down:#e66767;
    __SLOT_DARK__
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--plane); color:var(--ink);
    font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
  .wrap { max-width:1080px; margin:0 auto; padding:32px 20px 80px; }
  h1 { font-size:22px; margin:0 0 2px; letter-spacing:-.01em; }
  h2 { font-size:17px; margin:34px 0 10px; }
  .sub { color:var(--ink2); margin:0 0 24px; font-size:13.5px; }
  .tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr));
    gap:12px; margin-bottom:20px; }
  .tile { background:var(--surface); border:1px solid var(--border); border-radius:12px;
    padding:14px 16px; }
  .tile-run { font-weight:600; display:flex; align-items:center; gap:7px; margin-bottom:10px; }
  .nrun { color:var(--muted); font-weight:400; font-size:12px; margin-left:auto; }
  .dot { width:11px; height:11px; border-radius:3px; background:var(--c); }
  .tile-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px 8px; }
  .tile-grid div b { display:block; font-size:18px; letter-spacing:-.01em; }
  .tile-grid div span { font-size:11.5px; color:var(--muted); }
  .tile-grid .warn b { color:var(--down); }
  table { width:100%; border-collapse:collapse; font-size:13px;
    font-variant-numeric:tabular-nums; }
  th,td { text-align:right; padding:7px 10px; border-bottom:1px solid var(--border); }
  th:first-child, td:first-child { text-align:left; }
  thead th { color:var(--ink2); font-weight:600; }
  .verdict td.up { color:var(--up); font-weight:600; }
  .verdict td.down { color:var(--down); font-weight:600; }
  .verdict td.flat { color:var(--muted); }
  .hint { color:var(--muted); font-size:12px; margin-top:10px; max-width:760px; }
  .charts { display:grid; grid-template-columns:1fr 1fr; gap:18px; margin-top:8px; }
  @media (max-width:720px) { .charts { grid-template-columns:1fr; } }
  figure { margin:0; background:var(--surface); border:1px solid var(--border);
    border-radius:12px; padding:12px 14px 8px; }
  figcaption { font-weight:600; font-size:13.5px; margin-bottom:2px; }
  .legend { display:flex; gap:14px; flex-wrap:wrap; font-size:12px; color:var(--ink2);
    margin:2px 0 4px; }
  .legend span { display:inline-flex; align-items:center; gap:6px; }
  .legend i { width:14px; height:3px; border-radius:2px; }
  svg { display:block; width:100%; height:auto; overflow:visible; }
  .grid line { stroke:var(--grid); stroke-width:1; }
  .axis { stroke:var(--axis); stroke-width:1; }
  .tick { fill:var(--muted); font-size:10px; font-variant-numeric:tabular-nums; }
  .cross { stroke:var(--axis); stroke-width:1; stroke-dasharray:3 3; opacity:0; }
  .tip { position:fixed; pointer-events:none; background:var(--surface);
    border:1px solid var(--border); border-radius:8px; padding:7px 9px; font-size:12px;
    box-shadow:0 4px 14px rgba(0,0,0,.18); opacity:0; transition:opacity .08s; z-index:9;
    font-variant-numeric:tabular-nums; }
  .note { color:var(--muted); font-size:12px; margin-top:24px; max-width:820px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Thermal soak comparison</h1>
  <p class="sub">__CHIP__ · __RUN_LABELS__ · generated __NOW__</p>
  <div class="tiles">__TILES__</div>
  __VERDICT__
  <h2>Time series</h2>
  <p class="hint">Line = mean across a group's runs; shaded band = min–max across
  runs (its width is the run-to-run scatter). Lightly smoothed. Hover for values.</p>
  <div class="charts" id="charts"></div>
  <p class="note">“Sustained” = mean of the last quarter (thermal steady state);
  “peak” for temps = max over the run. Power is SoC compute power (CPU+GPU), the
  part the cooling mod affects — not total system watts. Battery temperature is the
  pack sensor from ioreg; watch it for the mod pushing heat toward the battery.</p>
</div>

<div class="tip" id="tip"></div>
<script>
const DATA = __DATA__;
const tip = document.getElementById('tip');
function fmt(v,u){ const d=(u==='GFLOP/s'||u==='MHz')?0:1; return v.toFixed(d)+' '+u; }

function chart(cfg){
  const W=440,H=210,ml=46,mr=14,mt=10,mb=26, iw=W-ml-mr, ih=H-mt-mb;
  let ymin=Infinity,ymax=-Infinity,xmax=1;
  cfg.series.forEach(s=>{ xmax=Math.max(xmax,s.mean.length-1);
    s.lo.forEach(v=>ymin=Math.min(ymin,v)); s.hi.forEach(v=>ymax=Math.max(ymax,v)); });
  if(ymin===ymax){ymax+=1;ymin-=1;}
  const pad=(ymax-ymin)*0.08; ymin-=pad; ymax+=pad;
  if(['W','GFLOP/s','MHz','°C'].includes(cfg.unit)&&ymin>0&&ymin<ymax*0.4) ymin=0;
  const X=t=>ml+iw*t/xmax, Y=v=>mt+ih*(1-(v-ymin)/(ymax-ymin));
  const NS='http://www.w3.org/2000/svg';
  const svg=document.createElementNS(NS,'svg'); svg.setAttribute('viewBox',`0 0 ${W} ${H}`);

  const g=document.createElementNS(NS,'g'); g.setAttribute('class','grid'); svg.appendChild(g);
  for(let i=0;i<=4;i++){ const v=ymin+(ymax-ymin)*i/4, y=Y(v);
    const l=document.createElementNS(NS,'line');
    l.setAttribute('x1',ml);l.setAttribute('x2',W-mr);l.setAttribute('y1',y);l.setAttribute('y2',y);
    g.appendChild(l);
    const t=document.createElementNS(NS,'text'); t.setAttribute('class','tick');
    t.setAttribute('x',ml-6);t.setAttribute('y',y+3);t.setAttribute('text-anchor','end');
    t.textContent=(ymax-ymin>=10)?Math.round(v):v.toFixed(1); svg.appendChild(t); }
  const xt=Math.max(1,Math.ceil(xmax/60));
  for(let m=0;m<=xt;m++){ const t=Math.min(m*60,xmax),x=X(t);
    const tx=document.createElementNS(NS,'text'); tx.setAttribute('class','tick');
    tx.setAttribute('x',x);tx.setAttribute('y',H-8);tx.setAttribute('text-anchor','middle');
    tx.textContent=m+'m'; svg.appendChild(tx); }
  const ax=document.createElementNS(NS,'line'); ax.setAttribute('class','axis');
  ax.setAttribute('x1',ml);ax.setAttribute('x2',ml);ax.setAttribute('y1',mt);ax.setAttribute('y2',mt+ih);
  svg.appendChild(ax);

  cfg.series.forEach((s,si)=>{
    if(s.n>1){ let d='M'; s.hi.forEach((v,t)=>d+=`${X(t)},${Y(v)} L`);
      for(let t=s.lo.length-1;t>=0;t--) d+=`${X(t)},${Y(s.lo[t])} L`;
      d=d.slice(0,-2)+'Z'; const band=document.createElementNS(NS,'path');
      band.setAttribute('d',d); band.setAttribute('fill',`var(--s${si})`);
      band.setAttribute('opacity','0.13'); svg.appendChild(band); }
    const pl=document.createElementNS(NS,'polyline');
    pl.setAttribute('points',s.mean.map((v,t)=>X(t)+','+Y(v)).join(' '));
    pl.setAttribute('fill','none'); pl.setAttribute('stroke',`var(--s${si})`);
    pl.setAttribute('stroke-width','2'); pl.setAttribute('stroke-linejoin','round');
    svg.appendChild(pl);
  });

  const cx=document.createElementNS(NS,'line'); cx.setAttribute('class','cross');
  cx.setAttribute('y1',mt);cx.setAttribute('y2',mt+ih); svg.appendChild(cx);
  const dots=cfg.series.map((s,si)=>{ const c=document.createElementNS(NS,'circle');
    c.setAttribute('r','4');c.setAttribute('fill',`var(--s${si})`);
    c.setAttribute('stroke','var(--surface)');c.setAttribute('stroke-width','2');
    c.style.opacity='0'; svg.appendChild(c); return c; });
  const hit=document.createElementNS(NS,'rect');
  hit.setAttribute('x',ml);hit.setAttribute('y',mt);hit.setAttribute('width',iw);hit.setAttribute('height',ih);
  hit.setAttribute('fill','transparent'); svg.appendChild(hit);
  hit.addEventListener('mousemove',ev=>{
    const r=svg.getBoundingClientRect(); const px=(ev.clientX-r.left)/r.width*W;
    const t=Math.round(Math.max(0,Math.min(xmax,(px-ml)/iw*xmax)));
    cx.setAttribute('x1',X(t));cx.setAttribute('x2',X(t));cx.style.opacity='1';
    let rows='';
    cfg.series.forEach((s,si)=>{ const v=s.mean[Math.min(t,s.mean.length-1)];
      dots[si].setAttribute('cx',X(t));dots[si].setAttribute('cy',Y(v));dots[si].style.opacity='1';
      rows+=`<div><span style="display:inline-block;width:9px;height:9px;border-radius:2px;background:var(--s${si});margin-right:5px"></span>${s.label}: <b>${fmt(v,cfg.unit)}</b></div>`; });
    tip.innerHTML=`<div style="color:var(--muted);margin-bottom:3px">${t}s (${(t/60).toFixed(1)}m)</div>${rows}`;
    tip.style.opacity='1'; tip.style.left=Math.min(ev.clientX+14,window.innerWidth-170)+'px';
    tip.style.top=(ev.clientY+14)+'px';
  });
  hit.addEventListener('mouseleave',()=>{cx.style.opacity='0';tip.style.opacity='0';dots.forEach(d=>d.style.opacity='0');});

  const fig=document.createElement('figure');
  const cap=document.createElement('figcaption'); cap.textContent=`${cfg.title} (${cfg.unit})`; fig.appendChild(cap);
  const leg=document.createElement('div'); leg.className='legend';
  cfg.series.forEach((s,si)=>{ const sp=document.createElement('span');
    sp.innerHTML=`<i style="background:var(--s${si})"></i>${s.label} <span style="color:var(--muted)">×${s.n}</span>`; leg.appendChild(sp); });
  fig.appendChild(leg); fig.appendChild(svg); return fig;
}
const host=document.getElementById('charts');
DATA.forEach(c=>host.appendChild(chart(c)));
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
