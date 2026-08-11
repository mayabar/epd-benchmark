#!/usr/bin/env python3
"""Run a single benchmark session end-to-end.

Steps:
  1. Delete any existing sglang-bench job in the namespace.
  2. Detect whether the namespace is aggregated (one deployment containing
     "mm-baseline-nvidia-gpu") or disaggregated (two deployments containing
     "decode" and "encode").
  3. If --restart, scale the relevant deployment(s) to 0.
  4. Ensure deployment(s) are scaled to at least 1.
  5. Wait for the vLLM pod(s) to be Ready (can take 5-8 minutes).
  6. Port-forward each vLLM pod and save /metrics:
        - aggregated:    before.txt
        - disaggregated: e_before.txt, d_before.txt
  7. Apply the benchmark job yaml (file in folder starting with "benchmark").
  8. Follow job logs; when "All rates complete" appears, save log to output.txt.
  9. Read /metrics again -> after.txt / e_after.txt / d_after.txt.
 10. Run diff_vllm_histograms.py against the pair(s), saving diff.txt
     (aggregated) or e_diff.txt / d_diff.txt (disaggregated).
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import urllib.request

KUBECTL = "kubectl"
SCRIPT_DIR = Path(__file__).resolve().parent
DIFF_SCRIPT = SCRIPT_DIR / "diff_vllm_histograms.py"

AGG_MATCH = "mm-baseline-nvidia-gpu"
DECODE_MATCH = "decode"
ENCODE_MATCH = "encode"

JOB_NAME = "sglang-bench"
DONE_MARKER = "All rates complete"
POD_READY_TIMEOUT = 15 * 60  # 15 minutes
POD_POLL_INTERVAL = 15
VLLM_METRICS_PORT = 8000
VLLM_METRICS_PORT_DECODE = 8200  # disaggregated: decode pod's vLLM listens here


def run(cmd, check=True, capture=True, **kw):
    print(f"$ {' '.join(cmd) if isinstance(cmd, list) else cmd}", flush=True)
    r = subprocess.run(cmd, check=False, text=True,
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.PIPE if capture else None, **kw)
    if check and r.returncode != 0:
        sys.stderr.write(r.stderr or "")
        raise SystemExit(f"Command failed ({r.returncode}): {cmd}")
    return r


def kget_json(ns, kind, selector=None):
    cmd = [KUBECTL, "get", kind, "-n", ns, "-o", "json"]
    if selector:
        cmd += ["-l", selector]
    r = run(cmd)
    return json.loads(r.stdout)


def list_deployments(ns):
    data = kget_json(ns, "deployments")
    return [item["metadata"]["name"] for item in data.get("items", [])]


def detect_mode(ns):
    """Return ('agg', [dep_name]) or ('disagg', {'encode': name, 'decode': name})."""
    deps = list_deployments(ns)
    agg = [d for d in deps if AGG_MATCH in d]
    encode = [d for d in deps if ENCODE_MATCH in d]
    decode = [d for d in deps if DECODE_MATCH in d]
    if agg:
        return "agg", {"agg": agg[0]}
    if encode and decode:
        return "disagg", {"encode": encode[0], "decode": decode[0]}
    raise SystemExit(f"Could not detect mode from deployments: {deps}")


def get_replicas(ns, dep):
    r = run([KUBECTL, "get", "deployment", dep, "-n", ns,
             "-o", "jsonpath={.spec.replicas}"])
    try:
        return int(r.stdout.strip() or "0")
    except ValueError:
        return 0


def scale(ns, dep, replicas):
    run([KUBECTL, "scale", "deployment", dep, "-n", ns,
         f"--replicas={replicas}"])


def delete_job(ns, name=JOB_NAME):
    run([KUBECTL, "delete", "job", name, "-n", ns, "--ignore-not-found"],
        check=False)


def pods_for_deployment(ns, dep):
    """Return list of pod objects (dicts) belonging to `dep`."""
    d = kget_json(ns, "deployment/" + dep)
    match_labels = (d.get("spec", {}).get("selector", {})
                    .get("matchLabels", {}))
    if not match_labels:
        raise SystemExit(f"deployment {dep} has no matchLabels")
    selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
    pods = kget_json(ns, "pods", selector=selector)
    return pods.get("items", [])


def pod_is_ready(pod):
    for c in pod.get("status", {}).get("conditions", []):
        if c.get("type") == "Ready" and c.get("status") == "True":
            return True
    return False


def wait_for_deployment_ready(ns, dep, timeout=POD_READY_TIMEOUT):
    print(f"Waiting for deployment {dep} to become Ready (up to {timeout}s)...")
    start = time.time()
    while time.time() - start < timeout:
        pods = pods_for_deployment(ns, dep)
        if pods:
            statuses = []
            all_ready = True
            for p in pods:
                name = p["metadata"]["name"]
                ready = pod_is_ready(p)
                cs = p.get("status", {}).get("containerStatuses", []) or []
                ready_ct = sum(1 for c in cs if c.get("ready"))
                total_ct = len(cs)
                statuses.append(f"{name}={ready_ct}/{total_ct}")
                if not ready:
                    all_ready = False
            print(f"  [{int(time.time()-start)}s] {dep}: {', '.join(statuses)}")
            if all_ready:
                return pods
        else:
            print(f"  [{int(time.time()-start)}s] {dep}: no pods yet")
        time.sleep(POD_POLL_INTERVAL)
    raise SystemExit(f"Timed out waiting for {dep}")


def free_local_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def fetch_metrics(ns, pod_name, out_path, remote_port=VLLM_METRICS_PORT):
    """Port-forward pod:remote_port and save /metrics to out_path."""
    local = free_local_port()
    print(f"Port-forwarding {pod_name} :{remote_port} -> localhost:{local}")
    pf = subprocess.Popen(
        [KUBECTL, "port-forward", "-n", ns, f"pod/{pod_name}",
         f"{local}:{remote_port}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        # wait until socket is accepting
        deadline = time.time() + 30
        last_err = None
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", local), timeout=1):
                    break
            except OSError as e:
                last_err = e
                if pf.poll() is not None:
                    err = pf.stderr.read() if pf.stderr else ""
                    raise SystemExit(f"port-forward exited: {err}")
                time.sleep(0.5)
        else:
            raise SystemExit(f"port-forward never became ready: {last_err}")

        url = f"http://127.0.0.1:{local}/metrics"
        with urllib.request.urlopen(url, timeout=30) as r:
            body = r.read().decode("utf-8", errors="replace")
        Path(out_path).write_text(body)
        print(f"Wrote {out_path} ({len(body)} bytes)")
    finally:
        pf.terminate()
        try:
            pf.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pf.kill()


def find_job_yaml(folder):
    for p in sorted(Path(folder).iterdir()):
        if p.is_file() and p.name.startswith("benchmark") and \
           p.suffix in (".yaml", ".yml"):
            return p
    raise SystemExit(f"No benchmark*.yaml file found in {folder}")


def apply_job(ns, yaml_path):
    run([KUBECTL, "apply", "-f", str(yaml_path), "-n", ns])


def follow_job_logs(ns, out_path):
    """Stream job logs to out_path; stop when DONE_MARKER seen."""
    print(f"Following logs of job/{JOB_NAME} in {ns}; saving to {out_path}")
    # Wait until the job has an active pod so `logs -f` doesn't race.
    for _ in range(60):
        r = run([KUBECTL, "get", "job", JOB_NAME, "-n", ns,
                 "-o", "jsonpath={.status.active}"], check=False)
        if r.stdout.strip() not in ("", "0"):
            break
        time.sleep(2)

    proc = subprocess.Popen(
        [KUBECTL, "logs", "-f", f"job/{JOB_NAME}", "-n", ns],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        bufsize=1,
    )
    done = False
    try:
        with open(out_path, "w") as f:
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                f.write(line)
                if DONE_MARKER in line:
                    done = True
                    break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if not done:
        print(f"WARNING: '{DONE_MARKER}' not seen; log stream ended early.")


def run_diff(before, after, out):
    if Path(out).exists():
        Path(out).unlink()
    run([sys.executable, str(DIFF_SCRIPT), str(before), str(after),
         "-o", str(out)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("namespace")
    ap.add_argument("folder", help="folder containing benchmark*.yaml")
    ap.add_argument("--restart", action="store_true",
                    help="scale deployment(s) down to 0 before scaling up")
    args = ap.parse_args()

    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        raise SystemExit(f"Not a directory: {folder}")
    ns = args.namespace

    # 1. delete any existing job
    delete_job(ns)

    # 2. detect mode
    mode, deps = detect_mode(ns)
    print(f"Detected mode: {mode}  deployments: {deps}")

    # 3. restart if requested
    if args.restart:
        for name in deps.values():
            scale(ns, name, 0)
        # small pause so scale-down begins before scale-up
        time.sleep(3)

    # 4. ensure scaled up to >=1
    for name in deps.values():
        if get_replicas(ns, name) == 0:
            scale(ns, name, 1)

    # 5. wait until Ready
    ready_pods = {}
    for key, name in deps.items():
        pods = wait_for_deployment_ready(ns, name)
        # use the first pod in the deployment
        ready_pods[key] = pods[0]["metadata"]["name"]
    print(f"Pods ready: {ready_pods}")

    # 6. metrics BEFORE
    if mode == "agg":
        before_paths = {"agg": folder / "before.txt"}
    else:
        before_paths = {"encode": folder / "e_before.txt",
                        "decode": folder / "d_before.txt"}
    for key, pod in ready_pods.items():
        port = VLLM_METRICS_PORT_DECODE if key == "decode" else VLLM_METRICS_PORT
        fetch_metrics(ns, pod, before_paths[key], remote_port=port)

    # 7. apply job
    yaml_path = find_job_yaml(folder)
    apply_job(ns, yaml_path)

    # 8. follow logs -> output.txt
    follow_job_logs(ns, folder / "output.txt")

    # 9. metrics AFTER (re-lookup pods; they should still be the same)
    after_paths = ({"agg": folder / "after.txt"} if mode == "agg"
                   else {"encode": folder / "e_after.txt",
                         "decode": folder / "d_after.txt"})
    current_pods = {}
    for key, name in deps.items():
        pods = pods_for_deployment(ns, name)
        pods = [p for p in pods if pod_is_ready(p)]
        if not pods:
            raise SystemExit(f"No ready pods for {name} after benchmark")
        current_pods[key] = pods[0]["metadata"]["name"]
    for key, pod in current_pods.items():
        port = VLLM_METRICS_PORT_DECODE if key == "decode" else VLLM_METRICS_PORT
        fetch_metrics(ns, pod, after_paths[key], remote_port=port)

    # 10. diff
    if mode == "agg":
        run_diff(before_paths["agg"], after_paths["agg"], folder / "diff.txt")
    else:
        run_diff(before_paths["encode"], after_paths["encode"],
                 folder / "e_diff.txt")
        run_diff(before_paths["decode"], after_paths["decode"],
                 folder / "d_diff.txt")

    print("Benchmark session complete.")


if __name__ == "__main__":
    main()
