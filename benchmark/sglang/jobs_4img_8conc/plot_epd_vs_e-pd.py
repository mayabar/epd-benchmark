#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["matplotlib"]
# ///
"""epd vs e-pd (Qwen3-VL-32B, 4x 1080p images, max concurrency 8): mean E2E, TTFT and TPOT
against request rate, from the sglang.bench_serving result blocks collected so far.

Reads every *.log except load*.log in <setup>/r<rate>-*/ and backup/<setup>/r<rate>-*/ (setup =
epd, e-pd), keeps the logs that contain a finished "Serving Benchmark Result" block, and averages
the per-run means when a rate has more than one run (each run is also drawn as a small dot).
Writes to epd_vs_e-pd/ (next to this script):
  e2e_comparison.png, ttft_comparison.png, tpot_comparison.png
  summary.csv  (one row per run: the table view of the graphs)

Usage (from anywhere):
  uv run benchmark/sglang/jobs_4img_8conc/plot_epd_vs_e-pd.py
"""

import csv
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent  # jobs_4img_8conc
OUT = ROOT / "epd_vs_e-pd"

# (setup folder, label, color, marker) - same hues as the inference_perf epd/e-pd graphs
SERIES = [
    ("epd", "epd (aggregated)", "#3987e5", "o"),
    ("e-pd", "e-pd (encode-disaggregated)", "#eb6834", "s"),
]
METRICS = [  # (file stem, bench_serving line, title)
    ("e2e", "Mean E2E Latency (ms)", "Mean E2E request latency"),
    ("ttft", "Mean TTFT (ms)", "Mean time to first token (TTFT)"),
    ("tpot", "Mean TPOT (ms)", "Mean time per output token (TPOT)"),
]
RESULT_HEADER = "Serving Benchmark Result"
TEXT_PRIMARY, TEXT_SECONDARY, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"


def parse_log(path: Path) -> dict[str, float] | None:
    """{bench_serving line: value} for the result block in `path`, or None if it has none."""
    text = path.read_text(errors="replace")
    if RESULT_HEADER not in text:
        return None
    block = text.split(RESULT_HEADER, 1)[1]
    values = {}
    for key in ["Traffic request rate", "Successful requests"] + [line for _, line, _ in METRICS]:
        m = re.search(rf"^{re.escape(key)}:\s+([0-9.]+)", block, re.MULTILINE)
        if m is None:
            return None  # block cut off mid-way
        values[key] = float(m.group(1))
    return values


def load(setup: str) -> list[dict]:
    """One dict per finished run of `setup`: rate, source log and the parsed values."""
    runs = []
    for case in sorted([*ROOT.glob(f"{setup}/r*-*"), *ROOT.glob(f"backup/{setup}/r*-*")]):
        folder_rate = float(case.name.split("-", 1)[0][1:])  # "r0.5-t128-..." -> 0.5
        for log in sorted(case.glob("*.log")):
            if log.name.startswith("load"):
                continue  # queue-watcher output, not benchmark results
            values = parse_log(log)
            if values is None:
                continue
            if values["Traffic request rate"] != folder_rate:
                raise SystemExit(f"{log}: rate {values['Traffic request rate']} != folder rate {folder_rate}")
            runs.append({"rate": folder_rate, "log": str(log.relative_to(ROOT)), **values})
    return runs


def averaged(runs: list[dict], line: str) -> tuple[list[float], list[float]]:
    rates = sorted({r["rate"] for r in runs})
    means = [sum(r[line] for r in runs if r["rate"] == rate) / sum(r["rate"] == rate for r in runs)
             for rate in rates]
    return rates, means


def plot(stem: str, line: str, title: str, data: dict[str, list[dict]], out: Path) -> None:
    fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for setup, label, color, marker in SERIES:
        runs = data[setup]
        if not runs:
            continue
        rates, means = averaged(runs, line)
        ax.plot(rates, [m / 1000 for m in means] if stem != "tpot" else means, "-", color=color,
                linewidth=2, marker=marker, markersize=8, markeredgecolor=SURFACE,
                markeredgewidth=1.5, label=label, zorder=3)
        # individual runs, only where a rate has more than one
        multi = [r for r in runs if sum(o["rate"] == r["rate"] for o in runs) > 1]
        if multi:
            ys = [r[line] / 1000 if stem != "tpot" else r[line] for r in multi]
            ax.scatter([r["rate"] for r in multi], ys, s=18, facecolors="none", edgecolors=color,
                       linewidths=1, zorder=2, label=f"{label.split()[0]} single runs (line = their average)")
        # direct label at the series' last point
        last_y = means[-1] / 1000 if stem != "tpot" else means[-1]
        ax.annotate(label.split()[0], (rates[-1], last_y), xytext=(8, 0), textcoords="offset points",
                    va="center", fontsize=9, color=TEXT_PRIMARY)
    rates = sorted({r["rate"] for runs in data.values() for r in runs})
    ax.set_xticks(rates, [f"{r:g}" for r in rates])
    ax.set_xlim(0, max(rates) * 1.12 if rates else 1)
    ax.set_ylim(bottom=0)
    ax.set_xlabel("Request rate (req/s)", color=TEXT_SECONDARY)
    ax.set_ylabel(f"Latency ({'ms' if stem == 'tpot' else 's'})", color=TEXT_SECONDARY)
    ax.set_title(f"{title} vs request rate: epd vs e-pd\n"
                 "Qwen3-VL-32B, 4x 1080p images, 128 in / 128 out tokens, max concurrency 8",
                 color=TEXT_PRIMARY, fontsize=11, loc="left")
    ax.grid(True, axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=TEXT_SECONDARY, labelsize=9)
    ax.legend(frameon=False, fontsize=9, labelcolor=TEXT_PRIMARY, loc="upper left")
    fig.tight_layout()
    fig.savefig(out, facecolor=SURFACE)
    plt.close(fig)


def main() -> None:
    data = {setup: load(setup) for setup, *_ in SERIES}
    if not any(data.values()):
        raise SystemExit(f"no finished bench_serving results under {ROOT}")
    OUT.mkdir(exist_ok=True)
    for stem, line, title in METRICS:
        plot(stem, line, title, data, OUT / f"{stem}_comparison.png")
    with open(OUT / "summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["setup", "rate", "requests", "e2e_mean_ms", "ttft_mean_ms", "tpot_mean_ms", "log"])
        for setup, *_ in SERIES:
            for r in sorted(data[setup], key=lambda r: (r["rate"], r["log"])):
                w.writerow([setup, r["rate"], int(r["Successful requests"]),
                            *(r[line] for _, line, _ in METRICS), r["log"]])
    print(f"wrote {OUT}/{{e2e,ttft,tpot}}_comparison.png and summary.csv")
    print(f"{'setup':5} {'rate':>4} {'E2E s':>7} {'TTFT s':>7} {'TPOT ms':>8}  log")
    for setup, *_ in SERIES:
        for r in sorted(data[setup], key=lambda r: (r["rate"], r["log"])):
            print(f"{setup:5} {r['rate']:>4g} {r['Mean E2E Latency (ms)'] / 1000:7.2f} "
                  f"{r['Mean TTFT (ms)'] / 1000:7.2f} {r['Mean TPOT (ms)']:8.1f}  {r['log']}")


if __name__ == "__main__":
    main()
