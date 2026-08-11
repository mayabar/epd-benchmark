#!/usr/bin/env python3
"""Compare vLLM Prometheus metrics between a before/after metrics dump.

Usage:
    python3 compare_vllm_metrics.py [before.txt] [after.txt]

Defaults to results/aggregated/out 128/before.txt and after.txt.
"""

import re
import sys

DEFAULT_BEFORE = "results/aggregated/out 128/before.txt"
DEFAULT_AFTER = "results/aggregated/out 128/after.txt"

# Requested metric names. Some are matched via an alias if the exact name
# isn't present in the dump (e.g. vLLM renamed time_per_output_token_seconds
# to request_time_per_output_token_seconds).
METRICS = [
    "vllm:e2e_request_latency_seconds",
    "vllm:time_to_first_token_seconds",
    "vllm:request_inference_time_seconds",
    "vllm:request_prefill_time_seconds",
    "vllm:request_decode_time_seconds",
    "vllm:time_per_output_token_seconds",
    "vllm:inter_token_latency_seconds",
    "vllm:request_generation_tokens",
    "vllm:request_prompt_tokens",
    "vllm:generation_tokens_total",
    "vllm:prompt_tokens_total",
]

LINE_RE = re.compile(
    r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)'
    r'(\{(?P<labels>[^}]*)\})?'
    r'\s+(?P<value>\S+)'
)
LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def parse_file(path):
    """Return dict: base_metric_name -> {label_key (sorted tuple, excl. 'le'): {..fields..}}"""
    series = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = LINE_RE.match(line)
            if not m:
                continue
            name = m.group("name")
            raw_labels = m.group("labels") or ""
            try:
                value = float(m.group("value"))
            except ValueError:
                continue
            labels = dict(LABEL_RE.findall(raw_labels))
            labels.pop("le", None)
            label_key = tuple(sorted(labels.items()))

            if name.endswith("_bucket"):
                continue  # histogram buckets aren't needed for avg comparison
            elif name.endswith("_count"):
                base = name[: -len("_count")]
                field = "count"
            elif name.endswith("_sum"):
                base = name[: -len("_sum")]
                field = "sum"
            elif name.endswith("_created"):
                continue
            else:
                base = name
                field = "value"

            series.setdefault(base, {}).setdefault(label_key, {})[field] = value
    return series


def resolve_metric(requested, series):
    """Find the actual base metric name in `series` for a requested metric name."""
    if requested in series:
        return requested
    # alias: vllm:X_seconds -> vllm:request_X_seconds
    if requested.startswith("vllm:"):
        aliased = "vllm:request_" + requested[len("vllm:"):]
        if aliased in series:
            return aliased
    return None


def fmt(v):
    if v is None:
        return "n/a"
    return f"{v:.6g}"


def pct_change(before, after):
    if before is None or after is None:
        return "n/a"
    if before == 0:
        return "n/a (before=0)" if after == 0 else "+inf"
    return f"{(after - before) / abs(before) * 100:+.2f}%"


def main():
    before_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BEFORE
    after_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_AFTER

    before_series = parse_file(before_path)
    after_series = parse_file(after_path)

    print(f"Comparing:\n  before = {before_path}\n  after  = {after_path}\n")

    for requested in METRICS:
        before_base = resolve_metric(requested, before_series)
        after_base = resolve_metric(requested, after_series)

        if before_base is None and after_base is None:
            print(f"=== {requested} ===\n  NOT FOUND in either file\n")
            continue

        actual_name = before_base or after_base
        note = f" (actual name: {actual_name})" if actual_name != requested else ""
        print(f"=== {requested}{note} ===")

        label_keys = set()
        if before_base:
            label_keys |= set(before_series[before_base].keys())
        if after_base:
            label_keys |= set(after_series[after_base].keys())

        for lk in sorted(label_keys):
            label_str = ", ".join(f'{k}="{v}"' for k, v in lk) or "(no labels)"
            b = before_series.get(before_base, {}).get(lk, {})
            a = after_series.get(after_base, {}).get(lk, {})

            if "count" in b or "count" in a:
                # histogram: report count, sum, and derived average
                b_count, b_sum = b.get("count"), b.get("sum")
                a_count, a_sum = a.get("count"), a.get("sum")
                b_avg = (b_sum / b_count) if b_count else None
                a_avg = (a_sum / a_count) if a_count else None

                print(f"  [{label_str}]")
                print(f"    count : before={fmt(b_count)}  after={fmt(a_count)}")
                print(f"    sum   : before={fmt(b_sum)}  after={fmt(a_sum)}")
                print(f"    avg   : before={fmt(b_avg)}  after={fmt(a_avg)}  "
                      f"change={pct_change(b_avg, a_avg)}")
            else:
                # counter/gauge: single value
                b_val, a_val = b.get("value"), a.get("value")
                print(f"  [{label_str}]")
                print(f"    value : before={fmt(b_val)}  after={fmt(a_val)}  "
                      f"change={pct_change(b_val, a_val)}")
        print()


if __name__ == "__main__":
    main()
