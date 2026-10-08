#!/usr/bin/env python3
"""Plot E2E latency, TTFT and TPOT vs request rate for an inference-perf run.

Usage: plot_latency.py <results_dir>
Reads <results_dir>/inference-perf_*_rate_<rate>_*/summary_lifecycle_metrics.json
and writes e2e_vs_rate.png, ttft_vs_rate.png, tpot_vs_rate.png into <results_dir>.
"""
import glob
import json
import os
import re
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

METRICS = [
    ("e2e", "request_latency", "E2E request latency", "s", 1.0),
    ("ttft", "time_to_first_token", "Time to first token (TTFT)", "s", 1.0),
    ("tpot", "time_per_output_token", "Time per output token (TPOT)", "ms", 1000.0),
]
STATS = [("mean", "mean"), ("p50", "median"), ("p90", "p90"), ("p99", "p99")]
COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
INK, MUTED, GRID, SURFACE = "#1f1f1e", "#6b6b68", "#e6e6e3", "#fcfcfb"


def load(results_dir):
    runs = []
    for d in glob.glob(os.path.join(results_dir, "inference-perf_*_rate_*")):
        m = re.search(r"_rate_([0-9p.]+)_", os.path.basename(d))
        path = os.path.join(d, "summary_lifecycle_metrics.json")
        if not m or not os.path.exists(path):
            continue
        with open(path) as f:
            runs.append((float(m.group(1).replace("p", ".")), json.load(f)))
    return sorted(runs, key=lambda r: r[0])


def plot(runs, key, field, title, unit, scale, out):
    rates = [r for r, _ in runs]
    fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for (label, stat), color in zip(STATS, COLORS):
        ys = [d["successes"]["latency"][field][stat] * scale for _, d in runs]
        ax.plot(rates, ys, color=color, linewidth=2, marker="o", markersize=6,
                markeredgecolor=SURFACE, markeredgewidth=1.5, label=label)
    ax.set_xscale("log", base=2)
    ax.set_xticks(rates)
    ax.set_xticklabels([f"{r:g}" for r in rates])
    ax.set_ylim(bottom=0)
    ax.set_xlabel("Request rate (req/s)", color=MUTED)
    ax.set_ylabel(f"Latency ({unit})", color=MUTED)
    ax.set_title(f"{title} vs request rate", loc="left", color=INK, fontsize=12)
    ax.grid(True, axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.legend(frameon=False, loc="upper left", fontsize=9, labelcolor=INK)
    fig.tight_layout()
    fig.savefig(out, facecolor=SURFACE)
    plt.close(fig)


def main():
    results_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    runs = load(results_dir)
    if not runs:
        sys.exit(f"no rate_* runs found in {results_dir}")
    for key, field, title, unit, scale in METRICS:
        out = os.path.join(results_dir, f"{key}_vs_rate.png")
        plot(runs, key, field, title, unit, scale, out)
        print(out)


if __name__ == "__main__":
    main()
