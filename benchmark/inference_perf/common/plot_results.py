#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["matplotlib", "pyyaml"]
# ///
"""Plot E2E latency, TTFT and TPOT against request rate for the epd and e-pd scenarios.

Reads <scenario>/results/<experiment id>/inference-perf_<id>_<workload>_<stack>/ folders
(written by run.sh) and uses the newest experiment id per scenario unless --epd / --e-pd
names one. The warmup workload (type: concurrent) is skipped.

Writes to <inference_perf>/plots/:
  e2e_vs_rate.png, ttft_vs_rate.png, tpot_vs_rate.png, summary.csv

Usage (from benchmark/inference_perf):
  uv run common/plot_results.py
  uv run common/plot_results.py --epd 1791290000 --e-pd 1791295000
"""

import argparse
import csv
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import yaml  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = {  # name -> line color (default palette, categorical slots 1 and 2)
    "epd": "#2a78d6",
    "e-pd": "#eb6834",
}
METRICS = [  # (file stem, summary key, title, unit, scale)
    ("e2e", "request_latency", "E2E request latency", "s", 1.0),
    ("ttft", "time_to_first_token", "Time to first token (TTFT)", "s", 1.0),
    ("tpot", "time_per_output_token", "Time per output token (TPOT)", "ms", 1000.0),
]
STATS = [("mean", "-", "mean"), ("p90", "--", "p90")]  # (summary key, line style, label)
TEXT_PRIMARY, TEXT_SECONDARY, GRID = "#0b0b0b", "#52514e", "#e4e3df"


def latest_experiment(results: Path) -> str | None:
    ids = [p.name for p in results.iterdir() if p.is_dir() and p.name.isdigit()] if results.is_dir() else []
    return max(ids, key=int) if ids else None


def load_scenario(name: str, experiment: str | None) -> tuple[str | None, list[dict]]:
    results = ROOT / name / "results"
    experiment = experiment or latest_experiment(results)
    if experiment is None:
        return None, []
    rows = []
    # run_only.sh copies the whole PVC, so keep only this experiment's folders.
    for run_dir in sorted((results / experiment).glob(f"inference-perf_{experiment}_*")):
        summary_file = run_dir / "summary_lifecycle_metrics.json"
        workload_files = [f for f in run_dir.glob("*.yaml") if f.name not in ("config.yaml", "run_metadata.yaml")
                          and not f.name.startswith("benchmark_report")]
        if not summary_file.exists() or not workload_files:
            print(f"  skipping {run_dir.name}: no summary or workload file", file=sys.stderr)
            continue
        load = yaml.safe_load(workload_files[0].read_text())["load"]
        if load.get("type") == "concurrent":  # warmup
            continue
        summary = json.loads(summary_file.read_text())
        ok = summary.get("successes") or {}
        row = {
            "scenario": name,
            "experiment": experiment,
            "workload": workload_files[0].stem,
            "rate": float(load["stages"][0]["rate"]),
            "achieved_rps": (ok.get("throughput") or {}).get("requests_per_sec"),
            "successes": ok.get("count", 0),
            "failures": (summary.get("failures") or {}).get("count", 0),
        }
        for stem, key, _, _, scale in METRICS:
            dist = (ok.get("latency") or {}).get(key) or {}
            for stat, _, _ in STATS:
                value = dist.get(stat)
                row[f"{stem}_{stat}"] = None if value is None else value * scale
        rows.append(row)
    return experiment, sorted(rows, key=lambda r: r["rate"])


def plot_metric(stem: str, title: str, unit: str, data: dict[str, list[dict]], out: Path) -> None:
    fig, ax = plt.subplots(figsize=(7.5, 4.6), dpi=150)
    rates = sorted({r["rate"] for rows in data.values() for r in rows})
    for name, rows in data.items():
        color = SCENARIOS[name]
        for stat, style, label in STATS:
            pts = [(r["rate"], r[f"{stem}_{stat}"]) for r in rows if r[f"{stem}_{stat}"] is not None]
            if not pts:
                continue
            xs, ys = zip(*pts)
            ax.plot(xs, ys, style, color=color, linewidth=2, marker="o", markersize=6,
                    markeredgecolor="white", markeredgewidth=1.5, label=f"{name} {label}")
        # direct label on the mean line's last point
        last = [r for r in rows if r[f"{stem}_mean"] is not None]
        if last:
            ax.annotate(name, (last[-1]["rate"], last[-1][f"{stem}_mean"]), xytext=(8, 0),
                        textcoords="offset points", va="center", color=TEXT_PRIMARY, fontsize=9)
    ax.set_xscale("log", base=2)
    ax.set_xticks(rates, [f"{r:g}" for r in rates])
    ax.minorticks_off()
    ax.set_xlabel("Request rate (req/s)", color=TEXT_SECONDARY)
    ax.set_ylabel(f"{title.split(' (')[0]} ({unit})", color=TEXT_SECONDARY)
    ax.set_ylim(bottom=0)
    ax.set_title(f"{title} vs request rate - 4×1080p images, 128 in / 128 out tokens",
                 color=TEXT_PRIMARY, fontsize=10, loc="left")
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=TEXT_SECONDARY, labelsize=9)
    ax.legend(frameon=False, fontsize=9, labelcolor=TEXT_PRIMARY)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--epd", help="experiment id under epd/results (default: newest)")
    ap.add_argument("--e-pd", dest="e_pd", help="experiment id under e-pd/results (default: newest)")
    ap.add_argument("--out", type=Path, default=ROOT / "plots", help="output directory (default: %(default)s)")
    args = ap.parse_args()

    data = {}
    for name, experiment in (("epd", args.epd), ("e-pd", args.e_pd)):
        experiment, rows = load_scenario(name, experiment)
        if rows:
            data[name] = rows
            print(f"{name}: experiment {experiment}, rates {[r['rate'] for r in rows]}")
        else:
            print(f"{name}: no results found under {ROOT / name / 'results'}", file=sys.stderr)
    if not data:
        sys.exit("nothing to plot")

    args.out.mkdir(parents=True, exist_ok=True)
    for stem, _, title, unit, _ in METRICS:
        plot_metric(stem, title, unit, data, args.out / f"{stem}_vs_rate.png")

    rows = [r for name in data for r in data[name]]
    with open(args.out / "summary.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n{'scenario':8} {'rate':>5} {'achieved':>8} {'ok':>5} {'fail':>4} "
          f"{'e2e mean/p90 (s)':>17} {'ttft mean/p90 (s)':>18} {'tpot mean/p90 (ms)':>19}")
    fmt = lambda v, d: "-" if v is None else f"{v:.{d}f}"  # noqa: E731
    for r in rows:
        print(f"{r['scenario']:8} {r['rate']:>5g} {fmt(r['achieved_rps'], 2):>8} {r['successes']:>5} {r['failures']:>4} "
              f"{fmt(r['e2e_mean'], 2):>8}/{fmt(r['e2e_p90'], 2):<8} {fmt(r['ttft_mean'], 2):>9}/{fmt(r['ttft_p90'], 2):<8} "
              f"{fmt(r['tpot_mean'], 1):>10}/{fmt(r['tpot_p90'], 1):<8}")
        if r["failures"]:
            print(f"         ^ {r['failures']} failed requests - see that run's stdout.log")
    print(f"\nwrote {args.out}/{{e2e,ttft,tpot}}_vs_rate.png and summary.csv")


if __name__ == "__main__":
    main()
