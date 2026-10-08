#!/usr/bin/env python3
"""Compare setups: mean and p90 (with shaded mean-p90 band) vs request rate, plus a combined CSV.

Usage: plot_compare.py <out_dir> <label>=<results_dir> [<label>=<results_dir> ...]
Writes e2e_comparison.png, ttft_comparison.png, tpot_comparison.png and
latency_summary.csv (one row per setup and rate) into <out_dir>.
"""
import csv
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from plot_distribution import CSV_STATS
from plot_latency import COLORS, GRID, INK, METRICS, MUTED, SURFACE, load


def plot(setups, field, title, unit, scale, out):
    fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    rates = sorted({r for _, runs in setups for r, _ in runs})
    for (label, runs), color in zip(setups, COLORS):
        xs = [r for r, _ in runs]
        mean = [d["successes"]["latency"][field]["mean"] * scale for _, d in runs]
        p90 = [d["successes"]["latency"][field]["p90"] * scale for _, d in runs]
        ax.fill_between(xs, mean, p90, color=color, alpha=0.15, linewidth=0,
                        label=f"{label} mean–p90 band")
        ax.plot(xs, mean, color=color, linewidth=2, marker="o", markersize=6,
                markeredgecolor=SURFACE, markeredgewidth=1.5, label=f"{label} mean")
        ax.plot(xs, p90, color=color, linewidth=1.5, linestyle="--", marker="^", markersize=6,
                markeredgecolor=SURFACE, markeredgewidth=1.5, label=f"{label} p90")
    ax.set_xscale("log", base=2)
    ax.set_xticks(rates)
    ax.set_xticklabels([f"{r:g}" for r in rates])
    ax.set_ylim(bottom=0)
    ax.set_xlabel("Request rate (req/s)", color=MUTED)
    ax.set_ylabel(f"Latency ({unit})", color=MUTED)
    names = " vs ".join(label for label, _ in setups)
    ax.set_title(f"{title} vs request rate (mean, p90): {names}", loc="left", color=INK, fontsize=12)
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


def write_csv(setups, out):
    header = ["setup", "results_dir", "rate", "requests", "throughput_req_s", "output_tokens_s"]
    for key, _, _, unit, _ in METRICS:
        header += [f"{key}_{s}_{unit}" for s in CSV_STATS]
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for label, results_dir, runs in setups:
            for rate, d in runs:
                tp = d["successes"]["throughput"]
                row = [label, results_dir, rate, d["successes"]["count"],
                       round(tp["requests_per_sec"], 4), round(tp["output_tokens_per_sec"], 4)]
                for _, field, _, _, scale in METRICS:
                    row += [round(d["successes"]["latency"][field][s] * scale, 4) for s in CSV_STATS]
                w.writerow(row)


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    out_dir = sys.argv[1]
    os.makedirs(out_dir, exist_ok=True)
    setups = []
    for arg in sys.argv[2:]:
        label, results_dir = arg.split("=", 1)
        runs = load(results_dir)
        if not runs:
            sys.exit(f"no rate_* runs found in {results_dir}")
        setups.append((label, results_dir, runs))
    for key, field, title, unit, scale in METRICS:
        out = os.path.join(out_dir, f"{key}_comparison.png")
        plot([(label, runs) for label, _, runs in setups], field, title, unit, scale, out)
        print(out)
    out = os.path.join(out_dir, "latency_summary.csv")
    write_csv(setups, out)
    print(out)


if __name__ == "__main__":
    main()
