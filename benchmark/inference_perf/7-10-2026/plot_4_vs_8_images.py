#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["matplotlib"]
# ///
"""epd vs e-pd, 4 vs 8 images per request: E2E, TTFT and TPOT against request rate (0.5-4 req/s).

Reads summary_lifecycle_metrics.json from each run folder (the CSVs have no p75) and writes
three sets of graphs to 7-10-2026/epd_vs_e-pd/:
  values/     mean only
  mean_p90/   mean + shaded mean-p90 band
  mean_p75/   mean + shaded mean-p75 band
each with e2e_comparison.png, ttft_comparison.png, tpot_comparison.png.

Usage (from benchmark/inference_perf):
  uv run 7-10-2026/plot_4_vs_8_images.py                                     # all sets, rates 0.5-4
  uv run 7-10-2026/plot_4_vs_8_images.py --max-rate 2 --set values --suffix _2   # -> values_2/
"""

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent  # benchmark/inference_perf
OUT = Path(__file__).resolve().parent / "epd_vs_e-pd"
MAX_RATE = 4.0

# (label, experiment folder, color, marker). Hue = setup, shade + marker = image count.
SERIES = [
    ("epd 4 images", "6-10-2026/epd/results/1791289595", "#3987e5", "o"),
    ("e-pd 4 images", "6-10-2026/e-pd/results/1791292590", "#eb6834", "o"),
    ("epd 8 images", "7-10-2026/epd/results/1791365686", "#184f95", "s"),
    ("e-pd 8 images", "7-10-2026/e-pd/results/1791361006", "#9a3412", "s"),
]
METRICS = [  # (file stem, summary key, title, unit, scale)
    ("e2e", "request_latency", "E2E request latency", "s", 1.0),
    ("ttft", "time_to_first_token", "Time to first token (TTFT)", "s", 1.0),
    ("tpot", "time_per_output_token", "Time per output token (TPOT)", "ms", 1000.0),
]
SETS = [("values", None), ("mean_p90", "p90"), ("mean_p75", "p75")]  # (folder, band upper stat)
TEXT_PRIMARY, TEXT_SECONDARY, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"


def load(experiment: str) -> list[tuple[float, dict]]:
    """[(rate, successes.latency), ...] for rate runs up to MAX_RATE, sorted by rate."""
    runs = []
    for run_dir in (ROOT / experiment).glob("inference-perf_*_rate_*"):
        workload = run_dir.name.split("_rate_")[1].rsplit("_", 1)[0]  # e.g. "0p5", "4"
        rate = float(workload.replace("p", "."))
        if rate <= MAX_RATE:
            summary = json.loads((run_dir / "summary_lifecycle_metrics.json").read_text())
            runs.append((rate, summary["successes"]["latency"]))
    if not runs:
        raise SystemExit(f"no rate runs under {ROOT / experiment}")
    return sorted(runs, key=lambda r: r[0])


def plot(stem: str, key: str, title: str, unit: str, scale: float, band: str | None,
         data: dict[str, list[tuple[float, dict]]], out: Path) -> None:
    fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for label, _, color, marker in SERIES:
        runs = data[label]
        xs = [rate for rate, _ in runs]
        mean = [lat[key]["mean"] * scale for _, lat in runs]
        if band:
            upper = [lat[key][band] * scale for _, lat in runs]
            ax.fill_between(xs, mean, upper, color=color, alpha=0.12, linewidth=0,
                            label=f"{label} mean–{band} band")
        ax.plot(xs, mean, "-", color=color, linewidth=2, marker=marker, markersize=7,
                markeredgecolor=SURFACE, markeredgewidth=1.5, label=f"{label} mean")
        if band:
            ax.plot(xs, upper, "--", color=color, linewidth=1.5, marker="^", markersize=6,
                    markeredgecolor=SURFACE, markeredgewidth=1, label=f"{label} {band}")
    rates = sorted({rate for runs in data.values() for rate, _ in runs})
    ax.set_xscale("log", base=2)
    ax.set_xticks(rates, [f"{r:g}" for r in rates])
    ax.minorticks_off()
    ax.set_ylim(bottom=0)
    ax.set_xlabel("Request rate (req/s)", color=TEXT_SECONDARY)
    ax.set_ylabel(f"Latency ({unit})", color=TEXT_SECONDARY)
    stats = f"mean, {band}" if band else "mean"
    ax.set_title(f"{title} vs request rate ({stats}): epd vs e-pd, 4 vs 8 images",
                 color=TEXT_PRIMARY, fontsize=11, loc="left")
    ax.grid(True, axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=TEXT_SECONDARY, labelsize=9)
    ax.legend(frameon=False, fontsize=8 if band else 9, labelcolor=TEXT_PRIMARY, loc="upper left")
    fig.tight_layout()
    fig.savefig(out, facecolor=SURFACE)
    plt.close(fig)


def main() -> None:
    global MAX_RATE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-rate", type=float, default=MAX_RATE, help="highest rate to plot (default: %(default)g)")
    ap.add_argument("--set", choices=[folder for folder, _ in SETS], action="append", dest="sets",
                    help="only this set (repeatable; default: all)")
    ap.add_argument("--suffix", default="", help="appended to each set's folder name, e.g. _2 -> values_2")
    args = ap.parse_args()
    MAX_RATE = args.max_rate

    data = {label: load(experiment) for label, experiment, _, _ in SERIES}
    for folder, band in SETS:
        if args.sets and folder not in args.sets:
            continue
        out_dir = OUT / f"{folder}{args.suffix}"
        out_dir.mkdir(parents=True, exist_ok=True)
        for stem, key, title, unit, scale in METRICS:
            plot(stem, key, title, unit, scale, band, data, out_dir / f"{stem}_comparison.png")
        print(f"wrote {out_dir}/{{e2e,ttft,tpot}}_comparison.png")
    print(f"{'series':14} {'rate':>4}  " + "  ".join(f"{s:>22}" for s, *_ in METRICS))
    for label, *_ in SERIES:
        for rate, lat in data[label]:
            cells = [f"{lat[k]['mean'] * sc:7.2f} /{lat[k]['p75'] * sc:7.2f} /{lat[k]['p90'] * sc:7.2f}"
                     for _, k, _, _, sc in METRICS]
            print(f"{label:14} {rate:>4g}  " + "  ".join(cells))
    print("(each cell: mean / p75 / p90; e2e and ttft in s, tpot in ms)")


if __name__ == "__main__":
    main()
