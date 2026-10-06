#!/usr/bin/env python3
"""Poll a vLLM /metrics endpoint every second and print running/waiting request counts.

Output: time(hh:mm:ss) running waiting
Stop with Ctrl-C (SIGINT) or kill.
"""

import argparse
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

DEFAULT_URL = "http://localhost:8022/metrics"


def make_pattern(metric: str) -> re.Pattern:
    # Matches e.g. vllm:num_requests_running{engine="0",model_name="..."} 0.0
    return re.compile(r"^" + re.escape(metric) + r"(?:\{[^}]*\})?\s+([0-9eE+.\-]+)\s*$")


RUNNING_RE = make_pattern("vllm:num_requests_running")
WAITING_RE = make_pattern("vllm:num_requests_waiting")


def scrape(url: str, timeout: float):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")

    running = waiting = None
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        m = RUNNING_RE.match(line)
        if m:
            running = float(m.group(1))
            continue
        m = WAITING_RE.match(line)
        if m:
            waiting = float(m.group(1))
    return running, waiting


def fmt(value):
    return "n/a" if value is None else f"{value:g}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=DEFAULT_URL, help=f"metrics URL (default: {DEFAULT_URL})")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between samples (default: 1)")
    ap.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout in seconds (default: 5)")
    args = ap.parse_args()

    print("time     running waiting", flush=True)
    try:
        while True:
            start = time.monotonic()
            stamp = datetime.now().strftime("%H:%M:%S")
            try:
                running, waiting = scrape(args.url, args.timeout)
                print(f"{stamp} {fmt(running)} {fmt(waiting)}", flush=True)
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                print(f"{stamp} ERROR {exc}", file=sys.stderr, flush=True)

            sleep_for = args.interval - (time.monotonic() - start)
            if sleep_for > 0:
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
