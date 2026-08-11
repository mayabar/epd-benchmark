#!/usr/bin/env python3
"""Build per-bucket (non-cumulative) after-minus-before histograms for vLLM metrics.

Usage:
    python3 diff_vllm_histograms.py <before.txt> <after.txt> [-o out.txt]

For each histogram metric listed in METRICS the script:
  1. de-cumulates each bucket (count in bucket i = cum[i] - cum[i-1])
  2. subtracts the before value from the after value
and writes the resulting per-bucket deltas to the output file.
"""

import argparse
import os
import re
import sys
from collections import defaultdict

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
]

LINE_RE = re.compile(
    r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)'
    r'(\{(?P<labels>[^}]*)\})?'
    r'\s+(?P<value>\S+)'
)
LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def parse_histograms(path):
    """Return dict: base_metric -> label_key (tuple excl. 'le') -> {
        'buckets': sorted [(le_float, le_str, cum_value), ...],
        'count':   float or None,
        'sum':     float or None,
    }.
    """
    tmp = defaultdict(lambda: defaultdict(lambda: {"buckets": [], "count": None, "sum": None}))
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = LINE_RE.match(line)
            if not m:
                continue
            name = m.group("name")
            try:
                value = float(m.group("value"))
            except ValueError:
                continue
            labels = dict(LABEL_RE.findall(m.group("labels") or ""))

            if name.endswith("_bucket"):
                base = name[: -len("_bucket")]
                le_str = labels.pop("le", None)
                if le_str is None:
                    continue
                try:
                    le_val = float(le_str)
                except ValueError:
                    le_val = float("inf")
                label_key = tuple(sorted(labels.items()))
                tmp[base][label_key]["buckets"].append((le_val, le_str, value))
            elif name.endswith("_count"):
                base = name[: -len("_count")]
                label_key = tuple(sorted(labels.items()))
                tmp[base][label_key]["count"] = value
            elif name.endswith("_sum"):
                base = name[: -len("_sum")]
                label_key = tuple(sorted(labels.items()))
                tmp[base][label_key]["sum"] = value

    result = {}
    for base, per_label in tmp.items():
        result[base] = {}
        for lk, entry in per_label.items():
            entry["buckets"].sort(key=lambda x: x[0])
            result[base][lk] = entry
    return result


def resolve_metric(requested, series):
    if requested in series:
        return requested
    if requested.startswith("vllm:"):
        aliased = "vllm:request_" + requested[len("vllm:"):]
        if aliased in series:
            return aliased
    return None


def fmt_num(v):
    """Format as plain decimal. Integers -> no decimals; floats -> 2 decimals. Never scientific."""
    if v is None:
        return "n/a"
    if float(v).is_integer():
        return f"{int(v)}"
    return f"{v:.2f}"


def de_cumulate(buckets):
    """Given sorted [(le_val, le_str, cum), ...] return [(le_str, non_cum), ...]."""
    out = []
    prev = 0.0
    for _, le_str, cum in buckets:
        out.append((le_str, cum - prev))
        prev = cum
    return out


def format_labels(label_key, le_str):
    parts = [f'{k}="{v}"' for k, v in label_key]
    parts.append(f'le="{le_str}"')
    return "{" + ",".join(parts) + "}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("-o", "--output", default="histogram_diff.txt")
    args = ap.parse_args()

    if os.path.exists(args.output):
        print(f"ERROR: output file already exists: {args.output}", file=sys.stderr)
        sys.exit(1)

    before = parse_histograms(args.before)
    after = parse_histograms(args.after)

    with open(args.output, "x") as out:
        out.write(f"Per-bucket (non-cumulative) after-minus-before histogram deltas\n")
        out.write(f"before = {args.before}\n")
        out.write(f"after  = {args.after}\n\n")

        for requested in METRICS:
            b_base = resolve_metric(requested, before)
            a_base = resolve_metric(requested, after)
            if b_base is None and a_base is None:
                out.write(f"{requested}\n{'-' * len(requested)}\nNOT FOUND\n\n")
                continue

            actual = b_base or a_base
            title = requested if actual == requested else f"{requested} (actual: {actual})"
            out.write(f"{title}\n{'-' * len(title)}\n")

            label_keys = set()
            if b_base:
                label_keys |= set(before[b_base].keys())
            if a_base:
                label_keys |= set(after[a_base].keys())

            for lk in sorted(label_keys):
                b_entry = before.get(b_base, {}).get(lk, {"buckets": [], "count": None, "sum": None})
                a_entry = after.get(a_base, {}).get(lk, {"buckets": [], "count": None, "sum": None})
                b_buckets = b_entry["buckets"]
                a_buckets = a_entry["buckets"]
                b_non_cum = dict(de_cumulate(b_buckets))
                a_non_cum = dict(de_cumulate(a_buckets))

                all_le = []
                seen = set()
                for _, le_str, _ in (a_buckets or b_buckets):
                    if le_str not in seen:
                        seen.add(le_str)
                        all_le.append(le_str)
                for _, le_str, _ in (b_buckets if a_buckets else []):
                    if le_str not in seen:
                        seen.add(le_str)
                        all_le.append(le_str)

                if len(label_keys) > 1:
                    label_str = ", ".join(f'{k}="{v}"' for k, v in lk) or "(no labels)"
                    out.write(f"[{label_str}]\n")

                out.write("Buckets:\n")
                out.write(f"  {'bucket limit':>14} | value\n")
                out.write(f"  {'-' * 14}-+-{'-' * 10}\n")
                for le_str in all_le:
                    delta = a_non_cum.get(le_str, 0.0) - b_non_cum.get(le_str, 0.0)
                    out.write(f"  {le_str:>14} | {fmt_num(delta)}\n")

                count_delta = (a_entry["count"] or 0.0) - (b_entry["count"] or 0.0)
                sum_delta = (a_entry["sum"] or 0.0) - (b_entry["sum"] or 0.0)
                out.write(f"Count: {fmt_num(count_delta)}\n")
                out.write(f"Sum: {fmt_num(sum_delta)}\n")
            out.write("\n")

    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
