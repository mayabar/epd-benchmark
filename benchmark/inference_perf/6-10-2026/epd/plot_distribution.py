#!/usr/bin/env python3
"""Plot mean and p90 (with a shaded mean-p90 band) vs request rate, and export a CSV.

Usage: plot_distribution.py <results_dir>
Writes e2e_distribution.png, ttft_distribution.png, tpot_distribution.png and
latency_summary.csv into <results_dir>.
"""
import csv
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from plot_latency import GRID, INK, METRICS, MUTED, SURFACE, load

COLOR = "#2a78d6"
CSV_STATS = ["mean", "median", "p90", "p99", "min", "max"]


def plot(runs, field, title, unit, scale, out):
    rates = [r for r, _ in runs]
    mean = [d["successes"]["latency"][field]["mean"] * scale for _, d in runs]
    p90 = [d["successes"]["latency"][field]["p90"] * scale for _, d in runs]
    fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.fill_between(rates, mean, p90, color=COLOR, alpha=0.15, linewidth=0, label="mean–p90 band")
    ax.plot(rates, mean, color=COLOR, linewidth=2, marker="o", markersize=6,
            markeredgecolor=SURFACE, markeredgewidth=1.5, label="mean")
    ax.plot(rates, p90, color=COLOR, linewidth=1.5, linestyle="--", marker="^", markersize=6,
            markeredgecolor=SURFACE, markeredgewidth=1.5, label="p90")
    ax.set_xscale("log", base=2)
    ax.set_xticks(rates)
    ax.set_xticklabels([f"{r:g}" for r in rates])
    ax.set_ylim(bottom=0)
    ax.set_xlabel("Request rate (req/s)", color=MUTED)
    ax.set_ylabel(f"Latency ({unit})", color=MUTED)
    ax.set_title(f"{title} vs request rate (mean, p90)", loc="left", color=INK, fontsize=12)
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


def write_csv(runs, out):
    header = ["rate", "requests", "throughput_req_s", "output_tokens_s"]
    for key, _, _, unit, _ in METRICS:
        header += [f"{key}_{s}_{unit}" for s in CSV_STATS]
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for rate, d in runs:
            tp = d["successes"]["throughput"]
            row = [rate, d["successes"]["count"],
                   round(tp["requests_per_sec"], 4), round(tp["output_tokens_per_sec"], 4)]
            for _, field, _, _, scale in METRICS:
                row += [round(d["successes"]["latency"][field][s] * scale, 4) for s in CSV_STATS]
            w.writerow(row)


def main():
    results_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    runs = load(results_dir)
    if not runs:
        sys.exit(f"no rate_* runs found in {results_dir}")
    for key, field, title, unit, scale in METRICS:
        out = os.path.join(results_dir, f"{key}_distribution.png")
        plot(runs, field, title, unit, scale, out)
        print(out)
    out = os.path.join(results_dir, "latency_summary.csv")
    write_csv(runs, out)
    print(out)


if __name__ == "__main__":
    main()
