#!/usr/bin/env python3
"""Run an sglang benchmark job N times, capturing bench output and vLLM queue load.

Per iteration <i>:
  1. delete a leftover job of the same name and wait for its pod to disappear
  2. start watch_vllm_queue.py  -> <folder>/load<i>.log
  3. kubectl apply -f <folder>/benchmark-job.yaml
  4. kubectl logs -f job/<job>  -> <folder>/log<i>.log   (blocks until the job ends)
  5. keep the watcher going until the queue has drained -- the last
     DRAIN_SAMPLES lines of load<i>.log all read "<time> 0 0" -- then stop it

The watcher scrapes a vLLM /metrics endpoint over a port-forward, so one has to be
running before this script starts (see --metrics-url).

Examples:
  ./run_tests.py jobs/epd-r1-t60-in128-out128-img4 3
  ./run_tests.py jobs/epd-r1-t60-in128-out128-img4 5 -n mayab-diaggr --start-index 1 --overwrite
"""

import argparse
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
WATCHER = SCRIPT_DIR / "watch_vllm_queue.py"

# A watcher sample with nothing running and nothing queued, e.g. "12:27:52 0 0".
IDLE_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\s+0\s+0\s*$")
# Trailing idle samples needed before the queue counts as drained (1 sample/sec).
DRAIN_SAMPLES = 45


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def kubectl(*args: str, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    cmd = ["kubectl", *args]
    if not quiet:
        log("$ " + " ".join(cmd))
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def preflight_cluster(namespace: str) -> None:
    res = kubectl("get", "namespace", namespace, "-o", "name", check=False, quiet=True)
    if res.returncode != 0:
        sys.exit(
            f"ERROR: namespace {namespace} not reachable ({res.stderr.strip()}).\n"
            f"Current KUBECONFIG={os.environ.get('KUBECONFIG', '(unset -- using ~/.kube/config)')}\n"
            "Export the right kubeconfig or pass --kubeconfig."
        )


def preflight_metrics(url: str) -> None:
    try:
        with urllib.request.urlopen(url, timeout=5):
            return
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        sys.exit(
            f"ERROR: cannot reach the vLLM metrics endpoint {url} ({exc}).\n"
            "Start a port-forward first, e.g.:\n"
            "  kubectl port-forward -n mayab-aggr deploy/mm-baseline-nvidia-gpu-vllm-decode 8022:8000"
        )


def job_pods(namespace: str, job: str) -> list[str]:
    res = kubectl(
        "get", "pods", "-n", namespace, "-l", f"job-name={job}",
        "-o", "jsonpath={range .items[*]}{.metadata.name}:{.status.phase} {end}",
        check=False, quiet=True,
    )
    return res.stdout.split()


def delete_job(namespace: str, job: str, timeout: float) -> None:
    kubectl("delete", "job", job, "-n", namespace, "--ignore-not-found", "--wait=true", check=False)
    # kubectl returns as soon as the Job object is gone; its pod can outlive it
    # briefly, and `kubectl logs job/...` picks pods by label -- so wait it out.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not job_pods(namespace, job):
            return
        time.sleep(2)
    log(f"WARNING: pods for job/{job} still present after {timeout:.0f}s, continuing anyway")


def wait_for_pod(namespace: str, job: str, timeout: float) -> str:
    """Block until the job's pod is streamable (or already finished). Returns its phase."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        pods = job_pods(namespace, job)
        if pods:
            name, _, phase = pods[0].partition(":")
            if phase in ("Running", "Succeeded", "Failed"):
                log(f"pod {name} is {phase}")
                return phase
            if pods[0] != last:
                log(f"waiting for pod {name} ({phase or 'no phase yet'}) ...")
                last = pods[0]
        time.sleep(3)
    sys.exit(f"ERROR: job/{job} pod did not start within {timeout:.0f}s")


def stream_done(namespace: str, job: str) -> bool:
    """True only when the bench container is known to have exited.

    Anything unreadable -- a failed API call, a pod that has not reported status
    yet -- counts as "not done", so a transient blip makes the caller reattach
    instead of silently truncating the log. Note that the pod's phase lags the
    container by a few seconds, so the container's terminated state is checked too.
    """
    res = kubectl(
        "get", "pods", "-n", namespace, "-l", f"job-name={job}", "-o",
        "jsonpath={.items[0].status.containerStatuses[0].state.terminated.reason}"
        "{'|'}{.items[0].status.phase}",
        check=False, quiet=True,
    )
    if res.returncode != 0:
        log(f"  pod state unreadable ({res.stderr.strip().splitlines()[-1:] or ['?']}), assuming still running")
        return False
    reason, _, phase = res.stdout.strip().partition("|")
    if reason or phase in ("Succeeded", "Failed"):
        return True
    if phase:
        return False
    # No pod matched. It may have been cleaned up after finishing -- ask the Job.
    res = kubectl(
        "get", "job", job, "-n", namespace,
        "-o", "jsonpath={.status.succeeded}{'|'}{.status.failed}", check=False, quiet=True,
    )
    return res.returncode == 0 and any(part.strip() for part in res.stdout.split("|"))


def start_watcher(out_path: Path, url: str, interval: float) -> tuple[subprocess.Popen, object]:
    handle = out_path.open("w")
    proc = subprocess.Popen(
        [sys.executable, str(WATCHER), "--url", url, "--interval", str(interval)],
        stdout=handle, stderr=subprocess.STDOUT,
    )
    log(f"watcher pid {proc.pid} -> {out_path}")
    return proc, handle


def stop_watcher(proc: subprocess.Popen, handle) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)  # the watcher exits cleanly on SIGINT
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    handle.close()
    log("watcher stopped")


def stream_logs(namespace: str, job: str, out_path: Path, attempts: int = 10) -> None:
    """Follow the job's logs into out_path until the bench container has exited.

    `kubectl logs` always replays the container's log from the start, so a naive
    append on reattach duplicates everything already captured. Lines are counted
    instead and the replayed prefix is skipped, making a reattach a true resume.
    """
    written = 0  # complete lines already in out_path
    for attempt in range(1, attempts + 1):
        log(f"$ kubectl logs -f job/{job} -n {namespace} > {out_path}"
            + (f"  (attempt {attempt}, resuming after {written} lines)" if attempt > 1 else ""))
        resume_at = written  # lines of replay to drop before appending again
        # Binary mode keeps the progress-bar carriage returns byte-for-byte.
        with out_path.open("wb" if attempt == 1 else "ab") as handle:
            proc = subprocess.Popen(
                ["kubectl", "logs", "-f", f"job/{job}", "-n", namespace],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            skipped = 0
            partial = b""
            for line in proc.stdout:
                if not line.endswith(b"\n"):
                    partial = line  # incomplete tail; only keep it if the log is done
                    break
                if skipped < resume_at:
                    skipped += 1
                    continue
                handle.write(line)
                handle.flush()
                written += 1
            proc.stdout.close()
            proc.wait()
            done = stream_done(namespace, job)
            if partial and done and skipped >= resume_at:
                handle.write(partial)

        if done:
            log(f"log stream ended, container finished after {written} lines")
            return
        log(f"log stream dropped at {written} lines while the container is still up, reattaching")
        time.sleep(3)
    log("WARNING: gave up reattaching to the log stream")


def check_complete(log_path: Path, marker: str) -> bool:
    """Warn loudly if the captured log is missing the run's end marker."""
    try:
        text = log_path.read_text(errors="replace")
    except OSError as exc:
        log(f"WARNING: cannot read {log_path.name} ({exc})")
        return False
    hits = text.count(marker)
    if hits == 1:
        return True
    if hits == 0:
        log(f"WARNING: {log_path.name} has no '{marker}' -- the capture looks truncated")
    else:
        log(f"WARNING: {log_path.name} contains '{marker}' {hits} times -- looks duplicated")
    return False


def trailing_idle(path: Path) -> int:
    """How many trailing samples in the load log show 0 running / 0 waiting."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return 0
    count = 0
    for line in reversed(lines):
        if not line.strip():
            continue
        if IDLE_RE.match(line):
            count += 1
            continue
        break
    return count


def wait_for_drain(load_path: Path, timeout: float, interval: float) -> bool:
    log(f"waiting for {DRAIN_SAMPLES} idle samples in {load_path.name} ...")
    deadline = time.monotonic() + timeout
    reported = -1
    while time.monotonic() < deadline:
        idle = trailing_idle(load_path)
        if idle >= DRAIN_SAMPLES:
            log(f"queue drained ({idle} idle samples)")
            return True
        if idle != reported:
            log(f"  {idle}/{DRAIN_SAMPLES} idle samples")
            reported = idle
        time.sleep(interval)
    log(f"WARNING: queue still busy after {timeout:.0f}s, moving on")
    return False


def next_free_index(folder: Path) -> int:
    used = [
        int(m.group(1))
        for entry in folder.iterdir()
        if (m := re.fullmatch(r"(?:log|load)(\d+)\.log", entry.name))
    ]
    return max(used, default=0) + 1


def run_iteration(args, folder: Path, index: int) -> None:
    log_path = folder / f"log{index}.log"
    load_path = folder / f"load{index}.log"
    log(f"===== iteration {index}: {log_path.name} / {load_path.name} =====")

    delete_job(args.namespace, args.job, args.delete_timeout)

    watcher, handle = start_watcher(load_path, args.metrics_url, args.interval)
    try:
        kubectl("apply", "-f", str(folder / args.manifest), "-n", args.namespace)
        wait_for_pod(args.namespace, args.job, args.start_timeout)
        stream_logs(args.namespace, args.job, log_path)
        check_complete(log_path, args.end_marker)
        wait_for_drain(load_path, args.drain_timeout, args.interval)
    finally:
        stop_watcher(watcher, handle)
    log(f"===== iteration {index} finished =====")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("folder", type=Path, help="folder holding benchmark-job.yaml; logs are written here")
    ap.add_argument("count", type=int, help="number of test iterations to run")
    ap.add_argument("-n", "--namespace", default="mayab-aggr")
    ap.add_argument("--kubeconfig", help="kubeconfig to use (default: the KUBECONFIG already in the environment)")
    ap.add_argument("--job", default="sglang-bench", help="Job name in the manifest (default: sglang-bench)")
    ap.add_argument("--manifest", default="benchmark-job.yaml")
    ap.add_argument("--metrics-url", default="http://localhost:8022/metrics",
                    help="vLLM /metrics URL the watcher polls (default: %(default)s)")
    ap.add_argument("--interval", type=float, default=1.0, help="watcher sample interval in seconds")
    ap.add_argument("--end-marker", default="All rates complete",
                    help="string the bench script prints once when done; used to spot a truncated capture")
    ap.add_argument("--start-index", type=int,
                    help="first log index (default: one past the highest log<N>.log/load<N>.log present)")
    ap.add_argument("--overwrite", action="store_true",
                    help="allow overwriting existing log files at the chosen indices")
    ap.add_argument("--start-timeout", type=float, default=900.0,
                    help="seconds to wait for the bench pod to start (default: %(default)s)")
    ap.add_argument("--drain-timeout", type=float, default=900.0,
                    help="seconds to wait for the queue to drain after a run (default: %(default)s)")
    ap.add_argument("--delete-timeout", type=float, default=180.0,
                    help="seconds to wait for the previous job's pod to go away (default: %(default)s)")
    args = ap.parse_args()

    folder: Path = args.folder.resolve()
    if not (folder / args.manifest).is_file():
        sys.exit(f"ERROR: {folder / args.manifest} not found")
    if args.count < 1:
        sys.exit("ERROR: count must be >= 1")
    if not WATCHER.is_file():
        sys.exit(f"ERROR: watcher script {WATCHER} not found")

    start = args.start_index or next_free_index(folder)
    indices = list(range(start, start + args.count))
    if not args.overwrite:
        clashes = [p.name for i in indices for p in (folder / f"log{i}.log", folder / f"load{i}.log") if p.exists()]
        if clashes:
            sys.exit(f"ERROR: would overwrite {', '.join(clashes)} -- pass --overwrite or --start-index")

    if args.kubeconfig:
        os.environ["KUBECONFIG"] = str(Path(args.kubeconfig).expanduser())
    preflight_cluster(args.namespace)
    preflight_metrics(args.metrics_url)
    log(f"namespace {args.namespace}, folder {folder}")
    log(f"running iterations {indices[0]}..{indices[-1]}")

    began = time.monotonic()
    try:
        for index in indices:
            run_iteration(args, folder, index)
    except KeyboardInterrupt:
        log("interrupted")
        return 130
    log(f"all {args.count} iterations done in {(time.monotonic() - began) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
